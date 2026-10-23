# -*- coding: utf-8 -*-
"""
تامین‌کننده داده مستقیم از وب‌سرویس رسمی TSETMC (cdn.tsetmc.com)

چرا مستقیم و نه pytse-client؟
- pytse-client نام ستون‌ها را گمراه‌کننده می‌گذارد (قیمت پایانی رسمی را adjClose نامیده) و قیمت مرجع
  (priceYesterday) را عملاً از دست می‌دادیم؛ اینجا نگاشت هر فیلد صریح و مستند است.
- یک درخواست «دیده‌بان بازار» کل سهام بازار را با ۵ مظنه برمی‌گرداند (مناسب نوسان‌گیری درون‌روزی).
- pytse-client با SDK هوش مصنوعی TypeSafe تعارض وابستگی دارد.

هیچ داده ساختگی تولید نمی‌شود: در هر خطا، خطا لاگ شده و None برگردانده می‌شود.
"""

import hashlib
import logging
import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import settings
from core.daily_publications import publication_request
from core.dividend_evidence import MAX_BYTES, dividend_request
from core.instrument_history import history_request
from core.legacy_quotes import MAX_BYTES as QUOTE_MAX_BYTES
from core.legacy_quotes import quote_request
from core.price_revision import daily_request
from core.text import normalize_fa, normalize_symbol
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time, is_canonical_iso_date, now_tehran, now_tehran_iso
from data_collector.base import CLIENT_TYPE_HISTORY_COLUMNS, DAILY_COLUMNS, BaseDataProvider
from data_collector.validator import DataValidator

logger = logging.getLogger(__name__)

BASE_URL = "https://cdn.tsetmc.com/api"
# paperTypes=1: فقط سهام بورس و فرابورس (حدود ۱۰۰۰ نماد، ~۱ مگابایت)
MARKET_WATCH_STOCKS = (
    f"{BASE_URL}/ClosingPrice/GetMarketWatch?market=0&industrialGroup=&paperTypes%5B0%5D=1"
    "&showTraded=false&withBestLimits=true&hEven=0&RefID=0"
)
# همه انواع اوراق (فقط برای همگام‌سازی جامعه نمادها، چون نوع ابزار yVal را هم برمی‌گرداند)
MARKET_WATCH_ALL = (
    f"{BASE_URL}/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
    + "".join(f"&paperTypes%5B{i}%5D={i + 1}" for i in range(9))
    + "&showTraded=false&withBestLimits=true&hEven=0&RefID=0"
)

FLOW_TO_MARKET = {1: "bourse", 2: "farabourse", 4: "farabourse_base"}


