# -*- coding: utf-8 -*-
"""
لایه اعتبارسنجی و تضمین کیفیت داده‌ها (Data Validation Layer)

دو سطح:
۱. اعتبارسنجی نحوی (رد رکورد): قیمت مثبت، منطق OHLC، حجم غیرمنفی، زمان ثبت اسنپ‌شات
۲. اعتبارسنجی کمی (علامت‌گذاری، نه حذف): جهش قیمت غیرعادی و حجم انفجاری
"""

import logging
import math
from datetime import date as calendar_date
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from core.data_quality import quant_anomaly_flags

logger = logging.getLogger(__name__)

def _pick(record: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record.get(key)
    return None


def _valid_date(value: str) -> bool:
    try:
        return len(value) == 10 and calendar_date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _number(value, positive=False, integer=False) -> bool:
    if isinstance(value, (bool, str)):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and (number > 0 if positive else number >= 0) and (not integer or number.is_integer())
    except (TypeError, ValueError, OverflowError):
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

        if not isinstance(date, str) or not _valid_date(date):
            errors.append("تاریخ رکورد نامعتبر یا خالی است.")

        for name, val in [("Open", open_p), ("High", high_p), ("Low", low_p), ("Close", close_p)]:
            if not _number(val, positive=True):
                errors.append(f"قیمت {name} باید عددی مثبت و بزرگتر از صفر باشد (مقدار: {val}).")

        if all(_number(v, positive=True) for v in (open_p, high_p, low_p, close_p)):
            if high_p < low_p:
                errors.append(f"قیمت بیشترین ({high_p}) کمتر از کمترین ({low_p}) است.")
            if high_p < open_p or high_p < close_p:
                errors.append(f"قیمت بیشترین ({high_p}) باید بزرگتر مساوی باز و بسته باشد.")
            if low_p > open_p or low_p > close_p:
                errors.append(f"قیمت کمترین ({low_p}) باید کوچکتر مساوی باز و بسته باشد.")

        if volume is not None and not _number(volume, integer=True):
            errors.append(f"حجم معاملات نمی‌تواند منفی باشد: {volume}")
        if value is not None and not _number(value):
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
        if "date" not in cleaned:
            return cleaned.iloc[:0], initial_len
        dates = pd.to_datetime(cleaned["date"], format="%Y-%m-%d", errors="coerce")
        valid_date = dates.notna() & (cleaned["date"] == dates.dt.strftime("%Y-%m-%d"))
        cleaned = cleaned.loc[valid_date].drop_duplicates()
        # دو مقدار متفاوت برای یک روز: هیچ‌کدام بی‌دلیل «آخرین نسخه» فرض نمی‌شود.
        cleaned = cleaned.loc[~cleaned["date"].duplicated(keep=False)].copy()
        for col in cleaned.columns:
            if col.endswith("_raw") or col in {"volume", "value", "trade_count", "open", "high", "low", "close"}:
                cleaned[col] = pd.to_numeric(cleaned[col], errors="coerce")
        for col in ("volume", "value", "trade_count"):
            if col in cleaned:
                valid = np.isfinite(cleaned[col]) & (cleaned[col] >= 0)
                if col != "value":
                    valid &= cleaned[col] % 1 == 0
                cleaned = cleaned.loc[valid].copy()
        suffix = "_raw" if "high_raw" in cleaned.columns else ""
        o, h, lo, c = (f"{p}{suffix}" for p in ("open", "high", "low", "close"))
        if suffix:
            # داده خام TSETMC: فقط وقتی کل روز را حذف می‌کنیم که فیلدهای اساسی خراب باشند.
            # اگر فقط قیمت اولین/آخرین معامله با بازه روز ناسازگار است، همان فیلد خالی می‌شود
            # (داده ساختگی جایگزین نمی‌شود) و بقیه روز حفظ می‌شود.
            essential = [col for col in (h, lo, "close_final_raw") if col in cleaned.columns]
            for col in essential:
                cleaned = cleaned[np.isfinite(cleaned[col]) & (cleaned[col] > 0)]
            cleaned = cleaned[cleaned[h] >= cleaned[lo]]
            repaired = 0
            for col in (o, c):
                if col in cleaned.columns:
                    bad = ~np.isfinite(cleaned[col]) | (cleaned[col] <= 0) | (cleaned[col] > cleaned[h]) | (cleaned[col] < cleaned[lo])
                    repaired += int(bad.sum())
                    cleaned.loc[bad, col] = float("nan")
            if repaired:
                logger.info("تعداد %d فیلد قیمت ناسازگار با بازه روز خالی شد (روزها حفظ شدند).", repaired)
        else:
            for col in (o, h, lo, c):
                if col in cleaned.columns:
                    cleaned = cleaned[np.isfinite(cleaned[col]) & (cleaned[col] > 0)]
            if h in cleaned.columns and lo in cleaned.columns:
                cleaned = cleaned[cleaned[h] >= cleaned[lo]]
                for col in (o, c):
                    if col in cleaned.columns:
                        cleaned = cleaned[(cleaned[col] <= cleaned[h]) & (cleaned[col] >= cleaned[lo])]

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
        return quant_anomaly_flags(df)

    @staticmethod
    def validate_snapshot(snapshot: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """اعتبارسنجی اسنپ‌شات لحظه‌ای تابلو."""
        errors = []
        for key, value in snapshot.items():
            if (key.endswith(("_price", "_volume", "_count")) or key in {"volume", "value", "trade_count"}
                    or key.startswith(("bid_", "ask_", "price_limit_"))):
                if value is not None and not _number(value):
                    errors.append(f"عدد نامعتبر در {key}")
        if errors:
            return False, errors
        if not snapshot.get("captured_at"):
            errors.append("زمان ثبت اسنپ‌شات مشخص نیست.")

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
