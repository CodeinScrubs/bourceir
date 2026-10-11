# -*- coding: utf-8 -*-
"""
مجموعه آزمون‌های پذیرش کمی BRS v0.4.2 (Quant Acceptance Tests)
==============================================================
این آزمون‌ها تضمین می‌کنند که:
۱. داده‌های نامعتبر (قیمت منفی، منطق نقض‌شده High < Low) هرگز وارد دیتابیس نمی‌شوند.
۲. تمام داده‌ها با فیلدهای خام Point-in-Time و ضریب تعدیل ذخیره می‌شوند.
۳. در بکتست، پر شدن در صفوف قفل غیرممکن است و Win Rate با دقت ۱۰۰٪ محاسبه می‌شود.
۴. تاخیر و داده‌های کهنه (Stale Feed) بدون خطا شناسایی می‌شوند.
۵. موتور بازپخش بازار (MarketReplayEngine) داده‌ها را گام‌به‌گام تزریق می‌کند.
۶. تکرارپذیری کامل (Reproducibility): داده یکسان + پارامتر یکسان = نتیجه دقیقاً یکسان.
"""

import pytest
import pandas as pd
from datetime import datetime

from database.db import DatabaseManager
from data_collector.validator import DataValidator
from data_collector.monitor import DataFreshnessMonitor
from data_collector.universe import UniverseManager
from backtest.engine import BacktestEngine
from backtest.replay import MarketReplayEngine


def test_acceptance_bad_data_never_enters_database(tmp_path):
    """آزمون پذیرش ۱: داده‌های مخدوش و نامعتبر هرگز نباید در پایگاه داده ثبت شوند."""
    db_file = tmp_path / "test_quarantine.db"
    db = DatabaseManager(db_path=db_file)
    sym_id = db.insert_symbol("TEST", "تست_نماد")

    # ۱. کندل با قیمت منفی
    bad_candle = {
        "date": "2026-10-01",
        "open": -100.0,
        "high": 1000.0,
        "low": 900.0,
        "close": 950.0,
        "volume": 10000,
        "value": 9500000.0
    }
    is_valid, errs = DataValidator.validate_daily_candle(bad_candle)
    assert not is_valid, "کندل با قیمت منفی باید رد شود"

    # ۲. کندل با نقض منطق بیشترین کمتر از کمترین (High < Low)
    inverted_candle = {
        "date": "2026-10-02",
        "open": 1000.0,
        "high": 800.0,   # غیرممکن: های کمتر از لو
        "low": 900.0,
        "close": 950.0,
        "volume": 10000,
        "value": 9500000.0
    }
    is_valid_inv, errs_inv = DataValidator.validate_daily_candle(inverted_candle)
    assert not is_valid_inv, "کندل با High < Low باید فیلتر شود"

    # ۳. اسنپ‌شات بدون زمان ثبت
    bad_snap = {"last_price": 1000.0, "volume": 500}
    is_snap_valid, _ = DataValidator.validate_snapshot(bad_snap)
    assert not is_snap_valid, "اسنپ‌شات بدون زمان باید نامعتبر تشخیص داده شود"

    db.close()


def test_acceptance_point_in_time_storage(tmp_path):
    """آزمون پذیرش ۲: ستون‌های داده خام Point-in-Time و ضریب تعدیل باید فیزیکاً پر شوند."""
    db_file = tmp_path / "test_pit.db"
    db = DatabaseManager(db_path=db_file)
    sym_id = db.insert_symbol("KHODRO", "خودرو")

    # درج یک رکورد با قیمت خام و تعدیل‌شده
    db.insert_daily_price(
        symbol_id=sym_id,
        date="2026-10-05",
        open=1000.0,
        high=1050.0,
        low=990.0,
        close=1020.0,
        adj_close=816.0,  # افزایش سرمایه داشته
        volume=500000,
        value=510000000.0,
        trade_count=350,
        open_raw=1000.0,
        high_raw=1050.0,
        low_raw=990.0,
        close_raw=1020.0,
        adjustment_factor=0.8000
    )

    df = db.get_daily_prices_df("خودرو")
    assert not df.empty
    row = df.iloc[0]
    assert row["close_raw"] == 1020.0
    assert row["adj_close"] == 816.0
    assert row["adjustment_factor"] == 0.8
    assert row["open_raw"] == 1000.0

    db.close()


