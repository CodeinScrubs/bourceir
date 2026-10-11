# -*- coding: utf-8 -*-
"""
ماژول زمان‌بندی و هماهنگ‌کننده گردآوری داده‌ها (Data Collector & Scheduler)
این ماژول وظیفه اجرای دوره‌ای دریافت داده‌های زنده و تاریخی، ذخیره‌سازی در پایگاه داده SQLite
و بررسی ساعات رسمی معاملات بازار بورس تهران را بر عهده دارد.
"""

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any
import pandas as pd

from config import settings
from database.db import DatabaseManager, get_current_tehran_time
from data_collector.provider import TSEDataProvider

logger = logging.getLogger(__name__)


def is_market_trading_time() -> bool:
    """
    بررسی اینکه آیا در زمان جاری بازار سهام تهران (شنبه تا چهارشنبه، ۹:۰۰ تا ۱۲:۳۰) باز است یا خیر.
    """
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")

    now = datetime.now(tz)
    current_weekday = now.weekday()

    # روزهای کاری بورس تهران (شنبه=5، یکشنبه=6، دوشنبه=0، سه‌شنبه=1، چهارشنبه=2)
    trading_days = getattr(settings, 'TRADING_DAYS', [5, 6, 0, 1, 2])
    if current_weekday not in trading_days:
        return False

    # ساعت شروع و پایان معاملات
    hours_conf = getattr(settings, 'TRADING_HOURS', {'start': '09:00', 'end': '12:30'})
    start_parts = [int(p) for p in hours_conf.get('start', '09:00').split(':')]
    end_parts = [int(p) for p in hours_conf.get('end', '12:30').split(':')]

    start_time = dtime(hour=start_parts[0], minute=start_parts[1])
    end_time = dtime(hour=end_parts[0], minute=end_parts[1])

    current_time = now.time()
    return start_time <= current_time <= end_time


