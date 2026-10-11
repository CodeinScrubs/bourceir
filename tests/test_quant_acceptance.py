# -*- coding: utf-8 -*-
"""
آزمون‌های پذیرش کمی BRS (Quant Acceptance Tests)

۱. داده نامعتبر هرگز وارد دیتابیس نمی‌شود و اسنپ‌شات بدون زمان ثبت رد می‌شود
۲. داده خام Point-in-Time (قیمت پایانی رسمی + قیمت مرجع) ذخیره و بازده بدون نگاه به آینده محاسبه می‌شود
۳. اسنپ‌شات تولیدشده توسط provider واقعاً از اعتبارسنجی عبور می‌کند (باگ نسخه 0.4.2)
۴. موتور بازپخش بازار اسنپ‌شات‌ها را به ترتیب زمانی تزریق می‌کند
"""

import pytest

from backtest.replay import MarketReplayEngine
from core import market_rules
from data_collector.tsetmc import TsetmcProvider
from data_collector.validator import DataValidator


def test_acceptance_bad_data_never_enters_database():
    assert not DataValidator.validate_daily_candle(
        {"date": "2026-10-01", "open": -100.0, "high": 1000.0, "low": 900.0, "close": 950.0})[0]
    assert not DataValidator.validate_daily_candle(
        {"date": "2026-10-02", "open": 1000.0, "high": 800.0, "low": 900.0, "close": 950.0})[0]
    assert DataValidator.validate_daily_candle(
        {"date": "2026-10-10", "open": 5000.0, "high": 5200.0, "low": 4900.0, "close": 5100.0, "volume": 1})[0]
    assert not DataValidator.validate_snapshot({"last_price": 1000.0, "volume": 500})[0]


def test_acceptance_provider_snapshot_passes_validation(market_watch_rows, client_type_all):
    """نسخه 0.4.2: provider فیلد captured_at را نمی‌ساخت و همه اسنپ‌شات‌های واقعی رد می‌شدند."""
    snap = TsetmcProvider.snapshot_from_market_watch(
        market_watch_rows[0], client_type_all["46348559193224090"], "2026-10-11T10:00:00+03:30")
    is_valid, errors = DataValidator.validate_snapshot(snap)
    assert is_valid, errors


def test_acceptance_point_in_time_storage(db):
    sym_id = db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "1"})
    db.insert_daily_price(sym_id, "2025-03-11", 5500, 5560, 5450, 5530, volume=10, value=1.0,
                          close_final_raw=5530, ref_price_raw=5480)
    db.insert_daily_price(sym_id, "2025-03-12", 4000, 4100, 3950, 4050, volume=10, value=1.0,
                          close_final_raw=4000, ref_price_raw=3982)
    df = db.get_daily_prices_df("فولاد")
    assert list(df["close_raw"]) == [5530.0, 4050.0]           # آخرین معامله
    assert list(df["close_final_raw"]) == [5530.0, 4000.0]     # قیمت پایانی رسمی
    returns = market_rules.pit_daily_returns(df)
    assert returns.iloc[1] == pytest.approx(4000 / 3982 - 1)   # بدون افت کاذب ناشی از افزایش سرمایه


def test_acceptance_market_replay_engine(db, tmp_path):
    sym_id = db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "1"})
    for t, price in [("09:00:00", 5000.0), ("09:00:30", 5050.0), ("09:01:00", 5100.0)]:
        db.insert_snapshot(sym_id, {"captured_at": f"2026-10-10T{t}+03:30", "last_price": price, "volume": 100})
    replay = MarketReplayEngine(db_path=str(db.db_path))
    assert replay.get_available_replay_dates() == ["2026-10-10"]
    seen = []
    count = replay.run_replay(callback=lambda sym, snap: seen.append((sym, snap["last_price"])), date_str="2026-10-10")
    assert count == 3 and seen[0] == ("فولاد", 5000.0) and seen[-1] == ("فولاد", 5100.0)
    replay.db.close()