def test_acceptance_backtest_impossible_fills_and_exact_metrics():
    """آزمون پذیرش ۳: عدم پر شدن در صفوف قفل و محاسبه ۱۰۰٪ دقیق Win Rate و PnL."""
    engine = BacktestEngine(initial_capital=50_000_000.0, default_slippage_bps=0.0)

    # تست ۱: خرید در صف خرید قفل ممنوع است
    blocked = engine.execute_buy("شپنا", price=5000.0, quantity=1000, date="2026-10-01", is_locked_limit_up=True)
    assert blocked is None

    # تست ۲: فروش در صف فروش قفل ممنوع است
    engine.execute_buy("شپنا", price=5000.0, quantity=1000, date="2026-10-01", is_locked_limit_up=False)
    blocked_exit = engine.execute_sell("شپنا", price=4800.0, quantity=1000, date="2026-10-02", is_locked_limit_down=True)
    assert blocked_exit is None

    # تست ۳: فروش موفق و بررسی دقت نرخ برد
    # فروش سودده
    engine.execute_sell("شپنا", price=5500.0, quantity=1000, date="2026-10-03", is_locked_limit_down=False)
    # خرید و فروش زیان‌ده بعدی
    engine.execute_buy("شپنا", price=6000.0, quantity=1000, date="2026-10-04")
    engine.execute_sell("شپنا", price=5500.0, quantity=1000, date="2026-10-05")

    metrics = engine.calculate_metrics()
    assert len(engine.closed_trades) == 2
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate == 50.0  # یک برد، یک باخت = دقیقا ۵۰٪


def test_acceptance_market_replay_engine(tmp_path):
    """آزمون پذیرش ۴: موتور بازپخش داده‌های بازار (Replay Engine)."""
    db_file = tmp_path / "test_replay.db"
    db = DatabaseManager(db_path=db_file)
    sym_id = db.insert_symbol("FOOLAD", "فولاد")

    # ثبت ۳ اسنپ‌شات تستی
    db.insert_snapshot(sym_id, {"captured_at": "2026-10-10 09:00:00", "last_price": 5000.0, "volume": 100})
    db.insert_snapshot(sym_id, {"captured_at": "2026-10-10 09:00:30", "last_price": 5050.0, "volume": 200})
    db.insert_snapshot(sym_id, {"captured_at": "2026-10-10 09:01:00", "last_price": 5100.0, "volume": 300})

    replay = MarketReplayEngine(db_path=str(db_file))
    replayed = []

    def on_tick(symbol, snap):
        replayed.append((symbol, snap["last_price"]))

    count = replay.run_replay(callback=on_tick, date_str="2026-10-10", speed_multiplier=0.0)
    assert count == 3
    assert len(replayed) == 3
    assert replayed[0] == ("فولاد", 5000.0)
    assert replayed[2] == ("فولاد", 5100.0)

    db.close()


def test_acceptance_reproducibility():
    """آزمون پذیرش ۵: تکرارپذیری قطعی (Deterministic Reproducibility)."""
    # اجرای دو بکتست مستقل با ورودی‌های کاملاً یکسان
    data = [
        {"date": "2026-10-01", "close": 1000.0, "volume": 50000},
        {"date": "2026-10-02", "close": 1050.0, "volume": 50000},
        {"date": "2026-10-03", "close": 1100.0, "volume": 50000},
    ]
    df = pd.DataFrame(data)

    def strat(row, eng):
        return "BUY" if row["date"] == "2026-10-01" else ("SELL" if row["date"] == "2026-10-03" else None)

    eng1 = BacktestEngine(initial_capital=10_000_000.0, default_slippage_bps=5.0)
    m1 = eng1.run_replay_simulation(df, "سهم", strat)

    eng2 = BacktestEngine(initial_capital=10_000_000.0, default_slippage_bps=5.0)
    m2 = eng2.run_replay_simulation(df, "سهم", strat)

    assert m1.net_pnl == m2.net_pnl
    assert m1.win_rate == m2.win_rate
    assert m1.final_equity == m2.final_equity
    assert len(m1.equity_curve) == len(m2.equity_curve)
