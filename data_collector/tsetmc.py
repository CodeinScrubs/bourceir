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
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

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
from data_collector.base import CLIENT_TYPE_HISTORY_COLUMNS, BaseDataProvider
from data_collector.daily_history import normalize_history_payload
from data_collector.source_cooldown import SourceGate

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
    supports_history_observations = True

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

    def get_historical_market_messages(self, ins_code: str, start: str, end_exclusive: str) -> Optional[Dict[str, Any]]:
        """Project one bounded historical window; never return full mixed-period bytes."""
        from core.market_messages import MAX_BYTES, message_request, project_messages

        url = message_request(ins_code, start, end_exclusive)
        observation = self._get_quote_context_json(ins_code, url, max_bytes=MAX_BYTES)
        if observation is None:
            return None
        try:
            data = project_messages(observation['content'], ins_code, start, end_exclusive,
                                    received_at=observation['received_at'])
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning('Malformed market message response: %s', type(exc).__name__)
            return None
        receipt = {key: value for key, value in observation.items() if key != 'content'}
        return dict(receipt=receipt, projection=data)

    def get_development_index_history(self, index: str, start: str, end_exclusive: str) -> Optional[Dict[str, Any]]:
        from core.market_index import MAX_BYTES, index_history_request, project_index_history

        code, url = index_history_request(index, start, end_exclusive)
        observation = self._get_quote_context_json(code, url, max_bytes=MAX_BYTES)
        if observation is None:
            return None
        try:
            projection = project_index_history(observation['content'], index, start, end_exclusive,
                                               received_at=observation['received_at'])
        except ValueError as exc:
            logger.warning('Malformed index history: %s', exc)
            return None
        receipt = {key: value for key, value in observation.items() if key != 'content'}
        return dict(receipt=receipt, projection=projection)

    def __init__(self, session: Optional[requests.Session] = None, *, realtime=False):
        if type(realtime) is not bool:
            raise ValueError('وضعیت پروفایل زنده باید بولی صریح باشد')
        self.realtime = realtime
        self.timeout = min(settings.REQUEST_TIMEOUT_SECONDS, 5) if realtime else settings.REQUEST_TIMEOUT_SECONDS
        self.session = session if session is not None else self._build_session(realtime=realtime)
        self.source_gate = SourceGate()
        self.last_transport_evidence = None
        self.request_profile = ('external-session-unknown' if session is not None else
                                'live-no-retry-5s-io' if realtime else 'retrying-default')

    @staticmethod
    def _build_session(*, realtime=False) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=0 if realtime else settings.MAX_RETRIES, backoff_factor=0.0 if realtime else 1.0,
            status=0, redirect=0, other=0, status_forcelist=(), allowed_methods=("GET",),
            raise_on_status=False, respect_retry_after_header=False,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
        session.headers.update({"User-Agent": "Mozilla/5.0 (BRS Quant Engine)", "Accept": "application/json"})
        return session

    def _send(self, url, **options):
        if options.get('allow_redirects') not in (None,False):
            raise ValueError('Public source requests cannot follow redirects')
        options['allow_redirects'] = False
        try:
            return self.source_gate.get(self.session, url, **options)
        finally:
            self.last_transport_evidence = self.source_gate.last_evidence

    def _get_json(self, url: str, rate_limit: bool = True, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if rate_limit and settings.RATE_LIMIT_DELAY > 0:
            time.sleep(settings.RATE_LIMIT_DELAY)
        try:
            request_timeout = min(timeout or self.timeout, 5) if self.realtime else timeout or self.timeout
            options = {'allow_redirects': False} if self.realtime else {}
            response = self._send(url, timeout=request_timeout, **options)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('پاسخ عمومی باید کامل و بدون تغییرمسیر باشد')
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

    def get_research_rights_search_observation(self, parent_code: str,
                                               parent_symbol: str) -> Optional[Dict[str, Any]]:
        """Public bounded search for research; never adds rights to the operational universe."""
        from core.rights_discovery import MAX_BYTES, VERSION, search_request

        request = search_request(parent_code, parent_symbol)
        observed = self._get_quote_context_json(request['parent_code'], request['url'],
                                                max_bytes=MAX_BYTES, validate_content=False)
        if observed is None:
            return None
        return dict(observed, version=VERSION, parent_symbol=request['parent_symbol'],
                    query_symbol=request['query_symbol'])

    def get_instrument_info(self, ins_code: str) -> Optional[Dict[str, Any]]:
        if type(ins_code) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,29}', str(ins_code)):
            return None
        code = str(ins_code)
        data = self._get_json(f"{BASE_URL}/Instrument/GetInstrumentInfo/{code}")
        info = (data or {}).get("instrumentInfo")
        if not isinstance(info, dict) or type(info.get('insCode')) not in (str, int) or str(info['insCode']) != code:
            return None
        # cIsin and instrumentID can identify different entities in the same response.
        instrument_id = info.get('instrumentID')
        if not isinstance(instrument_id, str) or not re.fullmatch(r'IR[A-Z0-9]{10}', instrument_id):
            return None
        flow = info.get("flow")
        instrument_type = info.get('yVal')
        sector = info.get('sector')
        limits = info.get('staticThreshold')
        board = info.get('cgrValCot')
        if ((flow is not None and type(flow) is not int)
                or (instrument_type is not None and (type(instrument_type) not in (int, str)
                    or not re.fullmatch(r'[0-9]{1,3}', str(instrument_type))))
                or (sector is not None and not isinstance(sector, dict))
                or (limits is not None and not isinstance(limits, dict))):
            return None
        sector_code = (sector or {}).get('cSecVal')
        return {
            "symbol_fa": normalize_symbol(info.get("lVal18AFC", "")),
            "name_fa": normalize_fa(info.get("lVal30", "")),
            "ins_code": code,
            "tse_code": instrument_id,
            "market": FLOW_TO_MARKET.get(flow, f"flow_{flow}"),
            "board_code": (board.strip() or None) if isinstance(board, str) else None,
            "instrument_type_code": int(instrument_type) if instrument_type is not None else None,
            "flow": flow,
            "sector_code": (sector_code.strip() or None) if isinstance(sector_code, str) else None,
            "sector_name": normalize_fa((sector or {}).get("lSecVal")) or None,
            "shares_outstanding": info.get("zTitad"),
            "base_volume": info.get("baseVol"),
            "instrument_type": "stock",
            "price_limit_up": (limits or {}).get("psGelStaMax"),
            "price_limit_down": (limits or {}).get("psGelStaMin"),
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

    def get_market_day_observation(self, trade_date: str) -> Optional[Dict[str, Any]]:
        from core.market_day import MAX_BYTES, market_day_request

        observation = self._get_quote_context_json('', market_day_request(trade_date), max_bytes=MAX_BYTES)
        return dict(observation, requested_date=trade_date) if observation is not None else None

    def get_symbol_history(self, ins_code: str, last_n: int = 0) -> Optional[pd.DataFrame]:
        data = self._get_json(f"{BASE_URL}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{int(last_n)}")
        return normalize_history_payload(data, ins_code=ins_code, as_of=now_tehran())

    def get_symbol_history_observation(self, ins_code: str, last_n: int = 100) -> Optional[Dict[str, Any]]:
        from data_collector.daily_history import MAX_BYTES, VERSION, history_request

        code, url = history_request(ins_code, last_n)
        observation = self._get_quote_context_json(code, url, max_bytes=MAX_BYTES, validate_content=False)
        if observation is None:
            return None
        return dict(observation, version=VERSION, price_unit='Rial', last_n=last_n,
                    normalization_as_of=observation['received_at'])

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

    def get_development_history_observation(self, ins_code: str, start_date: str,
                                            end_date: str) -> Optional[Dict[str, Any]]:
        """بازه پژوهشی محدود؛ ارقام خارج بازه و holdout در پاسخ مصرف‌کننده قرار نمی‌گیرند."""
        from data_collector.daily_history import MAX_BYTES, _finite_float, _nonfinite, _unique

        class _IntegerToken(str):
            pass

        class _FloatToken(str):
            pass

        class _NonfiniteToken(str):
            pass

        def _selected_value(value):
            # Numeric values are converted only after their row has passed the date boundary.
            if type(value) is _IntegerToken:
                return int(value)
            if type(value) is _FloatToken:
                return _finite_float(value)
            if type(value) is _NonfiniteToken:
                return _nonfinite(value)
            if isinstance(value, list):
                return [_selected_value(item) for item in value]
            if isinstance(value, dict):
                return {key: _selected_value(item) for key, item in value.items()}
            return value

        if (not is_canonical_iso_date(start_date) or not is_canonical_iso_date(end_date)
                or start_date < settings.RESEARCH_CONFIG['dev_start']
                or end_date > settings.RESEARCH_CONFIG['holdout_start'] or start_date >= end_date):
            raise ValueError('بازه تاریخچه باید کاملاً پیش از holdout و در بازه پژوهش باشد')
        code, _ = daily_request(ins_code, start_date)
        url = f'{BASE_URL}/ClosingPrice/GetClosingPriceDailyList/{code}/0'
        observation = self._get_quote_context_json(code, url, max_bytes=MAX_BYTES, validate_content=False)
        if observation is None:
            return None
        try:
            data = json.loads(observation['content'].decode('utf-8-sig'), object_pairs_hook=_unique,
                              parse_constant=_NonfiniteToken, parse_float=_FloatToken, parse_int=_IntegerToken)
            rows = data.get('closingPriceDaily') if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError('فهرست بومی تاریخچه موجود نیست')
            selected = []
            for row in rows:
                if not isinstance(row, dict) or type(row.get('dEven')) not in (str, _IntegerToken):
                    raise ValueError('تاریخ بومی تاریخچه نامعلوم است')
                day = str(row['dEven'])
                if len(day) != 8 or not day.isascii() or not day.isdigit():
                    raise ValueError('تاریخ بومی تاریخچه نامعتبر است')
                iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
                if not is_canonical_iso_date(iso):
                    raise ValueError('تاریخ بومی تاریخچه نامعتبر است')
                if start_date <= iso < end_date:
                    if type(row.get('insCode')) not in (str, _IntegerToken) or str(row['insCode']) != code:
                        raise ValueError('شناسه ردیف تاریخچه متفاوت است')
                    selected.append(_selected_value(row))
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            logger.warning('تاریخچه پژوهشی پذیرفته نشد: %s', type(exc).__name__)
            return None
        selected_payload = {'closingPriceDaily': selected}
        selected_body = json.dumps(selected_payload, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode('utf-8')
        return {key: value for key, value in observation.items() if key != 'content'} | dict(
            price_unit='Rial', start_date=start_date, end_date_exclusive=end_date,
            payload=selected_payload, selected_row_count=len(selected),
            selected_payload_sha256=hashlib.sha256(selected_body).hexdigest(),
            response_scope='selected_native_rows_pre_holdout', source_body_archived=False,
            unselected_numeric_rows_not_archived=True)

    def get_retrospective_rights_history_observation(self, ins_code: str, start_date: str,
                                                   end_date: str, *, retrospective_rights_only=False) -> Optional[Dict[str, Any]]:
        """Isolated, explicitly opted-in retrospective rights history; today is excluded."""
        if retrospective_rights_only is not True:
            raise ValueError('Explicit retrospective rights-only opt-in is required')
        from data_collector.daily_history import MAX_BYTES, _finite_float, _nonfinite, _unique

        class _IntegerToken(str):
            pass

        class _FloatToken(str):
            pass

        class _NonfiniteToken(str):
            pass

        def _selected_value(value):
            # Numeric values are converted only after their row has passed the date boundary.
            if type(value) is _IntegerToken:
                return int(value)
            if type(value) is _FloatToken:
                return _finite_float(value)
            if type(value) is _NonfiniteToken:
                return _nonfinite(value)
            if isinstance(value, list):
                return [_selected_value(item) for item in value]
            if isinstance(value, dict):
                return {key: _selected_value(item) for key, item in value.items()}
            return value

        if (not is_canonical_iso_date(start_date) or not is_canonical_iso_date(end_date)
                or start_date < '1967-01-01'
                or end_date > now_tehran().date().isoformat() or start_date >= end_date):
            raise ValueError('Retrospective history must exclude today and start no earlier than 1967')
        code, _ = daily_request(ins_code, start_date)
        url = f'{BASE_URL}/ClosingPrice/GetClosingPriceDailyList/{code}/0'
        observation = self._get_quote_context_json(code, url, max_bytes=MAX_BYTES, validate_content=False)
        if observation is None:
            return None
        try:
            data = json.loads(observation['content'].decode('utf-8-sig'), object_pairs_hook=_unique,
                              parse_constant=_NonfiniteToken, parse_float=_FloatToken, parse_int=_IntegerToken)
            rows = data.get('closingPriceDaily') if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError('فهرست بومی تاریخچه موجود نیست')
            selected = []
            for row in rows:
                if not isinstance(row, dict) or type(row.get('dEven')) not in (str, _IntegerToken):
                    raise ValueError('تاریخ بومی تاریخچه نامعلوم است')
                day = str(row['dEven'])
                if len(day) != 8 or not day.isascii() or not day.isdigit():
                    raise ValueError('تاریخ بومی تاریخچه نامعتبر است')
                iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
                if not is_canonical_iso_date(iso):
                    raise ValueError('تاریخ بومی تاریخچه نامعتبر است')
                if iso > now_tehran().date().isoformat():
                    raise ValueError('Future source date is invalid')
                if start_date <= iso < end_date:
                    if type(row.get('insCode')) not in (str, _IntegerToken) or str(row['insCode']) != code:
                        raise ValueError('شناسه ردیف تاریخچه متفاوت است')
                    selected.append(_selected_value(row))
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            logger.warning('تاریخچه پژوهشی پذیرفته نشد: %s', type(exc).__name__)
            return None
        selected_payload = {'closingPriceDaily': selected}
        selected_body = json.dumps(selected_payload, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode('utf-8')
        return {key: value for key, value in observation.items() if key != 'content'} | dict(
            price_unit='Rial', start_date=start_date, end_date_exclusive=end_date,
            payload=selected_payload, selected_row_count=len(selected),
            selected_payload_sha256=hashlib.sha256(selected_body).hexdigest(),
            response_scope='selected_native_rows_retrospective_rights', source_body_archived=False,
            unselected_numeric_rows_not_archived=True, holdout_contaminated_for_rights_hypothesis=True,
            accepted_for_signals=False)

    def get_historical_day_universe_observation(self, trade_date: str) -> Optional[Dict[str, Any]]:
        """Public single-day archive; no response can cross the sealed research boundary."""
        from core.rights_daily_universe import MAX_BYTES, day_request

        url = day_request(trade_date)
        observation = self._get_quote_context_json('', url, max_bytes=MAX_BYTES)
        if observation is None:
            return None
        return dict(observation, requested_date=trade_date)

    def get_retrospective_day_universe_observation(self, trade_date: str, *, retrospective_rights_only=False) -> Optional[Dict[str, Any]]:
        """Isolated retrospective discovery; never changes the sealed model-development boundary."""
        from core.rights_daily_retrospective import MAX_BYTES, day_request

        if retrospective_rights_only is not True:
            raise ValueError('Explicit retrospective rights-only opt-in is required')
        url = day_request(trade_date)
        observation = self._get_quote_context_json('', url, max_bytes=MAX_BYTES)
        if observation is None:
            return None
        return dict(observation, requested_date=trade_date)

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
            response = self._send(url, timeout=(min(self.timeout, 10), min(self.timeout, 10)),
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
            response = self._send(url, timeout=(min(self.timeout, 10), min(self.timeout, 10)),
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

    def _get_quote_context_json(self, code: str, url: str, *, max_bytes=None,
                                validate_content=True) -> Optional[Dict[str, Any]]:
        """بایت محدود JSON بومی؛ فراخوان‌های داخلی نشانی ثابت می‌سازند و نشست صادر نمی‌شود."""
        from core.quote_context import MAX_JSON_BYTES

        limit = MAX_JSON_BYTES if max_bytes is None else max_bytes
        if type(limit) is not int or limit < 1 or type(validate_content) is not bool:
            raise ValueError('Explicit positive byte limit and content-validation policy required')

        requested, started = now_tehran_iso(), time.monotonic()
        try:
            if settings.RATE_LIMIT_DELAY > 0:
                time.sleep(settings.RATE_LIMIT_DELAY)
            response = self._send(url, timeout=(min(self.timeout, 10), min(self.timeout, 10)),
                stream=True, allow_redirects=False, headers={'Accept': 'application/json'})
            try:
                response.raise_for_status()
                if response.status_code != 200 or response.url != url:
                    raise ValueError('پاسخ جزئی یا تغییرمسیر مشخصات/دامنه مجاز نیست')
                length = response.headers.get('Content-Length')
                declared_length = None
                if length is not None:
                    if not isinstance(length, str) or not length.isascii() or not length.isdigit():
                        raise ValueError('طول اعلام‌شده پاسخ نامعتبر است')
                    declared_length = int(length)
                    if declared_length > limit:
                        raise ValueError('پاسخ مشخصات/دامنه از حد حجم گذشت')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > limit or time.monotonic()-started > 15:
                        raise ValueError('پاسخ مشخصات/دامنه از حد حجم یا زمان گذشت')
                    chunks.append(chunk)
                content = b''.join(chunks)
                # iter_content decompresses the body but Content-Length refers to
                # encoded bytes; reconcile only where both sizes are comparable.
                encoding = response.headers.get('Content-Encoding', '').strip().lower()
                transfer = response.headers.get('Transfer-Encoding', '').strip().lower()
                if (declared_length is not None and encoding in ('', 'identity')
                        and transfer in ('', 'identity') and size != declared_length):
                    raise ValueError('طول بدنه با طول اعلام‌شده منبع متفاوت است')
                if validate_content and (not content or content.lstrip().startswith(b'<') or b'\x00' in content):
                    raise ValueError('پاسخ خالی یا HTML به جای مشخصات/دامنه')
                return dict(source=self.name, ins_code=code, url=url, requested_at=requested,
                    received_at=now_tehran_iso(), http_status=response.status_code,
                    response_bytes=len(content), content=content,
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
