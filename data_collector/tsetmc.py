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

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import settings
from core.text import normalize_fa, normalize_symbol
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time, now_tehran, now_tehran_iso
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

    def __init__(self, session: Optional[requests.Session] = None):
        self.timeout = settings.REQUEST_TIMEOUT_SECONDS
        self.session = session or self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=settings.MAX_RETRIES, backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
        )
        session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
        session.headers.update({"User-Agent": "Mozilla/5.0 (BRS Quant Engine)", "Accept": "application/json"})
        return session

    def _get_json(self, url: str, rate_limit: bool = True, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if rate_limit and settings.RATE_LIMIT_DELAY > 0:
            time.sleep(settings.RATE_LIMIT_DELAY)
        try:
            response = self.session.get(url, timeout=timeout or self.timeout)
            response.raise_for_status()
            return response.json()
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
            universe = [self._meta_from_market_watch(r) for r in rows if self.is_in_scope(r.get("lva", ""), r.get("yVal"))]
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
                if meta is None:
                    failed += 1
                    continue
            if self.is_in_scope(meta["symbol_fa"], meta.get("instrument_type_code")):
                universe.append(meta)
        if failed > max(10, len(candidates) // 10):
            logger.error("مشخصات %d نماد دریافت نشد؛ فهرست ناقص کنار گذاشته شد.", failed)
            return []
        logger.info("جامعه سهام (مسیر پشتیبان): %d نماد.", len(universe))
        return self._dedupe_by_symbol(universe, activity)

    def search_symbol(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        target = normalize_symbol(symbol_fa)
        data = self._get_json(f"{BASE_URL}/Instrument/GetInstrumentSearch/{requests.utils.quote(target)}")
        if not data:
            return None
        for item in data.get("instrumentSearch", []):
            if normalize_symbol(item.get("lVal18AFC", "")) == target and item.get("lastDate", 1) != 0:
                info = self.get_instrument_info(str(item["insCode"]))
                if info:
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

    def get_symbol_history(self, ins_code: str, last_n: int = 0) -> Optional[pd.DataFrame]:
        data = self._get_json(f"{BASE_URL}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{int(last_n)}")
        rows = (data or {}).get("closingPriceDaily")
        if rows is None:
            return None
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        df = pd.DataFrame({
            "date": df["dEven"].map(deven_to_iso),
            "open_raw": df["priceFirst"].astype(float),
            "high_raw": df["priceMax"].astype(float),
            "low_raw": df["priceMin"].astype(float),
            "close_raw": df["pDrCotVal"].astype(float),
            "close_final_raw": df["pClosing"].astype(float),
            "ref_price_raw": df["priceYesterday"].astype(float),
            "volume": df["qTotTran5J"].astype(float),
            "value": df["qTotCap"].astype(float),
            "trade_count": df["zTotTran"].astype(float),
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

    def get_client_types_history(self, ins_code: str, dates: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
        """
        تاریخچه حقیقی/حقوقی. بدون dates کل تاریخچه (~۱ مگابایت)؛ با dates فقط همان روزها (به‌روزرسانی سبک روزانه).
        """
        if dates is not None:
            rows = []
            for day in dates:
                data = self._get_json(f"{BASE_URL}/ClientType/GetClientTypeHistory/{ins_code}/{day.replace('-', '')}")
                if data is None:
                    return None
                if (data.get("clientType") or {}).get("recDate"):
                    rows.append(data["clientType"])
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
        return {str(r["insCode"]): r for r in (data or {}).get("clientTypeAllDto", [])}

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
        except (TypeError, ValueError):
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
            "last_trade_time": heven_to_time(row["hEven"]) if row.get("hEven") else None,
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
        for level in row.get("blDs") or []:
            n = level.get("n")
            if not n or n > 5:
                continue
            snap[f"bid_price_{n}"] = level.get("pmd")
            snap[f"bid_vol_{n}"] = level.get("qmd")
            snap[f"bid_count_{n}"] = level.get("zmd")
            snap[f"ask_price_{n}"] = level.get("pmo")
            snap[f"ask_vol_{n}"] = level.get("qmo")
            snap[f"ask_count_{n}"] = level.get("zmo")
        # صف خرید/فروش فقط وقتی معنا دارد که بهترین مظنه روی سقف/کف دامنه نوسان باشد
        limit_up, limit_down = snap["price_limit_up"], snap["price_limit_down"]
        # بدون دامنه مجاز روز، وجود صف نامعلوم است (None)، نه صفر
        snap["buy_queue_volume"] = (None if not limit_up else
                                    snap.get("bid_vol_1") if snap.get("bid_price_1") == limit_up else 0)
        snap["sell_queue_volume"] = (None if not limit_down else
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
        final_date = ((data or {}).get("closingPriceInfo") or {}).get("finalLastDate")
        return deven_to_iso(final_date) if final_date else None

    def get_session_candles(self) -> Optional[Dict[str, Any]]:
        """
        کندل روز جاری همه سهام از دیده‌بان بازار (بلافاصله بعد از پایان معاملات در دسترس است؛
        در حالی که سرویس تاریخچه روزانه، کندل امروز را تا عصر به‌تدریج منتشر می‌کند).
        خروجی: {'date': 'YYYY-MM-DD', 'candles': [{'ins_code', ستون‌های DAILY_COLUMNS}, ...]}
        """
        data = self._get_json(MARKET_WATCH_STOCKS, rate_limit=False)
        rows = [r for r in (data or {}).get("marketwatch", []) if (r.get("qtj") or 0) > 0]
        if not rows:
            return None
        most_traded = max(rows, key=lambda r: r.get("qtc") or 0)
        market_date = self.get_market_date(str(most_traded["insCode"]))
        if not market_date:
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
        if not data:
            return None
        client_types = self.get_client_types_today()
        captured_at = now_tehran_iso()  # زمانی که هر دو پاسخ واقعاً در دست ماست (نه قبل از درخواست)
        return [
            self.snapshot_from_market_watch(row, client_types.get(str(row.get("insCode"))), captured_at)
            for row in data.get("marketwatch", [])
        ]

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
