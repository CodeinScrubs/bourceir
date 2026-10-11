# -*- coding: utf-8 -*-
"""
ماژول پایش لحظه‌ای سلامت و تازگی داده‌های بورس (Data Freshness & Health Monitor)

تاخیر سرور TSETMC نسبت به ساعت رسمی تهران، فریز شدن پاسخ سرور (کش)، جهش‌های غیرعادی قیمت
و نسبت قدرت خریدار حقیقی را محاسبه می‌کند.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from core.timeutils import TEHRAN_TZ, now_tehran

logger = logging.getLogger(__name__)


def buyer_power(snapshot: Dict[str, Any]) -> Optional[float]:
    """
    قدرت خریدار حقیقی = سرانه خرید حقیقی ÷ سرانه فروش حقیقی.
    بزرگ‌تر از ۱ یعنی خریداران حقیقی به‌طور میانگین حجم بیشتری می‌خرند (ورود پول درشت حقیقی).
    """
    rb_vol = snapshot.get("real_buy_volume") or 0
    rb_cnt = snapshot.get("real_buy_count") or 0
    rs_vol = snapshot.get("real_sell_volume") or 0
    rs_cnt = snapshot.get("real_sell_count") or 0
    if rb_cnt > 0 and rs_cnt > 0 and rs_vol > 0:
        return round((rb_vol / rb_cnt) / (rs_vol / rs_cnt), 2)
    return None


def _to_tehran(value: Any) -> Optional[datetime]:
    if not value:
        return None
    ts = pd.to_datetime(value).to_pydatetime()
    # زمان بدون منطقه زمانی، به وقت تهران فرض می‌شود
    return ts.replace(tzinfo=TEHRAN_TZ) if ts.tzinfo is None else ts.astimezone(TEHRAN_TZ)


class DataFreshnessMonitor:
    """پایش سلامت و تازگی فید داده‌های لحظه‌ای بازار."""

    def __init__(self, stale_threshold_seconds: int = 90, delayed_threshold_seconds: int = 45):
        self.stale_threshold_seconds = stale_threshold_seconds
        self.delayed_threshold_seconds = delayed_threshold_seconds
        self.last_seen_timestamps: Dict[str, str] = {}
        self.last_seen_prices: Dict[str, float] = {}

    def inspect_snapshot(self, symbol_fa: str, snapshot: Dict[str, Any],
                         now: Optional[datetime] = None) -> Dict[str, Any]:
        """بررسی یک اسنپ‌شات لحظه‌ای و بازگرداندن کارت سلامت آن."""
        now = (now or now_tehran()).astimezone(TEHRAN_TZ)
        last_price = snapshot.get("last_price")
        provider_time_str = snapshot.get("provider_timestamp")

        delay_seconds = 0
        status_flag = "🟢 Fresh"
        warnings = []

        if provider_time_str:
            try:
                delay_seconds = max(0, int((now - _to_tehran(provider_time_str)).total_seconds()))
            except Exception:
                delay_seconds = 0

            # فریز بودن سرور: تایم‌استمپ سرور با وجود گذشت زمان تغییر نکرده
            if self.last_seen_timestamps.get(symbol_fa) == provider_time_str:
                warnings.append("⚠️ تایم‌استمپ سرور تغییر نکرده است (احتمال پاسخ کش‌شده سرور بورس).")
                status_flag = "🟡 Delayed"
            self.last_seen_timestamps[symbol_fa] = provider_time_str

        if delay_seconds > self.stale_threshold_seconds:
            status_flag = "🔴 Stale / Frozen"
            warnings.append(f"تاخیر سرور ({delay_seconds} ثانیه) از آستانه مجاز بیشتر است.")
        elif delay_seconds > self.delayed_threshold_seconds:
            status_flag = "🟡 Delayed"

        # جهش نامتعارف قیمت بین دو اسنپ‌شات (Bad Tick)
        if last_price and symbol_fa in self.last_seen_prices:
            prev_price = self.last_seen_prices[symbol_fa]
            if prev_price > 0:
                change_pct = abs((last_price - prev_price) / prev_price) * 100.0
                if change_pct > 10.0:
                    warnings.append(f"جهش مشکوک قیمت: {prev_price} -> {last_price} ({change_pct:.1f}%)")
        if last_price:
            self.last_seen_prices[symbol_fa] = last_price

        bp = buyer_power(snapshot)
        return {
            "symbol": symbol_fa,
            "checked_at": now.strftime("%H:%M:%S"),
            "provider_time": provider_time_str or now.isoformat(),
            "delay_seconds": delay_seconds,
            "status": status_flag,
            "last_price": last_price,
            "buyer_power": bp if bp is not None else 1.0,
            "warnings": warnings,
            "is_healthy": status_flag == "🟢 Fresh",
        }
