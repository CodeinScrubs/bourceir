# -*- coding: utf-8 -*-
"""
آزمون‌های واحد ماژول پایش سلامت و تازگی داده‌های بورس (DataFreshnessMonitor)
بررسی دقت محاسبه تاخیر، تشخیص داده فریز شده، فیلتر جهش‌های قیمت (Bad Tick) و قدرت خریدار.
"""

from datetime import datetime, timedelta
import pytest
from data_collector.monitor import DataFreshnessMonitor


def test_monitor_fresh_data():
    """بررسی داده‌های کاملاً تازه (زیر ۴۵ ثانیه تاخیر)."""
    monitor = DataFreshnessMonitor(stale_threshold_seconds=90)
    now = datetime.now()
    fresh_time = (now - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")

    snapshot = {
        "last_price": 2500.0,
        "provider_timestamp": fresh_time,
        "real_buy_volume": 100000,
        "real_buy_count": 10,
        "real_sell_volume": 100000,
        "real_sell_count": 20,
    }

    report = monitor.inspect_snapshot("خودرو", snapshot)
    assert report["symbol"] == "خودرو"
    assert report["status"] == "🟢 Fresh"
    assert report["is_healthy"] is True
    assert report["delay_seconds"] <= 15
    # سرانه خرید: 10,000 / سرانه فروش: 5,000 -> نسبت = 2.0
    assert report["buyer_power"] == 2.0


def test_monitor_delayed_data():
    """بررسی داده‌های دارای تاخیر متوسط (بین ۴۵ تا ۹۰ ثانیه)."""
    monitor = DataFreshnessMonitor(stale_threshold_seconds=90)
    now = datetime.now()
    delayed_time = (now - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")

    snapshot = {
        "last_price": 2500.0,
        "provider_timestamp": delayed_time,
    }

    report = monitor.inspect_snapshot("فولاد", snapshot)
    assert report["status"] == "🟡 Delayed"
    assert report["is_healthy"] is False
    assert 55 <= report["delay_seconds"] <= 65


def test_monitor_stale_data():
    """بررسی داده‌های منقضی و کهنه (بیش از ۹۰ ثانیه تاخیر)."""
    monitor = DataFreshnessMonitor(stale_threshold_seconds=90)
    now = datetime.now()
    stale_time = (now - timedelta(seconds=150)).strftime("%Y-%m-%d %H:%M:%S")

    snapshot = {
        "last_price": 2500.0,
        "provider_timestamp": stale_time,
    }

    report = monitor.inspect_snapshot("شپنا", snapshot)
    assert "🔴 Stale" in report["status"]
    assert report["is_healthy"] is False
    assert len(report["warnings"]) > 0


def test_monitor_bad_tick_detection():
    """تشخیص جهش ناگهانی و غیرعادی قیمت (Bad Tick)."""
    monitor = DataFreshnessMonitor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # اولین اسنپ‌شات قیمت عادی
    monitor.inspect_snapshot("وبملت", {"last_price": 1000.0, "provider_timestamp": now_str})

    # اسنپ‌شات دوم با جهش غیرعادی ۱۵ درصدی در یک لحظه
    report2 = monitor.inspect_snapshot("وبملت", {"last_price": 1150.0, "provider_timestamp": now_str})
    assert any("جهش مشکوک قیمت" in w for w in report2["warnings"])


def test_monitor_frozen_timestamp():
    """تشخیص کَش شدن سرور TSETMC زمانی که تایم‌استمپ تغییر نمی‌کند."""
    monitor = DataFreshnessMonitor()
    fixed_time = "2026-10-11 09:30:00"

    monitor.inspect_snapshot("کگل", {"last_price": 5000.0, "provider_timestamp": fixed_time})
    # ارسال همان زمان ثابت در تیک بعدی
    report2 = monitor.inspect_snapshot("کگل", {"last_price": 5000.0, "provider_timestamp": fixed_time})
    assert any("تایم‌استمپ سرور تغییر نکرده است" in w for w in report2["warnings"])