class TsetmcProvider(BaseDataProvider):
    """تامین‌کننده داده بازار سهام از وب‌سرویس JSON سایت TSETMC."""

    name = "tsetmc"
    supports_dated_publications = True
    supports_dated_instrument_states = True

    def close(self) -> None:
        if hasattr(self.session, 'close'):
            self.session.close()

    def get_daily_publication_observation(self, ins_code: str, trade_date: str, kind: str) -> Optional[Dict[str, Any]]:
        code, url = publication_request(ins_code, trade_date, kind)
        observation = self._get_quote_context_json(code, url)
        return (None if observation is None else dict(observation, kind=kind,
                requested_date=trade_date, price_unit='Rial'))

    def get_dated_instrument_state_observation(self, ins_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
        from core.instrument_states import MAX_BYTES as STATE_MAX_BYTES
        from core.instrument_states import state_request

        code, url = state_request(ins_code, trade_date)
        if trade_date > now_tehran().date().isoformat():
            raise ValueError('درخواست وضعیت تاریخی برای روز آینده مجاز نیست')
        observation = self._get_quote_context_json(code, url)
        if observation is None:
            return None
        # The shared transport has its existing larger bound; state observations
        # have a stricter acceptance bound without changing other callers.
        if observation['response_bytes'] > STATE_MAX_BYTES:
            logger.warning('پاسخ وضعیت نماد از حد حجم مجاز گذشت')
            return None
        return dict(observation, requested_date=trade_date, observation_kind='instrument_state')

    def __init__(self, session: Optional[requests.Session] = None, *, realtime=False):
        if type(realtime) is not bool:
            raise ValueError('وضعیت پروفایل زنده باید بولی صریح باشد')
        self.realtime = realtime
        self.timeout = min(settings.REQUEST_TIMEOUT_SECONDS, 5) if realtime else settings.REQUEST_TIMEOUT_SECONDS
        self.session = session if session is not None else self._build_session(realtime=realtime)
        self.request_profile = ('external-session-unknown' if session is not None else
                                'live-no-retry-5s-io' if realtime else 'retrying-default')

    @staticmethod
    def _build_session(*, realtime=False) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=0 if realtime else settings.MAX_RETRIES, backoff_factor=0.0 if realtime else 1.0,
            status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
        )
        session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
        session.headers.update({"User-Agent": "Mozilla/5.0 (BRS Quant Engine)", "Accept": "application/json"})
        return session

    def _get_json(self, url: str, rate_limit: bool = True, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if rate_limit and settings.RATE_LIMIT_DELAY > 0:
            time.sleep(settings.RATE_LIMIT_DELAY)
        try:
            request_timeout = min(timeout or self.timeout, 5) if self.realtime else timeout or self.timeout
            options = {'allow_redirects': False} if self.realtime else {}
            response = self.session.get(url, timeout=request_timeout, **options)
            response.raise_for_status()
            if self.realtime and response.status_code != 200:
                raise ValueError('پاسخ زنده باید کامل و بدون تغییرمسیر باشد')
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('ریشه پاسخ TSETMC باید شیء JSON باشد')
            return payload
        except Exception as e:
            logger.error("خطا در دریافت داده از TSETMC (%s): %s", url, e)
            return None

    # ═══════════════════════════════════════════════
    # نمادها و جامعه سهام
    # ═══════════════════════════════════════════════

    @staticmethod
    def _allowed_instrument_types() -> set:
        allowed = set(settings.UNIVERSE["instrument_types"])
        if settings.UNIVERSE["include_base_market"]:
            allowed.add(309)
        if settings.UNIVERSE["include_sme_market"]:
            allowed.add(313)
        return allowed

    @classmethod
    def is_in_scope(cls, symbol_fa: str, instrument_type_code: Any) -> bool:
        """آیا این ابزار در دامنه پروژه (سهام عادی بورس و فرابورس) است؟"""
        try:
            if int(instrument_type_code) not in cls._allowed_instrument_types():
                return False
        except (TypeError, ValueError):
            return False
        symbol = normalize_symbol(symbol_fa)
        # «فولاد2»، «ونفت3» و ... تابلوهای بلوکی/عمده هستند
        if settings.UNIVERSE["exclude_block_boards"] and symbol[-1:].isdigit():
            return False
        return bool(symbol)

    @staticmethod
    def _meta_from_market_watch(row: Dict[str, Any]) -> Dict[str, Any]:
        flow = row.get("flow")
        return {
            "symbol_fa": normalize_symbol(row.get("lva", "")),
            "name_fa": normalize_fa(row.get("lvc", "")),
            "ins_code": str(row.get("insCode")),
            "tse_code": row.get("insID"),
            "market": FLOW_TO_MARKET.get(flow, f"flow_{flow}") if flow is not None else None,
            "board_code": (row.get("cGrValCot") or "").strip() or None,
            "instrument_type_code": int(row["yVal"]) if row.get("yVal") is not None else None,
            "flow": flow,
            "sector_code": (row.get("csv") or "").strip() or None,
            "shares_outstanding": row.get("ztd"),
            "base_volume": int(row["bv"]) if row.get("bv") is not None else None,
            "instrument_type": "stock",
        }

    @staticmethod
    def _dedupe_by_symbol(universe: List[Dict[str, Any]], activity: Dict[str, float]) -> List[Dict[str, Any]]:
        """اگر دو ابزار فعال نام یکسان داشته باشند، فعال‌ترین (بیشترین ارزش معاملات) نگه داشته می‌شود."""
        chosen: Dict[str, Dict[str, Any]] = {}
        for meta in universe:
            current = chosen.get(meta["symbol_fa"])
            if current is None or activity.get(meta["ins_code"], 0) > activity.get(current["ins_code"], 0):
                if current is not None:
                    logger.warning("نام تکراری «%s» در جامعه سهام؛ ابزار فعال‌تر انتخاب شد.", meta["symbol_fa"])
                chosen[meta["symbol_fa"]] = meta
        return list(chosen.values())

    def get_stock_universe(self, known: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """
        فهرست سهام داخل دامنه پروژه.

        مسیر سریع: دیده‌بان کامل TSETMC کد نوع ابزار (yVal) را برمی‌گرداند → یک درخواست.
        مسیر پشتیبان: TSETMC در برخی ساعات (مثلاً عصر) yVal را در پاسخ نمی‌گذارد. در این حالت فهرست سهام
        از دیده‌بان مخصوص سهام گرفته می‌شود و نوع هر نماد «جدید» از صفحه مشخصات همان نماد خوانده می‌شود؛
        نوع نمادهای شناخته‌شده از known (ردیف‌های دیتابیس به تفکیک ins_code) استفاده می‌شود.
        خروجی خالی یعنی خطا (هرگز فهرست ناقص ساختگی برگردانده نمی‌شود).
        """
        known = known or {}
        # پاسخ کامل همه اوراق حجیم است (~۳ مگابایت) و گاهی تا ۳۰ ثانیه طول می‌کشد
        data = self._get_json(MARKET_WATCH_ALL, rate_limit=False, timeout=max(self.timeout, 120))
        rows = (data or {}).get("marketwatch", [])
        activity = {str(r.get("insCode")): float(r.get("qtc") or 0) for r in rows}
        if rows and any(r.get("yVal") is not None for r in rows):
            universe = []
            for row in rows:
                symbol = normalize_symbol(row.get('lva', ''))
                if settings.UNIVERSE['exclude_block_boards'] and symbol[-1:].isdigit():
                    continue
                meta = self._meta_from_market_watch(row)
                if row.get('yVal') is None:
                    cached = known.get(str(row.get('insCode')))
                    info = cached if cached and cached.get('instrument_type_code') is not None else self.get_instrument_info(str(row.get('insCode')))
                    if not info or info.get('instrument_type_code') is None:
                        logger.error('نوع ابزار %s در پاسخ مختلط نامعلوم است؛ فهرست ناقص منتشر نشد', symbol)
                        return []
                    for key in ('instrument_type_code', 'flow', 'market', 'board_code', 'sector_code', 'sector_name'):
                        if info.get(key) is not None:
                            meta[key] = info[key]
                if self.is_in_scope(meta['symbol_fa'], meta.get('instrument_type_code')):
                    universe.append(meta)
            logger.info("جامعه سهام (مسیر سریع): %d نماد.", len(universe))
            return self._dedupe_by_symbol(universe, activity)

        logger.info("پاسخ دیده‌بان نوع ابزار ندارد؛ مسیر پشتیبان (مشخصات هر نماد) استفاده می‌شود.")
        urls = [MARKET_WATCH_STOCKS]
        if settings.UNIVERSE["include_base_market"]:
            urls.append(MARKET_WATCH_STOCKS.replace("paperTypes%5B0%5D=1", "paperTypes%5B0%5D=2"))
        candidates = []
        for url in urls:
            part = self._get_json(url, rate_limit=False)
            if not part:
                return []
            candidates.extend(part.get("marketwatch", []))
        activity.update({str(r.get("insCode")): float(r.get("qtc") or 0) for r in candidates})

        universe, failed = [], 0
        for row in candidates:
            symbol = normalize_symbol(row.get("lva", ""))
            if settings.UNIVERSE["exclude_block_boards"] and symbol[-1:].isdigit():
                continue
            ins_code = str(row.get("insCode"))
            cached = known.get(ins_code)
            if cached and cached.get("instrument_type_code") is not None:
                meta = self._meta_from_market_watch(row)
                for key in ("instrument_type_code", "flow", "market", "board_code", "sector_code", "sector_name"):
                    if cached.get(key) is not None:
                        meta[key] = cached[key]
            else:
                meta = self.get_instrument_info(ins_code)
                if meta is None or meta.get('instrument_type_code') is None:
                    failed += 1
                    continue
            if self.is_in_scope(meta["symbol_fa"], meta.get("instrument_type_code")):
                universe.append(meta)
        if failed:
            logger.error("مشخصات %d نماد دریافت نشد؛ فهرست ناقص کنار گذاشته شد.", failed)
            return []
        logger.info("جامعه سهام (مسیر پشتیبان): %d نماد.", len(universe))
        return self._dedupe_by_symbol(universe, activity)

    def search_symbol(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        target = normalize_symbol(symbol_fa)
        data = self._get_json(f"{BASE_URL}/Instrument/GetInstrumentSearch/{requests.utils.quote(target)}")
        if not isinstance(data, dict):
            return None
        records = data.get('instrumentSearch')
        if not isinstance(records, list):
            logger.warning('قالب پاسخ جستجوی نماد معتبر نیست')
            return None
        matches = {str(item['insCode']): item for item in records if isinstance(item, dict) and item.get('insCode')
                   and normalize_symbol(item.get('lVal18AFC', '')) == target
                   and item.get('lastDate') not in (0, '0')}
        dated = {}
        for code, item in matches.items():
            raw_date = item.get('lastDate')
            # پاسخ زنده فعلی برای lastDate مقدار ۱ می‌دهد؛ آن را تاریخ فرض نمی‌کنیم.
            if raw_date in (None, 1, '1'):
                if len(matches) > 1:
                    logger.warning('هویت جاری «%s» با نشانگر غیرتاریخی قابل رتبه‌بندی نیست', target)
                    return None
                continue
            if raw_date is not None:
                try:
                    text = str(raw_date)
                    day = datetime.strptime(text, '%Y%m%d').date()
                    if day.strftime('%Y%m%d') != text or day > now_tehran().date():
                        raise ValueError('future date')
                    dated[code] = day
                except (ValueError, TypeError):
                    logger.warning('هویت جاری «%s» به علت تاریخ نامعلوم یا نامعتبر مبهم است', target)
                    return None
        if len(matches) > 1:
            newest = max(dated.values())
            matches = {code: matches[code] for code, day in dated.items() if day == newest}
        if len(matches) == 1:
            code = next(iter(matches))
            info = self.get_instrument_info(code)
            # شکست هویت جدید، مجوز برگشت به شناسه قدیمی یا هویت نامنطبق نیست.
            if info and normalize_symbol(info.get('symbol_fa', '')) == target and str(info.get('ins_code')) == code:
                return info
        logger.warning("نماد «%s» در TSETMC یافت نشد.", symbol_fa)
        return None

    def get_instrument_info(self, ins_code: str) -> Optional[Dict[str, Any]]:
        data = self._get_json(f"{BASE_URL}/Instrument/GetInstrumentInfo/{ins_code}")
        info = (data or {}).get("instrumentInfo")
        if not info:
            return None
        flow = info.get("flow")
        return {
            "symbol_fa": normalize_symbol(info.get("lVal18AFC", "")),
            "name_fa": normalize_fa(info.get("lVal30", "")),
            "ins_code": str(info.get("insCode")),
            "tse_code": info.get("cIsin"),
            "market": FLOW_TO_MARKET.get(flow, f"flow_{flow}"),
            "board_code": (info.get("cgrValCot") or "").strip() or None,
            "instrument_type_code": int(info["yVal"]) if info.get("yVal") else None,
            "flow": flow,
            "sector_code": ((info.get("sector") or {}).get("cSecVal") or "").strip() or None,
            "sector_name": normalize_fa((info.get("sector") or {}).get("lSecVal") or "") or None,
            "shares_outstanding": info.get("zTitad"),
            "base_volume": info.get("baseVol"),
            "instrument_type": "stock",
            "price_limit_up": (info.get("staticThreshold") or {}).get("psGelStaMax"),
            "price_limit_down": (info.get("staticThreshold") or {}).get("psGelStaMin"),
        }

    # ═══════════════════════════════════════════════
    # تاریخچه روزانه
    # ═══════════════════════════════════════════════

    def get_dated_daily_observation(self, ins_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
        code, url = daily_request(ins_code, trade_date)
        payload = self._get_json(url)
        if payload is None:
            return None
        return dict(source=self.name, price_unit='Rial', ins_code=code, requested_date=trade_date,
                    url=url, received_at=now_tehran_iso(), payload=payload)

    def get_symbol_history(self, ins_code: str, last_n: int = 0) -> Optional[pd.DataFrame]:
        data = self._get_json(f"{BASE_URL}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{int(last_n)}")
        rows = (data or {}).get("closingPriceDaily")
        if rows is None:
            return None
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        numeric_columns = {
            "open_raw": "priceFirst", "high_raw": "priceMax", "low_raw": "priceMin",
            "close_raw": "pDrCotVal", "close_final_raw": "pClosing", "ref_price_raw": "priceYesterday",
            "volume": "qTotTran5J", "value": "qTotCap", "trade_count": "zTotTran",
        }
        # Check before astype: a boolean would otherwise become a plausible 0/1 value.
        # Only numeric fields are constrained; source status flags are legitimately boolean.
        for column in numeric_columns.values():
            if df[column].map(lambda value: isinstance(value, (bool, np.bool_))).any():
                logger.error("نماد %s: مقدار بولی در ستون عددی %s؛ پاسخ تاریخچه رد شد.", ins_code, column)
                return None
        df = pd.DataFrame({
            "date": df["dEven"].map(deven_to_iso),
            **{target: df[source].astype(float) for target, source in numeric_columns.items()},
        })
        # روزهای بدون معامله (توقف نماد) کندل ندارند
        df = df[df["volume"] > 0].sort_values("date").reset_index(drop=True)
        # کندل امروز قبل از ساعت ۱۳ (پیش از محاسبه قیمت پایانی رسمی) و هر تاریخ آینده پذیرفته نمی‌شود
        now = now_tehran()
        today = now.date().isoformat()
        df = df[(df["date"] < today) | ((df["date"] == today) & (now.hour >= 13))].reset_index(drop=True)
        df, removed = DataValidator.validate_daily_dataframe(df)
        if removed:
            logger.warning("نماد %s: %d رکورد نامعتبر حذف شد.", ins_code, removed)
        return df[DAILY_COLUMNS]

    def get_dated_history_observation(self, ins_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
        code, _ = daily_request(ins_code, trade_date)
        url = f'{BASE_URL}/ClosingPrice/GetClosingPriceDailyList/{code}/0'
        payload = self._get_json(url)
        if payload is None:
            return None
        rows = payload.get('closingPriceDaily') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            logger.error('پاسخ فهرست تاریخچه %s ساختار معتبر ندارد', code)
            return None
        selected = [row for row in rows if isinstance(row, dict)
                    and type(row.get('dEven')) in (str, int) and str(row['dEven']) == trade_date.replace('-', '')]
        return dict(source=self.name, price_unit='Rial', ins_code=code, requested_date=trade_date,
                    url=url, received_at=now_tehran_iso(), received_row_count=len(rows),
                    payload=dict(closingPriceDaily=selected), response_scope='selected_native_rows',
                    unselected_numeric_rows_not_archived=True)

    def get_client_types_history(self, ins_code: str, dates: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
        """
        تاریخچه حقیقی/حقوقی. بدون dates کل تاریخچه (~۱ مگابایت)؛ با dates فقط همان روزها (به‌روزرسانی سبک روزانه).
        """
        if dates is not None:
            rows, failed = [], 0
            for day in dates:
                data = self._get_json(f"{BASE_URL}/ClientType/GetClientTypeHistory/{ins_code}/{day.replace('-', '')}")
                if data is None:
                    failed += 1   # یک روز خراب نباید آمار بقیه روزها را از بین ببرد؛ اجرای بعدی دوباره امتحان می‌شود
                    continue
                if (data.get("clientType") or {}).get("recDate") and deven_to_iso(data['clientType']['recDate']) == day:
                    rows.append(data["clientType"])
                else:
                    failed += 1
            if dates and failed == len(dates):
                return None   # هیچ درخواستی موفق نشد (احتمالاً قطعی شبکه)
        else:
            data = self._get_json(f"{BASE_URL}/ClientType/GetClientTypeHistory/{ins_code}")
            rows = (data or {}).get("clientType")
            if rows is None:
                return None
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=CLIENT_TYPE_HISTORY_COLUMNS)
        mapping = {
            "buy_I_Volume": "buy_i_volume", "buy_N_Volume": "buy_n_volume",
            "buy_I_Value": "buy_i_value", "buy_N_Value": "buy_n_value",
            "buy_I_Count": "buy_i_count", "buy_N_Count": "buy_n_count",
            "sell_I_Volume": "sell_i_volume", "sell_N_Volume": "sell_n_volume",
            "sell_I_Value": "sell_i_value", "sell_N_Value": "sell_n_value",
            "sell_I_Count": "sell_i_count", "sell_N_Count": "sell_n_count",
        }
        out = df.rename(columns=mapping)
        out["date"] = df["recDate"].map(deven_to_iso)
        return out.reindex(columns=CLIENT_TYPE_HISTORY_COLUMNS).sort_values("date").reset_index(drop=True)

    # ═══════════════════════════════════════════════
    # داده لحظه‌ای کل بازار
    # ═══════════════════════════════════════════════

    def get_client_types_today(self) -> Dict[str, Dict[str, Any]]:
        """حقیقی/حقوقی امروز برای کل بازار در یک درخواست: {ins_code: {...}}"""
        data = self._get_json(f"{BASE_URL}/ClientType/GetClientTypeAll", rate_limit=False)
        rows = data.get('clientTypeAllDto') if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get('insCode') for row in rows):
            logger.error('ساختار آمار حقیقی/حقوقی دیده‌بان نامعتبر است')
            return {}
        result, duplicates = {}, set()
        for row in rows:
            key = str(row['insCode'])
            if key in result:
                duplicates.add(key)
            result[key] = row
        for key in duplicates:
            result.pop(key)
        if duplicates:
            logger.error('آمار حقیقی/حقوقی تکراری برای %s شناسه؛ مؤلفه مبهم مصرف نشد', len(duplicates))
        return result

    @staticmethod
    def _provider_timestamp(heven: Any, deven: Any = None) -> Optional[str]:
        """
        زمان آخرین معامله/به‌روزرسانی نماد از تاریخ (dEven) و ساعت (hEven) خود سرور.
        بدون تاریخ سرور، زمان حدس زده نمی‌شود (None = تازگی نامعلوم): حدس «امروز یا دیروز» بعد از تعطیلی
        چندروزه، داده کهنه را تازه نشان می‌داد.
        """
        if not heven or not deven:
            return None
        try:
            stamp = datetime.fromisoformat(f"{deven_to_iso(deven)}T{heven_to_time(heven)}")
        except (TypeError, ValueError, OverflowError):
            return None
        return stamp.replace(tzinfo=TEHRAN_TZ).isoformat()

    @classmethod
    def snapshot_from_market_watch(cls, row: Dict[str, Any], client_type: Optional[Dict[str, Any]],
                                   captured_at: str) -> Dict[str, Any]:
        """تبدیل یک ردیف دیده‌بان بازار + حقیقی/حقوقی به اسنپ‌شات استاندارد BRS."""
        snap: Dict[str, Any] = {
            "ins_code": str(row.get("insCode")),
            "symbol_fa": normalize_symbol(row.get("lva", "")),
            "captured_at": captured_at,
            "provider_timestamp": cls._provider_timestamp(row.get("hEven"), row.get("dEven")),
            "last_trade_time": None,
            "last_price": row.get("pdv"),
            "close_price": row.get("pcl"),
            "open_price": row.get("pf"),
            "high_price": row.get("pmx"),
            "low_price": row.get("pmn"),
            "ref_price": row.get("py"),
            "price_limit_up": row.get("pMax"),
            "price_limit_down": row.get("pMin"),
            "base_volume": row.get("bv"),
            "volume": row.get("qtj"),
            "value": row.get("qtc"),
            "trade_count": row.get("ztt"),
            "source": "tsetmc",
            "price_unit": settings.PRICE_UNIT,
        }
        try:
            time_text = heven_to_time(row['hEven'])
            datetime.strptime(time_text, '%H:%M:%S')
            snap['last_trade_time'] = time_text
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        levels = row.get('blDs')
        seen_levels, duplicate_levels = set(), set()
        for level in levels if isinstance(levels, list) else []:
            if not isinstance(level, dict):
                continue
            n = level.get("n")
            if type(n) is not int or not 1 <= n <= 5:
                continue
            if n in seen_levels:
                duplicate_levels.add(n)
            seen_levels.add(n)
            snap[f"bid_price_{n}"] = level.get("pmd")
            snap[f"bid_vol_{n}"] = level.get("qmd")
            snap[f"bid_count_{n}"] = level.get("zmd")
            snap[f"ask_price_{n}"] = level.get("pmo")
            snap[f"ask_vol_{n}"] = level.get("qmo")
            snap[f"ask_count_{n}"] = level.get("zmo")
        for n in duplicate_levels:
            for side in ('bid', 'ask'):
                for field in ('price', 'vol', 'count'):
                    snap[f'{side}_{field}_{n}'] = None
        # صف خرید/فروش فقط وقتی معنا دارد که بهترین مظنه روی سقف/کف دامنه نوسان باشد
        limit_up, limit_down = snap["price_limit_up"], snap["price_limit_down"]
        # بدون دامنه مجاز روز، وجود صف نامعلوم است (None)، نه صفر
        snap["buy_queue_volume"] = (None if not limit_up or snap.get("bid_price_1") is None or snap.get("bid_vol_1") is None else
                                    snap.get("bid_vol_1") if snap.get("bid_price_1") == limit_up else 0)
        snap["sell_queue_volume"] = (None if not limit_down or snap.get("ask_price_1") is None or snap.get("ask_vol_1") is None else
                                     snap.get("ask_vol_1") if snap.get("ask_price_1") == limit_down else 0)
        if client_type:
            snap.update({
                "real_buy_volume": client_type.get("buy_I_Volume"),
                "real_sell_volume": client_type.get("sell_I_Volume"),
                "legal_buy_volume": client_type.get("buy_N_Volume"),
                "legal_sell_volume": client_type.get("sell_N_Volume"),
                "real_buy_count": client_type.get("buy_CountI"),
                "real_sell_count": client_type.get("sell_CountI"),
                "legal_buy_count": client_type.get("buy_CountN"),
                "legal_sell_count": client_type.get("sell_CountN"),
            })
        return snap

    def get_market_date(self, ins_code: str) -> Optional[str]:
        """تاریخ آخرین روز معاملاتی که داده دیده‌بان به آن تعلق دارد (از روی یک نماد معامله‌شده)."""
        data = self._get_json(f"{BASE_URL}/ClosingPrice/GetClosingPriceInfo/{ins_code}")
        info = data.get('closingPriceInfo') if isinstance(data, dict) else None
        try:
            final_date = deven_to_iso(info.get('finalLastDate')) if isinstance(info, dict) else None
        except (TypeError, ValueError, OverflowError):
            return None
        return final_date if is_canonical_iso_date(final_date) else None

    def get_instrument_history_observation(self, ins_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
        code, url = history_request(ins_code, trade_date)
        if trade_date > now_tehran_iso()[:10]:
            raise ValueError('درخواست تاریخ آینده مجاز نیست')
        payload = self._get_json(url)
        if payload is None:
            return None
        return dict(source=self.name, url=url, ins_code=code, requested_date=trade_date,
                    received_at=now_tehran_iso(), payload=payload)

    def get_dividend_observation(self, ins_code: str, symbol_fa: str) -> Optional[Dict[str, Any]]:
        code, symbol, url = dividend_request(ins_code, symbol_fa)
        requested = now_tehran_iso()
        started = time.monotonic()
        try:
            if settings.RATE_LIMIT_DELAY > 0:
                time.sleep(settings.RATE_LIMIT_DELAY)
            response = self.session.get(url, timeout=(min(self.timeout, 10), min(self.timeout, 10)),
                stream=True, allow_redirects=False, headers={'Accept': 'text/plain'})
            try:
                response.raise_for_status()
                if response.status_code != 200 or response.url != url:
                    raise ValueError('پاسخ جزئی یا تغییرمسیر، منبع کامل نیست')
                length = response.headers.get('Content-Length', '')
                if length.isdigit() and int(length) > MAX_BYTES:
                    raise ValueError('پاسخ منبع سود نقدی از حد حجم گذشت')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() - started > 60:
                        raise ValueError('پاسخ منبع سود نقدی از حد حجم یا زمان گذشت')
                    chunks.append(chunk)
                content = b''.join(chunks)
                # صفحه تأیید/ورود، بدنه قابل بایگانی این ابزار عمومی نیست.
                if b'<' in content or b'>' in content or b'\x00' in content:
                    raise ValueError('صفحه HTML یا کپچا به‌جای جدول سود نقدی')
                content.decode('utf-8-sig')
                return dict(source=self.name, ins_code=code, symbol=symbol, url=url,
                    requested_at=requested, received_at=now_tehran_iso(), content=content,
                    response_bytes=len(content), body_sha256=hashlib.sha256(content).hexdigest())
            finally:
                response.close()
        except (requests.RequestException, ValueError, UnicodeError) as error:
            logger.warning('دریافت منبع عمومی سود نقدی ناموفق بود: %s', type(error).__name__)
            return None

    def get_session_candles(self) -> Optional[Dict[str, Any]]:
        """
        کندل روز جاری همه سهام از دیده‌بان بازار (بلافاصله بعد از پایان معاملات در دسترس است؛
        در حالی که سرویس تاریخچه روزانه، کندل امروز را تا عصر به‌تدریج منتشر می‌کند).
        خروجی: {'date': 'YYYY-MM-DD', 'candles': [{'ins_code', ستون‌های DAILY_COLUMNS}, ...]}
        """
        data = self._get_json(MARKET_WATCH_STOCKS, rate_limit=False)
        rows = data.get('marketwatch') if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None
        rows = [row for row in rows if type(row.get('qtj')) in (int, float)
                and math.isfinite(row['qtj']) and row['qtj'] > 0]
        if not rows:
            return None
        dated = []
        for row in rows:
            try:
                trade_date = deven_to_iso(row["dEven"])
                datetime.fromisoformat(trade_date)
            except (KeyError, TypeError, ValueError):
                continue
            if trade_date <= now_tehran().date().isoformat():
                dated.append((trade_date, row))
        if not dated:
            logger.warning("تاریخ خود نمادها موجود نیست؛ کندل جلسه از دیده‌بان ساخته نشد.")
            return None
        most_traded = max(dated, key=lambda pair: (pair[0], pair[1].get("qtc") or 0))[1]
        market_date = self.get_market_date(str(most_traded["insCode"]))
        if not market_date:
            return None
        rows = [row for trade_date, row in dated if trade_date == market_date]
        if not rows:
            return None
        candles = [{
            "ins_code": str(r["insCode"]), "date": market_date,
            "open_raw": r.get("pf"), "high_raw": r.get("pmx"), "low_raw": r.get("pmn"),
            "close_raw": r.get("pdv"), "close_final_raw": r.get("pcl"), "ref_price_raw": r.get("py"),
            "volume": r.get("qtj"), "value": r.get("qtc"), "trade_count": r.get("ztt"),
        } for r in rows]
        return {"date": market_date, "candles": candles}

    def get_market_snapshots(self) -> Optional[List[Dict[str, Any]]]:
        data = self._get_json(MARKET_WATCH_STOCKS, rate_limit=False)
        rows = data.get('marketwatch') if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            logger.error('ساختار دیده‌بان بازار نامعتبر است')
            return None
        client_types = self.get_client_types_today()
        captured_at = now_tehran_iso()  # زمانی که هر دو پاسخ واقعاً در دست ماست (نه قبل از درخواست)
        return [
            self.snapshot_from_market_watch(row, client_types.get(str(row.get("insCode"))), captured_at)
            for row in rows
        ]

    def get_legacy_quote_observation(self, ins_code: str) -> Optional[Dict[str, Any]]:
        """بایت محدود از پاسخ یکپارچه؛ بدون دنبال‌کردن تغییرمسیر یا ساختن تاریخ."""
        code, url = quote_request(ins_code)
        requested, started = now_tehran_iso(), time.monotonic()
        try:
            if settings.RATE_LIMIT_DELAY > 0:
                time.sleep(settings.RATE_LIMIT_DELAY)
            response = self.session.get(url, timeout=(min(self.timeout, 10), min(self.timeout, 10)),
                                        stream=True, allow_redirects=False, headers={'Accept': 'text/plain'})
            try:
                response.raise_for_status()
                if response.status_code != 200 or response.url != url:
                    raise ValueError('پاسخ جزئی یا تغییرمسیر منبع معتبر نیست')
                length = response.headers.get('Content-Length', '')
                if length.isdigit() and int(length) > QUOTE_MAX_BYTES:
                    raise ValueError('پاسخ مظنه از حد حجم گذشت')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > QUOTE_MAX_BYTES or time.monotonic() - started > 30:
                        raise ValueError('پاسخ مظنه از حد حجم یا زمان گذشت')
                    chunks.append(chunk)
                content = b''.join(chunks)
                decoded = content.decode('utf-8-sig')
                if not content or decoded.lstrip().startswith('<') or b'\x00' in content:
                    raise ValueError('پاسخ خالی یا HTML به جای مظنه')
                return dict(source=self.name, ins_code=code, url=url, requested_at=requested,
                            received_at=now_tehran_iso(), content=content, response_bytes=len(content),
                            body_sha256=hashlib.sha256(content).hexdigest())
            finally:
                response.close()
        except (requests.RequestException, ValueError, UnicodeError) as error:
            logger.warning('دریافت مظنه یکپارچه ناموفق بود: %s', type(error).__name__)
            return None

    def _get_quote_context_json(self, code: str, url: str) -> Optional[Dict[str, Any]]:
        """بایت محدود JSON بومی؛ فراخوان‌های داخلی نشانی ثابت می‌سازند و نشست صادر نمی‌شود."""
        from core.quote_context import MAX_JSON_BYTES

        requested, started = now_tehran_iso(), time.monotonic()
        try:
            if settings.RATE_LIMIT_DELAY > 0:
                time.sleep(settings.RATE_LIMIT_DELAY)
            response = self.session.get(url, timeout=(min(self.timeout, 10), min(self.timeout, 10)),
                stream=True, allow_redirects=False, headers={'Accept': 'application/json'})
            try:
                response.raise_for_status()
                if response.status_code != 200 or response.url != url:
                    raise ValueError('پاسخ جزئی یا تغییرمسیر مشخصات/دامنه مجاز نیست')
                length = response.headers.get('Content-Length', '')
                if length.isdigit() and int(length) > MAX_JSON_BYTES:
                    raise ValueError('پاسخ مشخصات/دامنه از حد حجم گذشت')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_JSON_BYTES or time.monotonic()-started > 15:
                        raise ValueError('پاسخ مشخصات/دامنه از حد حجم یا زمان گذشت')
                    chunks.append(chunk)
                content = b''.join(chunks)
                if not content or content.lstrip().startswith(b'<') or b'\x00' in content:
                    raise ValueError('پاسخ خالی یا HTML به جای مشخصات/دامنه')
                return dict(source=self.name, ins_code=code, url=url, requested_at=requested,
                    received_at=now_tehran_iso(), response_bytes=len(content), content=content,
                    body_sha256=hashlib.sha256(content).hexdigest())
            finally:
                response.close()
        except (requests.RequestException, ValueError) as exc:
            logger.warning('دریافت مشخصات/دامنه مجاز ناموفق بود: %s', type(exc).__name__)
            return None

    def get_share_change_observation(self, ins_code: str) -> Optional[Dict[str, Any]]:
        from core.share_changes import share_change_request

        code, url = share_change_request(ins_code)
        return self._get_quote_context_json(code, url)

    def get_quote_context_observation(self, ins_code: str) -> Optional[Dict[str, Any]]:
        from core.legacy_quotes import parse_quote
        from core.quote_context import VERSION, context_requests

        code, _ = quote_request(ins_code)
        quote = self.get_legacy_quote_observation(code)
        if quote is None:
            return None
        bundle = dict(version=VERSION, ins_code=code, requested_at=quote['requested_at'],
                      received_at=now_tehran_iso(), quote=quote, instrument=None, limits=None)
        try:
            day = parse_quote(quote, 'دامنه')['provider_timestamp'][:10]
        except (ValueError, TypeError, KeyError):
            # بایتِ مظنه ردشده محفوظ است؛ ساعت یا تاریخ برای عبور حدس زده نمی‌شود.
            return bundle
        info_url, limits_url = context_requests(code, day)
        bundle['instrument'] = self._get_quote_context_json(code, info_url)
        bundle['limits'] = self._get_quote_context_json(code, limits_url)
        bundle['received_at'] = now_tehran_iso()
        return bundle

    def get_market_observation(self) -> Optional[Dict[str, Any]]:
        # مسیر مخصوص سهام گاهی yVal/flow ندارد؛ نوع ابزار از نام یا ISIN حدس زده نمی‌شود.
        data = self._get_json(MARKET_WATCH_ALL, rate_limit=False, timeout=max(self.timeout, 120))
        rows = data.get('marketwatch') if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            logger.error('ساختار دیده‌بان برای بررسی بازار نامعتبر است')
            return None
        received = now_tehran_iso()
        instruments = []
        for row in rows:
            try:
                if isinstance(row.get('yVal'), bool) or not float(row.get('yVal')).is_integer():
                    raise ValueError('نوع ابزار صحیح نیست')
                metadata = self._meta_from_market_watch(row)
            except (TypeError, ValueError, OverflowError, AttributeError):
                metadata = dict(ins_code=str(row.get('insCode')), symbol_fa=normalize_symbol(row.get('lva', '')),
                                instrument_type_code=None)
            instruments.append(dict(metadata=metadata, snapshot=self.snapshot_from_market_watch(row, None, received)))
        return dict(source=self.name, url=MARKET_WATCH_ALL, received_at=received,
                    instruments=instruments, payload=data)

    def get_closing_observation(self, ins_code: str) -> Optional[Dict[str, Any]]:
        if not str(ins_code).isdigit():
            raise ValueError('شناسه ابزار باید عددی باشد')
        url = f'{BASE_URL}/ClosingPrice/GetClosingPriceInfo/{ins_code}'
        data = self._get_json(url)
        info = data.get('closingPriceInfo') if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return None
        dates = []
        for key in ('dEven', 'finalLastDate'):
            try:
                value = deven_to_iso(info.get(key))
            except (TypeError, ValueError, OverflowError):
                value = None
            dates.append(value if is_canonical_iso_date(value) else None)
        # تاریخ آخرین روز به جای تاریخ خود کندل گذاشته نمی‌شود؛ تعارض صریح است.
        trade_date = dates[0] if dates[0] and dates[0] == dates[1] else None
        candle = dict(date=trade_date, open_raw=info.get('priceFirst'), high_raw=info.get('priceMax'),
                      low_raw=info.get('priceMin'), close_raw=info.get('pDrCotVal'),
                      close_final_raw=info.get('pClosing'), ref_price_raw=info.get('priceYesterday'),
                      volume=info.get('qTotTran5J'), value=info.get('qTotCap'), trade_count=info.get('zTotTran'))
        return dict(source=self.name, url=url, ins_code=str(ins_code), received_at=now_tehran_iso(),
                    date_status='matched' if trade_date else 'missing_or_conflicting', candle=candle,
                    provider_timestamp=self._provider_timestamp(info.get('hEven'), info.get('dEven')),
                    identity_attestation=('request_url_only' if info.get('insCode') in (None, 0, '0')
                                          else 'response_matches_request' if str(info.get('insCode')) == str(ins_code)
                                          else 'conflicting_response'), payload=data)

    def get_market_overview(self, flow: int) -> Optional[Dict[str, Any]]:
        if type(flow) is not int or flow not in (1, 2):
            raise ValueError('بازار باید بورس یا فرابورس باشد')
        url = f'{BASE_URL}/MarketData/GetMarketOverview/{flow}'
        data = self._get_json(url, rate_limit=False)
        info = data.get('marketOverview') if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return None
        return dict(source=self.name, url=url, received_at=now_tehran_iso(), flow=flow, payload=data,
                    provider_timestamp=self._provider_timestamp(info.get('marketActivityHEven'), info.get('marketActivityDEven')),
                    index_value=info.get('indexLastValue'), index_change=info.get('indexChange'),
                    equal_weight_value=info.get('indexEqualWeightedLastValue'),
                    equal_weight_change=info.get('indexEqualWeightedChange'),
                    market_state=info.get('marketStateTitle'))

    def get_sector_names(self) -> Optional[Dict[str, str]]:
        data = self._get_json(f'{BASE_URL}/MarketData/GetSectorsSummary', rate_limit=False)
        rows = data.get('sectorSummeries') if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None
        return {str(row['cSecVal']).strip(): normalize_fa(row['lSecVal']) for row in rows
                if row.get('cSecVal') and isinstance(row.get('lSecVal'), str)}

    def get_intraday_trades(self, ins_code: str) -> Optional[pd.DataFrame]:
        data = self._get_json(f"{BASE_URL}/Trade/GetTrade/{ins_code}")
        rows = (data or {}).get("trade")
        if rows is None:
            return None
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df = df[df["canceled"] == 0]
        return pd.DataFrame({
            "trade_number": df["nTran"].astype(int),
            "trade_time": df["hEven"].map(heven_to_time),
            "price": df["pTran"].astype(float),
            "volume": df["qTitTran"].astype(int),
        }).sort_values("trade_number").reset_index(drop=True)

    def test_connection(self) -> bool:
        data = self._get_json(MARKET_WATCH_STOCKS, rate_limit=False)
        return bool(data and data.get("marketwatch"))

    def get_trade_session(self, ins_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
        from datetime import date

        requested = date.fromisoformat(trade_date)
        if requested.isoformat() != trade_date or not str(ins_code).isdigit() or requested > now_tehran().date():
            raise ValueError('شناسه و تاریخ معتبر گذشته لازم است')
        url = f'{BASE_URL}/Trade/GetTradeHistory/{ins_code}/{requested:%Y%m%d}/false'
        data = self._get_json(url)
        if not isinstance(data, dict) or not isinstance(data.get('tradeHistory'), list):
            return None
        return dict(source='tsetmc', price_unit='Rial', ins_code=str(ins_code), requested_date=trade_date,
                    captured_at=now_tehran().isoformat(), source_url=url, response=data,
                    endpoint_is_grouped=False, completeness_verified=False)
