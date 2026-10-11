# -*- coding: utf-8 -*-
"""
لایه اعتبارسنجی و تضمین کیفیت داده‌ها (Data Validation Layer)
مسئول بررسی صحت، سلامت و عدم تناقض در داده‌های دریافتی پیش از ذخیره‌سازی در پایگاه داده.
"""

import logging
from typing import Dict, Any, Tuple, List
import pandas as pd

logger = logging.getLogger(__name__)


class DataValidator:
    """کلاس اعتبارسنجی داده‌های بازار سرمایه ایران."""

    @staticmethod
    def validate_daily_candle(record: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        بررسی صحت منطقی یک کندل روزانه (OHLCV).
        خروجی: (آیا رکورد معتبر است، لیست دلایل در صورت نامعتبر بودن)
        """
        errors = []

        date = record.get("date")
        open_p = record.get("open")
        high_p = record.get("high")
        low_p = record.get("low")
        close_p = record.get("close")
        volume = record.get("volume", 0)
        value = record.get("value", 0)

        # ۱. تاریخ باید معتبر و غیرخالی باشد
        if not date or not isinstance(date, str) or len(date.strip()) < 8:
            errors.append("تاریخ رکورد نامعتبر یا خالی است.")

        # ۲. بررسی مقادیر مثبت برای قیمت‌ها
        for name, val in [("Open", open_p), ("High", high_p), ("Low", low_p), ("Close", close_p)]:
            if val is None or val <= 0:
                errors.append(f"قیمت {name} باید عددی مثبت و بزرگتر از صفر باشد (مقدار: {val}).")

        # ۳. قوانین رابطه قیمت‌های OHLC
        if open_p and high_p and low_p and close_p:
            if high_p < low_p:
                errors.append(f"قیمت بیشترین ({high_p}) کمتر از کمترین ({low_p}) است.")
            if high_p < open_p or high_p < close_p:
                errors.append(f"قیمت بیشترین ({high_p}) باید بزرگتر مساوی باز و بسته باشد.")
            if low_p > open_p or low_p > close_p:
                errors.append(f"قیمت کمترین ({low_p}) باید کوچکتر مساوی باز و بسته باشد.")

        # ۴. حجم و ارزش معاملات نباید منفی باشد
        if volume is not None and volume < 0:
            errors.append(f"حجم معاملات نمی‌تواند منفی باشد: {volume}")
        if value is not None and value < 0:
            errors.append(f"ارزش معاملات نمی‌تواند منفی باشد: {value}")

        is_valid = len(errors) == 0
        if not is_valid:
            logger.warning("رد رکورد نامعتبر: %s | خطاها: %s", record, "; ".join(errors))

        return is_valid, errors

    @staticmethod
    def validate_daily_dataframe(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
        """
        پالایش و اعتبارسنجی کل یک دیتافریم تاریخی.
        رکوردهای نامعتبر یا تکراری فیلتر می‌شوند.
        خروجی: (دیتافریم پاکسازی‌شده، تعداد رکوردهای نامعتبر حذف‌شده)
        """
        if df.empty:
            return df, 0

        initial_len = len(df)
        cleaned_df = df.copy()

        # ۱. حذف ردیف‌های با تاریخ تکراری (نگه‌داشتن آخرین رکورد)
        if "date" in cleaned_df.columns:
            cleaned_df = cleaned_df.drop_duplicates(subset=["date"], keep="last")

        # ۲. فیلتر کردن ردیف‌های با قیمت‌های نامعتبر
        price_cols = [c for c in ["open", "high", "low", "close"] if c in cleaned_df.columns]
        for col in price_cols:
            cleaned_df = cleaned_df[cleaned_df[col] > 0]

        # ۳. فیلتر منطق high >= low
        if "high" in cleaned_df.columns and "low" in cleaned_df.columns:
            cleaned_df = cleaned_df[cleaned_df["high"] >= cleaned_df["low"]]

        # ۴. فیلتر عدم منفی بودن حجم
        if "volume" in cleaned_df.columns:
            cleaned_df = cleaned_df[cleaned_df["volume"] >= 0]

        removed_count = initial_len - len(cleaned_df)
        if removed_count > 0:
            logger.info("تعداد %d رکورد نامعتبر/تکراری در فرآیند پالایش حذف گردید.", removed_count)

        return cleaned_df, removed_count

    @staticmethod
    def validate_snapshot(snapshot: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        اعتبارسنجی اطلاعات یک اسنپ‌شات لحظه‌ای تابلو.
        """
        errors = []
        if not snapshot.get("captured_at"):
            errors.append("زمان ثبت اسنپ‌شات مشخص نیست.")

        last_p = snapshot.get("last_price")
        if last_p is not None and last_p <= 0:
            errors.append(f"قیمت آخرین معامله نامعتبر است: {last_p}")

        vol = snapshot.get("volume")
        if vol is not None and vol < 0:
            errors.append(f"حجم معاملات منفی است: {vol}")

        is_valid = len(errors) == 0
        return is_valid, errors
