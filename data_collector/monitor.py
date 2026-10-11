# -*- coding: utf-8 -*-
"""
ماژول پایش لحظه‌ای سلامت و تازگی داده‌های بورس (Data Freshness & Health Monitor)
این ماژول وظیفه دارد تاخیر زمانی سرورهای TSETMC را نسبت به ساعت رسمی تهران محاسبه کرده
و وقوع کَش شدن (فریز شدن دیتای سرور)، جهش‌های غیرعادی قیمت و قطعی فید را شناسایی کند.
"""

import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


class DataFreshnessMonitor:
    """پایش سلامت و تازگی فید داده‌های لحظه‌ای بازار."""

    def __init__(self, stale_threshold_seconds: int = 90):
        self.stale_threshold_seconds = stale_threshold_seconds
        self.last_seen_timestamps: Dict[str, str] = {}
        self.last_seen_prices: Dict[str, float] = {}

    def inspect_snapshot(self, symbol_fa: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        بررسی یک اسنپ‌شات لحظه‌ای و بازگرداندن کارت سلامت آن.
        """
        now = datetime.now()
        now_str = now.strftime("%H:%M:%S")

        last_price = snapshot.get("last_price")
        provider_time_str = snapshot.get("provider_timestamp")

        delay_seconds = 0
        status_flag = "🟢 Fresh"
        warnings = []

        # ۱. محاسبه تاخیر در صورت وجود زمان سرور
        if provider_time_str:
            try:
                # تبدیل provider_timestamp به زمان
                p_dt = pd.to_datetime(provider_time_str)
                delay_seconds = max(0, int((now - p_dt.replace(tzinfo=None)).total_seconds()))
            except Exception:
                delay_seconds = 0

            # بررسی فریز بودن سرور (عدم تغییر تایم‌استمپ سرور با وجود گذشت زمان)
            last_p_time = self.last_seen_timestamps.get(symbol_fa)
            if last_p_time and last_p_time == provider_time_str:
                warnings.append("⚠️ تایم‌استمپ سرور تغییر نکرده است (احتمال پاسخ کش‌شده سرور بورس).")
                status_flag = "🟡 Delayed"

            self.last_seen_timestamps[symbol_fa] = provider_time_str

        # ارزیابی تاخیر بر اساس آستانه
        if delay_seconds > self.stale_threshold_seconds:
            status_flag = "🔴 Stale / Frozen"
            warnings.append(f"تاخیر سرور ({delay_seconds} ثانیه) از آستانه مجاز بیشتر است.")
        elif delay_seconds > 45 and status_flag != "🔴 Stale / Frozen":
            status_flag = "🟡 Delayed"

        # ۲. بررسی جهش نامتعارف قیمت (Bad Tick Check)
        if last_price and symbol_fa in self.last_seen_prices:
            prev_price = self.last_seen_prices[symbol_fa]
            if prev_price > 0:
                change_pct = abs((last_price - prev_price) / prev_price) * 100.0
                if change_pct > 10.0:  # جهش بیش از ۱۰ درصد در یک اسنپ‌شات
                    warnings.append(f"جهش مشکوک قیمت: {prev_price} -> {last_price} ({change_pct:.1f}%)")

        if last_price:
            self.last_seen_prices[symbol_fa] = last_price

        # ۳. محاسبه نسبت قدرت خریدار به فروشنده
        real_buy_vol = snapshot.get("real_buy_volume", 0) or 0
        real_sell_vol = snapshot.get("real_sell_volume", 0) or 0
        real_buy_count = snapshot.get("real_buy_count", 0) or 0
        real_sell_count = snapshot.get("real_sell_count", 0) or 0

        buyer_power_ratio = 1.0
        if real_buy_count > 0 and real_sell_count > 0 and real_sell_vol > 0:
            avg_buy_per_capita = real_buy_vol / real_buy_count
            avg_sell_per_capita = real_sell_vol / real_sell_count
            if avg_sell_per_capita > 0:
                buyer_power_ratio = round(avg_buy_per_capita / avg_sell_per_capita, 2)

        return {
            "symbol": symbol_fa,
            "checked_at": now_str,
            "provider_time": provider_time_str or now_str,
            "delay_seconds": delay_seconds,
            "status": status_flag,
            "last_price": last_price,
            "buyer_power": buyer_power_ratio,
            "warnings": warnings,
            "is_healthy": status_flag == "🟢 Fresh"
        }
