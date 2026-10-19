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

from core.client_quality import snapshot_buyer_power, snapshot_client_metrics
from core.risk import finite_number
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, now_tehran

logger = logging.getLogger(__name__)


def buyer_power(snapshot: Dict[str, Any]) -> Optional[float]:
    """
    قدرت خریدار حقیقی = سرانه خرید حقیقی ÷ سرانه فروش حقیقی.
    بزرگ‌تر از ۱ یعنی خریداران حقیقی به‌طور میانگین حجم بیشتری می‌خرند (ورود پول درشت حقیقی).
    """
    power = snapshot_buyer_power(snapshot)
    return round(power, 2) if power is not None else None


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
        now = now if now is not None else now_tehran()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('زمان بررسی باید منطقه زمانی داشته باشد')
        now = now.astimezone(TEHRAN_TZ)
        symbol_fa = normalize_symbol(symbol_fa)
        try:
            last_price = finite_number(snapshot.get('last_price'))
        except ValueError:
            last_price = None
        provider_time_str = snapshot.get("provider_timestamp")

        delay_seconds: Optional[float] = None
        signed_age_seconds = None
        timezone_assumed = False
        status_flag = "⚪ Unknown"
        warnings = [] if last_price is not None else ['آخرین قیمت عدد مثبت معتبر نیست؛ سلامت مظنه تأیید نمی‌شود.']

        if provider_time_str:
            try:
                parsed = pd.to_datetime(provider_time_str)
                if not isinstance(parsed, pd.Timestamp) or pd.isna(parsed):
                    raise ValueError('زمان منبع معتبر نیست')
                timezone_assumed = parsed.tzinfo is None
                if timezone_assumed:
                    warnings.append('منطقه زمانی منبع تهران فرض شده است؛ سلامت قطعی نیست.')
                # علامت و کسری ثانیه پیش از تصمیم حفظ می‌شوند؛ زمان آینده به صفر گرد نمی‌شود.
                signed_age_seconds = (now - _to_tehran(provider_time_str)).total_seconds()
                delay_seconds = signed_age_seconds
                status_flag = "🟢 Fresh"
                if delay_seconds < 0:
                    warnings.append("زمان منبع پس از زمان بررسی است؛ ساعت منبع و رایانه باید مستقل بررسی شوند.")
                    status_flag = "⚪ Unknown"
                    delay_seconds = None
            except (TypeError, ValueError, OverflowError):
                warnings.append("زمان سرور قابل خواندن نیست؛ تازگی داده نامعلوم است.")

            # فریز بودن سرور: تایم‌استمپ سرور با وجود گذشت زمان تغییر نکرده
            if delay_seconds is not None and self.last_seen_timestamps.get(symbol_fa) == provider_time_str:
                warnings.append("⚠️ تایم‌استمپ سرور تغییر نکرده است (احتمال پاسخ کش‌شده سرور بورس).")
                status_flag = "🟡 Delayed"
            self.last_seen_timestamps[symbol_fa] = provider_time_str
        else:
            # بدون تاریخ و ساعت سرور، داده «تازه» فرض نمی‌شود
            warnings.append("زمان سرور برای این نماد موجود نیست؛ تازگی داده نامعلوم است.")

        if delay_seconds is None:
            pass
        elif delay_seconds > self.stale_threshold_seconds:
            status_flag = "🔴 Stale / Frozen"
            warnings.append(f"تاخیر سرور ({delay_seconds} ثانیه) از آستانه مجاز بیشتر است.")
        elif delay_seconds > self.delayed_threshold_seconds:
            status_flag = "🟡 Delayed"
        if last_price is None or timezone_assumed:
            status_flag = "⚪ Unknown"

        # جهش نامتعارف قیمت بین دو اسنپ‌شات (Bad Tick)
        if last_price and symbol_fa in self.last_seen_prices:
            prev_price = self.last_seen_prices[symbol_fa]
            if prev_price > 0:
                change_pct = abs((last_price - prev_price) / prev_price) * 100.0
                if change_pct > 10.0:
                    warnings.append(f"جهش مشکوک قیمت: {prev_price} -> {last_price} ({change_pct:.1f}%)")
        if last_price:
            self.last_seen_prices[symbol_fa] = last_price

        client_metrics = snapshot_client_metrics(snapshot)
        bp = client_metrics['buyer_power']
        return {
            "symbol": symbol_fa,
            "checked_at": now.strftime("%H:%M:%S"),
            "provider_time": provider_time_str,
            "delay_seconds": delay_seconds,
            "signed_price_age_seconds": signed_age_seconds,
            "provider_timezone_assumed": timezone_assumed,
            "status": status_flag,
            "last_price": last_price,
            "buyer_power": round(bp, 2) if bp is not None else None,
            "real_flow_estimate_rial": client_metrics['real_flow_estimate_rial'],
            "buy_volume_quality": client_metrics['buy_volume_quality'],
            "sell_volume_quality": client_metrics['sell_volume_quality'],
            "buy_volume_quality_label": client_metrics['buy_volume_quality_label'],
            "sell_volume_quality_label": client_metrics['sell_volume_quality_label'],
            "clock_coupling_verified": False,
            "warnings": warnings,
            "is_healthy": status_flag == "🟢 Fresh" and not warnings,
        }
