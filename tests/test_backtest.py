# -*- coding: utf-8 -*-
"""
آزمون‌های موتور بکتست: کارمزد رسمی، صف قفل، بازه قیمت روز، اجرای روز بعد و متریک‌های دقیق
"""

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from config import settings


def test_commissions_come_from_config():
    assert BacktestEngine.BUY_COMMISSION_RATE == settings.FEES["buy"] == 0.003712
    assert BacktestEngine.SELL_COMMISSION_RATE == settings.FEES["sell"] == 0.0088


def test_backtest_commission_calculation():
    engine = BacktestEngine(allow_unverified_fills=True, initial_capital=100_000_000.0, default_slippage_bps=0.0)
    buy = engine.execute_buy("فولاد", price=10_000.0, quantity=1000, date="2026-10-01")
    assert buy.gross_value == 10_000_000.0
    assert buy.commission == pytest.approx(10_000_000.0 * 0.003712, abs=1.0)
    sell = engine.execute_sell("فولاد", price=11_000.0, quantity=1000, date="2026-10-05")
    assert sell.commission == pytest.approx(11_000_000.0 * 0.0088, abs=1.0)

    metrics = engine.calculate_metrics()
    expected_net = (11_000_000.0 * (1 - 0.0088)) - (10_000_000.0 * (1 + 0.003712))
    assert metrics.winning_trades == 1 and metrics.win_rate == 100.0
    assert metrics.net_pnl == pytest.approx(expected_net, abs=2.0)
    assert engine.closed_trades[0].entry_date == "2026-10-01"


def test_locked_queues_and_participation_cap():
    engine = BacktestEngine(allow_unverified_fills=True, initial_capital=100_000_000.0, default_slippage_bps=20.0)
    assert engine.execute_buy("خودرو", 5000.0, 1000, "2026-10-02", is_locked_limit_up=True) is None
    trade = engine.execute_buy("فولاد", 10_000.0, 5000, "2026-10-02", daily_volume=20_000)
    assert trade.quantity == 1000  # سقف ۵٪ حجم روز
    assert trade.price > 10_000.0
    assert engine.execute_sell("فولاد", 9500.0, 1000, "2026-10-03", is_locked_limit_down=True) is None
    assert engine.blocked_orders == 2


def test_fill_price_never_outside_day_range():
    """باگ نسخه قبل: لغزش، قیمت خرید را به ۳۸۸۳.۸۸ (بالاتر از سقف مجاز ۳۸۸۰) می‌رساند."""
    engine = BacktestEngine(allow_unverified_fills=True, initial_capital=100_000_000.0, default_slippage_bps=10.0)
    trade = engine.execute_buy("فولاد", 3880.0, 1000, "2026-10-06", day_low=3880.0, day_high=3880.0)
    assert trade.price == 3880.0


def test_win_rate_accuracy_and_entry_date_after_reentry():
    engine = BacktestEngine(allow_unverified_fills=True, initial_capital=100_000_000.0, default_slippage_bps=0.0)
    engine.execute_buy("فولاد", 10_000.0, 1000, "2026-10-01")
    engine.execute_sell("فولاد", 12_000.0, 1000, "2026-10-02")
    engine.execute_buy("فولاد", 12_000.0, 1000, "2026-10-03")
    engine.execute_sell("فولاد", 10_000.0, 1000, "2026-10-04")
    metrics = engine.calculate_metrics()
    assert (metrics.winning_trades, metrics.losing_trades, metrics.win_rate) == (1, 1, 50.0)
    # تاریخ ورود معامله دوم باید ۰۳ باشد، نه اولین خرید تاریخچه
    assert [t.entry_date for t in engine.closed_trades] == ["2026-10-01", "2026-10-03"]


def test_replay_executes_on_next_day_without_lookahead():
    """سیگنال در پایان روز t، اجرا در روز t+1."""
    engine = BacktestEngine(allow_unverified_fills=True, initial_capital=50_000_000.0, default_slippage_bps=0.0)
    df = pd.DataFrame([
        {"date": "2026-10-01", "close": 1000.0, "volume": 100_000},
        {"date": "2026-10-02", "close": 1050.0, "volume": 100_000},
        {"date": "2026-10-03", "close": 1100.0, "volume": 100_000},
        {"date": "2026-10-04", "close": 1020.0, "volume": 100_000},
        {"date": "2026-10-05", "close": 1080.0, "volume": 100_000},
    ])

    def strategy(row, eng):
        return {"2026-10-01": "BUY", "2026-10-03": "SELL"}.get(row["date"])

    metrics = engine.run_replay_simulation(df, "خودرو", strategy)
    buy, sell = engine.trades
    # سفارش به سقف ۵٪ حجم روز رسیده؛ اثر حجم (Market Impact) = ۰.۰۵ × ۰.۱ = ۰.۵٪
    assert buy.date == "2026-10-02" and buy.quantity == 5000
    assert buy.price == pytest.approx(1050.0 * 1.005)
    assert sell.date == "2026-10-04" and sell.price == pytest.approx(1020.0 * 0.995)
    assert metrics.losing_trades == 1 and len(metrics.equity_curve) == 5


