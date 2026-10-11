# -*- coding: utf-8 -*-
"""
آزمون‌های پایش سلامت و تازگی داده (همه زمان‌ها به وقت تهران، مستقل از ساعت محلی سرور)
"""

from datetime import timedelta

from core.timeutils import now_tehran
from data_collector.monitor import DataFreshnessMonitor, buyer_power


def _stamp(seconds_ago: int) -> str:
    return (now_tehran() - timedelta(seconds=seconds_ago)).isoformat()


def test_monitor_fresh_data_and_buyer_power():
    report = DataFreshnessMonitor(stale_threshold_seconds=90).inspect_snapshot("خودرو", {
        "last_price": 2500.0, "provider_timestamp": _stamp(10),
        "real_buy_volume": 100000, "real_buy_count": 10, "real_sell_volume": 100000, "real_sell_count": 20,
    })
    assert report["status"] == "🟢 Fresh" and report["is_healthy"] is True
    assert report["delay_seconds"] <= 15
    assert report["buyer_power"] == 2.0  # سرانه خرید ۱۰٬۰۰۰ ÷ سرانه فروش ۵٬۰۰۰


def test_monitor_delayed_data():
    report = DataFreshnessMonitor(stale_threshold_seconds=90).inspect_snapshot(
        "فولاد", {"last_price": 2500.0, "provider_timestamp": _stamp(60)})
    assert report["status"] == "🟡 Delayed" and not report["is_healthy"]
    assert 55 <= report["delay_seconds"] <= 65


def test_monitor_stale_data():
    report = DataFreshnessMonitor(stale_threshold_seconds=90).inspect_snapshot(
        "شپنا", {"last_price": 2500.0, "provider_timestamp": _stamp(150)})
    assert "🔴 Stale" in report["status"] and report["warnings"]


def test_naive_timestamp_is_interpreted_as_tehran_time():
    naive = (now_tehran() - timedelta(seconds=20)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    report = DataFreshnessMonitor().inspect_snapshot("وبملت", {"last_price": 1.0, "provider_timestamp": naive})
    assert report["delay_seconds"] <= 25


def test_monitor_bad_tick_detection():
    monitor = DataFreshnessMonitor()
    monitor.inspect_snapshot("وبملت", {"last_price": 1000.0, "provider_timestamp": _stamp(1)})
    report = monitor.inspect_snapshot("وبملت", {"last_price": 1150.0, "provider_timestamp": _stamp(0)})
    assert any("جهش مشکوک قیمت" in w for w in report["warnings"])


def test_monitor_frozen_timestamp():
    monitor = DataFreshnessMonitor()
    fixed = _stamp(5)
    monitor.inspect_snapshot("کگل", {"last_price": 5000.0, "provider_timestamp": fixed})
    report = monitor.inspect_snapshot("کگل", {"last_price": 5000.0, "provider_timestamp": fixed})
    assert any("تایم‌استمپ سرور تغییر نکرده است" in w for w in report["warnings"])


def test_buyer_power_without_data_is_none():
    assert buyer_power({"real_buy_volume": 10}) is None


def test_missing_server_time_is_unknown_not_fresh():
    report = DataFreshnessMonitor().inspect_snapshot("فولاد", {"last_price": 3880.0})
    assert report["status"] == "⚪ Unknown" and not report["is_healthy"]
    assert report["delay_seconds"] is None and report["provider_time"] is None
    assert report["buyer_power"] is None and report["warnings"]   # نسخه قبل: «تازه» و قدرت خریدار ساختگی ۱