class DataCollector:
    """
    کلاس هماهنگ‌کننده گردآوری داده‌ها و ذخیره‌سازی در پایگاه داده.
    """

    def __init__(self, symbols: Optional[List[str]] = None, db: Optional[DatabaseManager] = None):
        """
        مقداردهی اولیه جمع‌آورنده داده با تعیین لیست نمادها و اتصال به دیتابیس.
        """
        if symbols is None:
            self.symbols = list(settings.TEST_SYMBOLS)
        else:
            self.symbols = list(symbols)

        self.provider = TSEDataProvider()
        self.db = db if db is not None else DatabaseManager()
        self.db.init_db()

        logger.info("جمع‌آورنده داده با %d نماد آغاز به کار کرد: %s", len(self.symbols), self.symbols)

    def _ensure_symbol_registered(self, symbol_fa: str) -> Optional[int]:
        """
        بررسی ثبت بودن نماد در جدول symbols و ثبت اولیه آن در صورت عدم وجود.
        """
        sym_id = self.db.get_symbol_id(symbol_fa)
        if sym_id is not None:
            return sym_id

        # دریافت متادیتا جهت ثبت اولیه
        meta = self.provider.get_symbol_metadata(symbol_fa)
        new_id = self.db.insert_symbol(
            tse_code=meta.get('tse_code', symbol_fa),
            symbol_fa=symbol_fa,
            name_fa=meta.get('name_fa', symbol_fa),
            market=meta.get('market', 'bourse'),
            instrument_type=meta.get('instrument_type', 'stock')
        )
        return new_id

    def collect_history(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        دریافت و ذخیره تاریخچه روزانه نمادها در پایگاه داده.
        از درج رکوردهای تکراری بر اساس شناسه نماد و تاریخ جلوگیری می‌شود.
        """
        target_symbols = symbols if symbols is not None else self.symbols
        started_at = get_current_tehran_time()
        logger.info("آغاز فرآیند گردآوری تاریخچه قیمت برای %d نماد...", len(target_symbols))

        succeeded = 0
        failed = 0
        errors = []
        total_rows_inserted = 0

        for sym in target_symbols:
            try:
                sym_id = self._ensure_symbol_registered(sym)
                if not sym_id:
                    err_msg = f"خطا در ثبت یا یافتن نماد '{sym}' در دیتابیس"
                    logger.error(err_msg)
                    errors.append(err_msg)
                    failed += 1
                    continue

                df = self.provider.get_symbol_history(sym)
                if df is None or df.empty:
                    err_msg = f"عدم دریافت سابقه قیمت برای نماد '{sym}'"
                    logger.warning(err_msg)
                    errors.append(err_msg)
                    failed += 1
                    continue

                inserted_for_sym = 0
                for _, row in df.iterrows():
                    date_val = str(row.get('date'))
                    open_val = float(row['open']) if pd.notna(row.get('open')) else None
                    high_val = float(row['high']) if pd.notna(row.get('high')) else None
                    low_val = float(row['low']) if pd.notna(row.get('low')) else None
                    close_val = float(row['close']) if pd.notna(row.get('close')) else None
                    adj_close_val = float(row['adj_close']) if pd.notna(row.get('adj_close')) else None
                    vol_val = int(row['volume']) if pd.notna(row.get('volume')) else None
                    val_val = float(row['value']) if pd.notna(row.get('value')) else None
                    count_val = int(row['trade_count']) if pd.notna(row.get('trade_count')) else None

                    success = self.db.insert_daily_price(
                        symbol_id=sym_id,
                        date=date_val,
                        open=open_val,
                        high=high_val,
                        low=low_val,
                        close=close_val,
                        adj_close=adj_close_val,
                        volume=vol_val,
                        value=val_val,
                        trade_count=count_val,
                        source='pytse',
                        price_unit=settings.PRICE_UNIT
                    )
                    if success:
                        inserted_for_sym += 1

                total_rows_inserted += inserted_for_sym
                succeeded += 1
                logger.info("نماد '%s': تعداد %d رکورد جدید تاریخچه با موفقیت ذخیره شد.", sym, inserted_for_sym)

            except Exception as e:
                err_msg = f"خطای پیش‌بینی‌نشده در گردآوری تاریخچه '{sym}': {e}"
                logger.error(err_msg)
                errors.append(err_msg)
                failed += 1

        finished_at = get_current_tehran_time()
        status = 'success' if failed == 0 else ('partial' if succeeded > 0 else 'failed')

        self.db.log_collection(
            started_at=started_at,
            finished_at=finished_at,
            collection_type='history',
            symbols_requested=len(target_symbols),
            symbols_succeeded=succeeded,
            symbols_failed=failed,
            errors="; ".join(errors) if errors else None,
            status=status
        )

        report = {
            'collection_type': 'history',
            'started_at': started_at,
            'finished_at': finished_at,
            'symbols_requested': len(target_symbols),
            'symbols_succeeded': succeeded,
            'symbols_failed': failed,
            'rows_inserted': total_rows_inserted,
            'errors': errors,
            'status': status
        }
        logger.info("فرآیند گردآوری تاریخچه به پایان رسید. وضعیت: %s (موفق: %d، ناموفق: %d)", status, succeeded, failed)
        return report

    def collect_snapshots(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        دریافت اسنپ‌شات لحظه‌ای نمادها و ثبت در جدول snapshots پایگاه داده.
        """
        target_symbols = symbols if symbols is not None else self.symbols
        started_at = get_current_tehran_time()
        logger.info("آغاز دریافت اسنپ‌شات لحظه‌ای برای %d نماد...", len(target_symbols))

        succeeded = 0
        failed = 0
        errors = []

        for sym in target_symbols:
            try:
                sym_id = self._ensure_symbol_registered(sym)
                if not sym_id:
                    err_msg = f"خطا در ثبت یا یافتن نماد '{sym}' در دیتابیس"
                    logger.error(err_msg)
                    errors.append(err_msg)
                    failed += 1
                    continue

                snapshot = self.provider.get_live_snapshot(sym)
                if snapshot is None:
                    err_msg = f"عدم دریافت اسنپ‌شات برای نماد '{sym}'"
                    logger.warning(err_msg)
                    errors.append(err_msg)
                    failed += 1
                    continue

                # ثبت اسنپ‌شات در دیتابیس
                inserted = self.db.insert_snapshot(sym_id, snapshot)
                if inserted:
                    succeeded += 1
                    logger.info("اسنپ‌شات لحظه‌ای نماد '%s' در دیتابیس ذخیره شد.", sym)
                else:
                    logger.warning("اسنپ‌شات نماد '%s' قبلاً ثبت شده یا تغییر نکرده است.", sym)
                    succeeded += 1

            except Exception as e:
                err_msg = f"خطا در دریافت یا ثبت اسنپ‌شات نماد '{sym}': {e}"
                logger.error(err_msg)
                errors.append(err_msg)
                failed += 1

        finished_at = get_current_tehran_time()
        status = 'success' if failed == 0 else ('partial' if succeeded > 0 else 'failed')

        self.db.log_collection(
            started_at=started_at,
            finished_at=finished_at,
            collection_type='snapshot',
            symbols_requested=len(target_symbols),
            symbols_succeeded=succeeded,
            symbols_failed=failed,
            errors="; ".join(errors) if errors else None,
            status=status
        )

        report = {
            'collection_type': 'snapshot',
            'started_at': started_at,
            'finished_at': finished_at,
            'symbols_requested': len(target_symbols),
            'symbols_succeeded': succeeded,
            'symbols_failed': failed,
            'errors': errors,
            'status': status
        }
        logger.info("فرآیند اسنپ‌شات لحظه‌ای پایان یافت. وضعیت: %s (موفق: %d، ناموفق: %d)", status, succeeded, failed)
        return report

    def run_collection_cycle(self) -> Dict[str, Any]:
        """
        اجرای یک چرخه پایش: بررسی باز بودن ساعات رسمی بازار بورس و در صورت باز بودن دریافت اسنپ‌شات‌ها.
        """
        is_open = is_market_trading_time()
        current_time = get_current_tehran_time()

        if not is_open:
            logger.info("بازار سهام در زمان جاری (%s) بسته است. چرخه اسنپ‌شات نادیده گرفته شد.", current_time)
            return {
                'status': 'skipped',
                'message': 'بازار در خارج از ساعات معاملاتی قرار دارد (ساعات معاملات: شنبه تا چهارشنبه ۹:۰۰ الی ۱۲:۳۰).',
                'timestamp': current_time,
                'is_market_open': False
            }

        logger.info("بازار سهام فعال است. اجرای دریافت اسنپ‌شات دوره‌ای...")
        report = self.collect_snapshots()
        report['is_market_open'] = True
        return report

    def get_collection_report(self) -> Dict[str, Any]:
        """
        ارائه آمار و آخرین وضعیت لاگ‌های گردآوری داده در سامانه.
        """
        logs = self.db.get_collection_stats()
        total_runs = len(logs)
        success_runs = sum(1 for log in logs if log.get('status') == 'success')
        failed_runs = sum(1 for log in logs if log.get('status') == 'failed')
        partial_runs = sum(1 for log in logs if log.get('status') == 'partial')

        return {
            'total_runs_logged': total_runs,
            'success_runs': success_runs,
            'failed_runs': failed_runs,
            'partial_runs': partial_runs,
            'recent_logs': logs[:5],
            'monitored_symbols': self.symbols
        }