def test_replay_blocks_buy_on_locked_day_and_retries_locked_sell():
    """داده واقعی فولاد: ۶ و ۷ مهر صف خرید قفل. خرید پر نمی‌شود؛ فروش در صف فروش قفل منتظر می‌ماند."""
    rows = [
        # date, low, high, close_raw, close_final, ref, volume, value
        ("2026-10-05", 3590, 3800, 3800, 3770, 3690, 4_227_500_464, 15_928_799_718_890),
        ("2026-10-06", 3880, 3880, 3880, 3880, 3770, 2_365_867_335, 9_179_565_259_800),
        ("2026-10-07", 3990, 3990, 3990, 3990, 3880, 1_581_454_223, 6_310_002_349_770),
        ("2026-10-10", 3900, 4050, 4000, 4000, 3990, 1_000_000_000, 3_980_000_000_000),
        ("2026-10-11", 3880, 3880, 3880, 3880, 4000, 500_000_000, 1_940_000_000_000),   # صف فروش قفل
        ("2026-10-12", 3800, 3900, 3850, 3850, 3880, 900_000_000, 3_465_000_000_000),
    ]
    df = pd.DataFrame(rows, columns=["date", "low_raw", "high_raw", "close_raw", "close_final_raw",
                                     "ref_price_raw", "volume", "value"])
    engine = BacktestEngine(initial_capital=100_000_000.0, default_slippage_bps=10.0)
    signals = {"2026-10-05": "BUY", "2026-10-06": "BUY", "2026-10-07": "BUY", "2026-10-10": "SELL"}
    metrics = engine.run_replay_simulation(df, "فولاد", lambda row, eng: signals.get(row["date"]))

    buy = engine.trades[0]
    assert buy.date == "2026-10-10"                       # ۰۶ و ۰۷ قفل بودند
    assert 3900 <= buy.price <= 4050                      # در بازه واقعی همان روز
    sell = engine.trades[1]
    assert sell.date == "2026-10-12"                      # ۱۱ مهر صف فروش قفل بود؛ روز بعد خارج شد
    assert metrics.blocked_orders == 3


def test_final_equity_is_mark_to_market():
    df = pd.DataFrame([
        {"date": "d1", "close": 1000.0, "volume": 1_000_000},
        {"date": "d2", "close": 1000.0, "volume": 1_000_000},
        {"date": "d3", "close": 2000.0, "volume": 1_000_000},
    ])
    engine = BacktestEngine(allow_unverified_fills=True, initial_capital=1_000_000.0, default_slippage_bps=0.0)
    metrics = engine.run_replay_simulation(df, "x", lambda row, eng: "BUY" if row["date"] == "d1" else None)
    held = engine.positions["x"].quantity
    assert metrics.final_equity == pytest.approx(engine.cash + held * 2000.0)
    assert metrics.final_equity > 1_800_000  # سهم دوبرابر شده و پوزیشن باز با قیمت روز ارزش‌گذاری می‌شود


def test_reproducibility():
    df = pd.DataFrame([{"date": f"2026-10-0{i}", "close": 1000.0 + 50 * i, "volume": 50_000} for i in range(1, 6)])

    def strategy(row, eng):
        return "BUY" if row["date"] == "2026-10-01" else ("SELL" if row["date"] == "2026-10-03" else None)

    runs = [BacktestEngine(allow_unverified_fills=True, initial_capital=10_000_000.0, default_slippage_bps=5.0).run_replay_simulation(df, "s", strategy)
            for _ in range(2)]
    assert runs[0].net_pnl == runs[1].net_pnl
    assert runs[0].final_equity == runs[1].final_equity
    assert runs[0].equity_curve == runs[1].equity_curve


def test_experiment_logging(db):
    exp_id = db.log_experiment(experiment_id="EXP-TEST-001", strategy_name="RSI Scalp Test", symbols_tested="فولاد",
                               parameters_json='{"rsi_period": 14}', metrics_json='{"win_rate": 65.0}',
                               dataset_version="v2.0")
    assert exp_id is not None
    exp = db.get_experiments()[0]
    assert exp["experiment_id"] == "EXP-TEST-001" and exp["dataset_version"] == "v2.0"
