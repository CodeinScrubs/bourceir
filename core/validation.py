# -*- coding: utf-8 -*-
"""
لایه اعتبارسنجی و تضمین کیفیت داده‌ها (Data Validation Layer)

دو سطح:
۱. اعتبارسنجی نحوی (رد رکورد): قیمت مثبت، منطق OHLC، حجم غیرمنفی، زمان ثبت اسنپ‌شات
۲. اعتبارسنجی کمی (علامت‌گذاری، نه حذف): جهش قیمت غیرعادی و حجم انفجاری
"""

import logging
import math
from datetime import date, datetime
from numbers import Real
from typing import Any, Dict, List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# جهش قیمت بزرگ‌تر از این مقدار نسبت به قیمت مرجع مشکوک است (دامنه نوسان عادی ±۳ تا ±۵ درصد است؛
# فقط روز بازگشایی بعد از توقف طولانی می‌تواند بدون دامنه باشد)
PRICE_JUMP_THRESHOLD = 0.105
VOLUME_SPIKE_MULTIPLIER = 10.0


def _pick(record: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record.get(key)
    return None


def _finite(value, minimum=0, integer=False):
    return (not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
            and value >= minimum and (not integer or float(value).is_integer()))


def _date_valid(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


class DataValidator:
    """کلاس اعتبارسنجی داده‌های بازار سرمایه ایران."""

    @staticmethod
    def validate_daily_candle(record: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        بررسی صحت منطقی یک کندل روزانه (کلیدهای open/high/low/close یا *_raw پذیرفته می‌شوند).
        خروجی: (معتبر است؟، لیست دلایل رد)
        """
        errors = []
        date = record.get("date")
        open_p = _pick(record, "open_raw", "open")
        high_p = _pick(record, "high_raw", "high")
        low_p = _pick(record, "low_raw", "low")
        close_p = _pick(record, "close_raw", "close")
        volume = record.get("volume", 0)
        value = record.get("value", 0)

        if not _date_valid(date):
            errors.append("تاریخ رکورد نامعتبر یا خالی است.")

        for name, val in [("Open", open_p), ("High", high_p), ("Low", low_p), ("Close", close_p)]:
            if not _finite(val, minimum=1e-12):
                errors.append(f"قیمت {name} باید عددی مثبت و بزرگتر از صفر باشد (مقدار: {val}).")

        if all(_finite(v, minimum=1e-12) for v in (open_p, high_p, low_p, close_p)):
            if high_p < low_p:
                errors.append(f"قیمت بیشترین ({high_p}) کمتر از کمترین ({low_p}) است.")
            if high_p < open_p or high_p < close_p:
                errors.append(f"قیمت بیشترین ({high_p}) باید بزرگتر مساوی باز و بسته باشد.")
            if low_p > open_p or low_p > close_p:
                errors.append(f"قیمت کمترین ({low_p}) باید کوچکتر مساوی باز و بسته باشد.")

        if volume is not None and not _finite(volume, integer=True):
            errors.append(f"حجم معاملات نمی‌تواند منفی باشد: {volume}")
        if value is not None and not _finite(value):
            errors.append(f"ارزش معاملات نمی‌تواند منفی باشد: {value}")

        is_valid = len(errors) == 0
        if not is_valid:
            logger.warning("رد رکورد نامعتبر: %s | خطاها: %s", record, "; ".join(errors))
        return is_valid, errors

    @staticmethod
    def validate_daily_dataframe(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
        """
        پالایش کل یک دیتافریم تاریخی: حذف تاریخ تکراری، قیمت نامثبت، نقض منطق OHLC و حجم منفی.
        خروجی: (دیتافریم پاکسازی‌شده، تعداد رکوردهای حذف‌شده)
        """
        if df.empty:
            return df, 0
        initial_len = len(df)
        cleaned = df.copy()
        if "date" not in cleaned.columns:
            return cleaned.iloc[:0], initial_len
        cleaned = cleaned[cleaned["date"].map(_date_valid)]
        conflicts = cleaned.groupby("date", dropna=False).nunique(dropna=False).gt(1).any(axis=1)
        cleaned = cleaned[~cleaned["date"].isin(conflicts[conflicts].index)].drop_duplicates("date")
        numeric = [c for c in cleaned if c.endswith("_raw") or c in
                   ("open", "high", "low", "close", "volume", "value", "trade_count")]
        for col in numeric:
            cleaned[col] = cleaned[col].map(lambda v: float("nan") if isinstance(v, bool) else v)
            cleaned[col] = pd.to_numeric(cleaned[col], errors="coerce").replace([float("inf"), -float("inf")], float("nan"))

        suffix = "_raw" if "high_raw" in cleaned.columns else ""
        o, h, lo, c = (f"{p}{suffix}" for p in ("open", "high", "low", "close"))
        if not {h, lo, "volume"}.issubset(cleaned.columns):
            return cleaned.iloc[:0], initial_len
        if suffix:
            # داده خام TSETMC: فقط وقتی کل روز را حذف می‌کنیم که فیلدهای اساسی خراب باشند.
            # اگر فقط قیمت اولین/آخرین معامله با بازه روز ناسازگار است، همان فیلد خالی می‌شود
            # (داده ساختگی جایگزین نمی‌شود) و بقیه روز حفظ می‌شود.
            essential = [col for col in (h, lo, "close_final_raw") if col in cleaned.columns]
            for col in essential:
                cleaned = cleaned[cleaned[col] > 0]
            cleaned = cleaned[cleaned[h] >= cleaned[lo]]
            repaired = 0
            for col in (o, c):
                if col in cleaned.columns:
                    bad = (cleaned[col] <= 0) | (cleaned[col] > cleaned[h]) | (cleaned[col] < cleaned[lo])
                    repaired += int(bad.sum())
                    cleaned.loc[bad, col] = float("nan")
            if repaired:
                logger.info("تعداد %d فیلد قیمت ناسازگار با بازه روز خالی شد (روزها حفظ شدند).", repaired)
        else:
            for col in (o, h, lo, c):
                if col in cleaned.columns:
                    cleaned = cleaned[cleaned[col] > 0]
            if h in cleaned.columns and lo in cleaned.columns:
                cleaned = cleaned[cleaned[h] >= cleaned[lo]]
                for col in (o, c):
                    if col in cleaned.columns:
                        cleaned = cleaned[(cleaned[col] <= cleaned[h]) & (cleaned[col] >= cleaned[lo])]
        if "volume" in cleaned.columns:
            cleaned = cleaned[cleaned["volume"].map(lambda v: _finite(v, integer=True))]
        for col in ("value", "trade_count"):
            if col in cleaned:
                cleaned = cleaned[cleaned[col].map(lambda v: _finite(v, integer=col == "trade_count"))]
        if "ref_price_raw" in cleaned:
            cleaned.loc[~(cleaned["ref_price_raw"] > 0), "ref_price_raw"] = float("nan")

        removed = initial_len - len(cleaned)
        if removed > 0:
            logger.info("تعداد %d رکورد نامعتبر/تکراری در فرآیند پالایش حذف گردید.", removed)
        return cleaned.reset_index(drop=True), removed

    @staticmethod
    def quant_anomaly_flags(df: pd.DataFrame) -> pd.DataFrame:
        """
        علامت‌گذاری ناهنجاری‌های کمی (بدون حذف داده):
        - price_anomaly: تغییر قیمت پایانی نسبت به قیمت مرجع بیش از ۱۰.۵٪
        - volume_anomaly: حجم بیش از ۱۰ برابر میانه حجم ۳۰ روز قبل
        """
        from core.market_rules import pit_daily_returns

        flags = pd.DataFrame(index=df.index)
        flags["price_anomaly"] = pit_daily_returns(df).abs() > PRICE_JUMP_THRESHOLD
        prior_median = df["volume"].shift(1).rolling(30, min_periods=10).median()
        flags["volume_anomaly"] = (df["volume"] > VOLUME_SPIKE_MULTIPLIER * prior_median).fillna(False)
        return flags

    @staticmethod
    def validate_snapshot(snapshot: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """اعتبارسنجی اسنپ‌شات لحظه‌ای تابلو."""
        errors = []
        try:
            captured = datetime.fromisoformat(snapshot.get("captured_at", ""))
            if captured.tzinfo is None:
                raise ValueError("timezone")
        except (ValueError, TypeError):
            errors.append("زمان ثبت اسنپ‌شات باید معتبر و دارای منطقه زمانی باشد.")
        for key, value in snapshot.items():
            numeric = key in ("value", "volume", "trade_count", "base_volume") or any(
                part in key for part in ("_price", "_volume", "_count", "_vol_", "_limit_"))
            if numeric and value is not None and not _finite(value, integer=any(
                part in key for part in ("volume", "count", "_vol_"))):
                errors.append(f"مقدار عددی نامعتبر: {key}")
        if errors:
            return False, errors

        last_p = snapshot.get("last_price")
        if last_p is not None and last_p < 0:
            errors.append(f"قیمت آخرین معامله نامعتبر است: {last_p}")
        if (snapshot.get("trade_count") or 0) > 0 and not last_p:
            errors.append("نماد معامله داشته ولی قیمت آخرین معامله صفر است.")

        vol = snapshot.get("volume")
        if vol is not None and vol < 0:
            errors.append(f"حجم معاملات منفی است: {vol}")

        up, down = snapshot.get("price_limit_up"), snapshot.get("price_limit_down")
        if last_p and up and down and (snapshot.get("trade_count") or 0) > 0:
            if not (down <= last_p <= up):
                errors.append(f"قیمت آخرین معامله ({last_p}) خارج از دامنه مجاز [{down}، {up}] است.")

        return len(errors) == 0, errors
