# -*- coding: utf-8 -*-
"""
ماژول تامین‌کننده داده‌های بازار سرمایه (TSE Data Provider)
این ماژول ارتباط با پکیج pytse-client را مدیریت کرده و داده‌های زنده و تاریخی بورس و فرابورس را دریافت می‌کند.
تمام خطاها مدیریت شده و هرگز داده ساختگی یا فرضی تولید نمی‌شود.
"""

import time
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


class TSEDataProvider:
    """
    تامین‌کننده داده‌های بازار بورس و فرابورس تهران با استفاده از pytse-client.
    """

    def __init__(self):
        """
        مقداردهی اولیه و واردسازی کتابخانه pytse_client با تنظیمات مورد نیاز.
        """
        try:
            import pytse_client as tse
            self.tse = tse
            logger.info("ماژول pytse-client با موفقیت لود و آماده‌سازی شد.")
        except ImportError as e:
            logger.error("خطا در بارگذاری کتابخانه pytse_client: %s", e)
            raise RuntimeError("کتابخانه pytse-client نصب نیست یا در دسترس نمی‌باشد.") from e

    def _apply_rate_limit(self) -> None:
        """
        ایجاد وقفه بین درخواست‌های متوالی جهت رعایت سقف مجاز ترافیک سرورهای بورس.
        """
        delay = getattr(settings, 'RATE_LIMIT_DELAY', 1.0)
        if delay > 0:
            time.sleep(delay)

    def get_symbol_history(self, symbol_fa: str, start_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """
        دریافت تاریخچه روزانه معاملات (کندل‌های OHLCV) برای یک نماد مشخص.

        پارامترها:
            symbol_fa: نام فارسی نماد (مانند 'خودرو' یا 'فولاد')
            start_date: فیلتر اختیاری برای تاریخ شروع به فرمت YYYY-MM-DD

        خروجی:
            دیتافریم استاندارد شده شامل ستون‌های:
            ['date', 'open', 'high', 'low', 'close', 'adj_close', 'volume', 'value', 'trade_count']
            در صورت بروز خطا یا عدم وجود داده، مقدار None بازگردانده می‌شود (داده ساختگی تولید نمی‌شود).
        """
        logger.info("در حال دریافت تاریخچه قیمت نماد: %s", symbol_fa)
        self._apply_rate_limit()

        try:
            # دریافت داده‌ها از طریق تابع رسمی download در pytse_client بدون ذخیره مستقیم روی دیسک
            records = self.tse.download(symbols=symbol_fa, write_to_csv=False)

            if not records or symbol_fa not in records:
                logger.warning("هیچ سابقه‌ای برای نماد '%s' از سرور دریافت نشد.", symbol_fa)
                return None

            df = records[symbol_fa]
            if df is None or df.empty:
                logger.warning("دیتافریم سابقه نماد '%s' خالی است.", symbol_fa)
                return None

            # کپی دیتافریم برای اعمال تغییرات و یکسان‌سازی نام ستون‌ها
            df = df.copy()

            # نگاشت نام ستون‌های خروجی pytse_client به ستون‌های استاندارد مدل دیتابیس BRS
            column_rename_map = {
                'adjClose': 'adj_close',
                'count': 'trade_count'
            }
            df = df.rename(columns=column_rename_map)

            # تبدیل تاریخ به رشته استاندار YYYY-MM-DD
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

            # فیلتر کردن براساس تاریخ آغازین در صورت تعیین شدن
            if start_date and 'date' in df.columns:
                df = df[df['date'] >= start_date].reset_index(drop=True)

            # اعتبارسنجی ستون‌های اصلی
            expected_columns = ['date', 'open', 'high', 'low', 'close', 'adj_close', 'volume', 'value', 'trade_count']
            available_columns = [col for col in expected_columns if col in df.columns]
            df = df[available_columns]

            # مرتب‌سازی صعودی بر اساس تاریخ معامله
            if 'date' in df.columns:
                df = df.sort_values(by='date').reset_index(drop=True)

            logger.info("تعداد %d رکورد سابقه برای نماد '%s' دریافت و استانداردسازی شد.", len(df), symbol_fa)
            return df

        except Exception as e:
            logger.error("خطا هنگام دریافت تاریخچه نماد '%s': %s", symbol_fa, e)
            return None

    def get_live_snapshot(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        """
        دریافت اسنپ‌شات لحظه‌ای بازار برای نماد مشخص با استفاده از شیء Ticker.
        شامل وضعیت صفوف عرضه و تقاضا، حجم مبنا، قیمت‌ها و تفکیک حقیقی/حقوقی.

        پارامترها:
            symbol_fa: نام فارسی نماد (مانند 'خودرو')

        خروجی:
            دیکشنری حاوی اطلاعات لحظه‌ای بازار یا None در صورت بروز خطا.
        """
        logger.info("در حال دریافت اسنپ‌شات لحظه‌ای نماد: %s", symbol_fa)
        self._apply_rate_limit()

        try:
            ticker = self.tse.Ticker(symbol=symbol_fa)

            # دریافت پاسخ لحظه‌ای و جزییات صفوف و معامله‌گران
            realtime_info = None
            try:
                realtime_info = ticker.get_ticker_real_time_info_response()
            except Exception as rt_err:
                logger.warning("عدم امکان دریافت RealtimeTickerInfo کامل برای '%s': %s", symbol_fa, rt_err)

            # استخراج حجم صف‌های خرید و فروش
            buy_queue_vol = None
            sell_queue_vol = None
            if realtime_info:
                buy_queue_vol = realtime_info.best_demand_vol
                sell_queue_vol = realtime_info.best_supply_vol
            else:
                buy_queue_vol = getattr(ticker, 'best_demand_vol', None)
                sell_queue_vol = getattr(ticker, 'best_supply_vol', None)

            # استخراج اطلاعات حقیقی و حقوقی
            real_buy_count = None
            real_sell_count = None
            legal_buy_count = None
            legal_sell_count = None
            real_buy_vol = None
            real_sell_vol = None
            legal_buy_vol = None
            legal_sell_vol = None

            if realtime_info and realtime_info.individual_trade_summary:
                ind = realtime_info.individual_trade_summary
                real_buy_count = ind.buy_count
                real_sell_count = ind.sell_count
                real_buy_vol = ind.buy_vol
                real_sell_vol = ind.sell_vol

            if realtime_info and realtime_info.corporate_trade_summary:
                corp = realtime_info.corporate_trade_summary
                legal_buy_count = corp.buy_count
                legal_sell_count = corp.sell_count
                legal_buy_vol = corp.buy_vol
                legal_sell_vol = corp.sell_vol

            # تاریخ/زمان اعلام‌شده از سوی سرور
            provider_ts = None
            if realtime_info and realtime_info.last_date:
                provider_ts = realtime_info.last_date.isoformat()
            elif hasattr(ticker, 'last_date') and ticker.last_date:
                provider_ts = str(ticker.last_date)

            snapshot = {
                'symbol_fa': symbol_fa,
                'last_price': getattr(ticker, 'last_price', None),
                'close_price': getattr(ticker, 'adj_close', None),
                'open_price': getattr(ticker, 'open_price', None),
                'high_price': getattr(ticker, 'high_price', None),
                'low_price': getattr(ticker, 'low_price', None),
                'volume': getattr(ticker, 'volume', None),
                'value': getattr(ticker, 'value', None),
                'trade_count': getattr(ticker, 'count', None),
                'buy_queue_volume': buy_queue_vol,
                'sell_queue_volume': sell_queue_vol,
                'real_buy_count': real_buy_count,
                'real_sell_count': real_sell_count,
                'legal_buy_count': legal_buy_count,
                'legal_sell_count': legal_sell_count,
                'real_buy_volume': real_buy_vol,
                'real_sell_volume': real_sell_vol,
                'legal_buy_volume': legal_buy_vol,
                'legal_sell_volume': legal_sell_vol,
                'price_unit': getattr(settings, 'PRICE_UNIT', 'Rial'),
                'source': 'pytse',
                'provider_timestamp': provider_ts,
            }

            logger.info("اسنپ‌شات لحظه‌ای نماد '%s' با موفقیت دریافت شد (آخرین قیمت: %s)", symbol_fa, snapshot['last_price'])
            return snapshot

        except Exception as e:
            logger.error("خطا در دریافت اسنپ‌شات نماد '%s': %s", symbol_fa, e)
            return None

    def get_symbols_list(self, common_stocks_only: bool = True) -> List[str]:
        """
        دریافت فهرست نمادهای بازار سرمایه با امکان پالایش و محدود کردن به سهام عادی.

        معیارهای فیلتر سهام عادی (Common Stocks):
        1. شناسه بین‌المللی نماد (ISIN) با پیشوند IRO آغاز شود (حذف اوراق مشارکت IRB و حق‌تقدم IRR).
        2. انتهای نام نماد حرف 'ح' نباشد (مربوط به حق تقدم).
        3. نماد صرفاً ارقام عددی یا اوراق مشتقه (ض یا ط) نباشد.
        """
        logger.info("در حال خواندن فهرست نمادهای بازار...")
        try:
            info_dict = self.tse.symbols_data.symbols_information()
            symbols = []

            for sym, data in info_dict.items():
                if not common_stocks_only:
                    symbols.append(sym)
                    continue

                code = data.get('code', '')
                name = data.get('name', '')

                # فیلتر نمادهای سهام عادی
                if not code.startswith('IRO'):
                    continue
                if sym.endswith('ح'):
                    continue
                if sym.startswith(('ض', 'ط')):
                    continue
                if sym.isdigit():
                    continue
                if any(kw in name for kw in ['اوراق', 'اسناد', 'صندوق', 'گواهی']):
                    continue

                symbols.append(sym)

            logger.info("تعداد %d نماد سهام عادی شناسایی شد.", len(symbols))
            return sorted(symbols)

        except Exception as e:
            logger.error("خطا در استخراج لیست نمادها: %s", e)
            return []

    def get_symbol_metadata(self, symbol_fa: str) -> Dict[str, Any]:
        """
        دریافت اطلاعات هویتی و متادیتای نماد (کد TSETMC، کد ملی و بازار) از اطلاعات آفلاین پکیج.
        """
        try:
            info_dict = self.tse.symbols_data.symbols_information()
            raw_meta = info_dict.get(symbol_fa, {})

            tse_code = raw_meta.get('code') or raw_meta.get('index') or symbol_fa
            name_fa = raw_meta.get('name', symbol_fa)

            # تشخیص تقریبی بازار بر اساس نوع کد بین‌المللی
            # کدهای IRO1 عموما بورس و IRO3 / IRO7 فرابورس هستند
            market = 'bourse'
            if str(tse_code).startswith(('IRO3', 'IRO7', 'IRR3', 'IRR7')):
                market = 'farabourse'

            return {
                'tse_code': str(tse_code),
                'symbol_fa': symbol_fa,
                'name_fa': name_fa,
                'market': market,
                'instrument_type': 'stock'
            }
        except Exception as e:
            logger.warning("خطا در بازیابی متادیتای نماد '%s': %s", symbol_fa, e)
            return {
                'tse_code': symbol_fa,
                'symbol_fa': symbol_fa,
                'name_fa': symbol_fa,
                'market': 'bourse',
                'instrument_type': 'stock'
            }

    def test_connection(self, test_symbol: str = 'خودرو') -> bool:
        """
        بررسی وضعیت اتصال به سرورهای بورس و دریافت آزمایشی داده نماد شاخص.
        در صورت موفقیت True و در صورت قطع ارتباط سرور False برمی‌گرداند.
        """
        logger.info("آغاز آزمایش ارتباط با سرورهای بورس برای نماد آزمایشی '%s'...", test_symbol)
        try:
            # ابتدا بررسی متادیتای نماد در سیستم محلی
            meta = self.get_symbol_metadata(test_symbol)
            if not meta or not meta.get('tse_code'):
                logger.warning("اطلاعات اولیه نماد '%s' در دیتابیس محلی یافت نشد.", test_symbol)

            # تلاش برای دریافت یک اسنپ‌شات یا یک رکورد تاریخی
            df = self.get_symbol_history(test_symbol)
            if df is not None and not df.empty:
                logger.info("آزمایش ارتباط با سرورهای بورس با موفقیت انجام شد (دریافت سابقه تاریخچه).")
                return True

            # تلاش ثانویه با دریافت اسنپ‌شات در صورت عدم پاسخ تاریخچه
            snapshot = self.get_live_snapshot(test_symbol)
            if snapshot is not None and snapshot.get('last_price') is not None:
                logger.info("آزمایش ارتباط با سرورهای بورس با موفقیت انجام شد (دریافت اسنپ‌شات لحظه‌ای).")
                return True

            logger.warning("پاسخی از سرورهای بورس برای نماد '%s' دریافت نشد یا سرور در دسترس نیست.", test_symbol)
            return False

        except Exception as e:
            logger.error("خطا در آزمایش اتصال به سرورهای بورس: %s", e)
            return False
