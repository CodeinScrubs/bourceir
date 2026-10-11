# -*- coding: utf-8 -*-
"""
تست‌های واحد موتور بکتستینگ (Backtest Engine Tests)
اعتبارسنجی کارمزد خرید و فروش بورس ایران، سود/زیان خالص و شاخص‌های عملکرد.
"""

import sys
from pathlib import Path
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.engine import BacktestEngine
from backtest.models import OrderSide


def test_backtest_commission_calculation():
    """
    بررسی دقیق کسر کارمزدهای قانونی بورس تهران در خرید و فروش (بدون لغزش)
    """
    engine = BacktestEngine(initial_capital=100_000_000.0, default_slippage_bps=0.0)

    # ۱. خرید ۱۰۰۰ سهم به قیمت ۱۰,۰۰۰ ریال (ارزش ناخالص: ۱۰ میلیون ریال)
    buy_trade = engine.execute_buy(
        symbol_fa="فولاد",
        price=10_000.0,
        quantity=1000,
        date="2026-10-01"
    )
    assert buy_trade is not None
    assert buy_trade.gross_value == 10_000_000.0
    # کارمزد خرید: ۰.۳۷۱۲٪ = ۳۷,۱۲۰ ریال
    expected_buy_commission = 10_000_000.0 * 0.003712
    assert abs(buy_trade.commission - expected_buy_commission) < 1.0

    # ۲. فروش همان ۱۰۰۰ سهم به قیمت ۱۱,۰۰۰ ریال (۱۰٪ سود اسمی، ارزش ناخالص: ۱۱ میلیون ریال)
    sell_trade = engine.execute_sell(
        symbol_fa="فولاد",
        price=11_000.0,
        quantity=1000,
        date="2026-10-05"
    )
    assert sell_trade is not None
    assert sell_trade.gross_value == 11_000_000.0
    # کارمزد و مالیات فروش: ۰.۸۸٪ = ۹۶,۸۰۰ ریال
    expected_sell_commission = 11_000_000.0 * 0.008800
    assert abs(sell_trade.commission - expected_sell_commission) < 1.0

    # ۳. بررسی متریک‌های بکتست
    metrics = engine.calculate_metrics()
    assert metrics.winning_trades == 1
    assert metrics.win_rate == 100.0
    assert metrics.profit_factor > 1.0
    expected_net = (11_000_000.0 - expected_sell_commission) - (10_000_000.0 + expected_buy_commission)
    assert abs(metrics.net_pnl - expected_net) < 2.0


def test_backtest_slippage_and_queue_fill():
    """
    بررسی مدل واقع‌گرایانه قفل صفوف و لغزش قیمت در بورس تهران
    """
    engine = BacktestEngine(
        initial_capital=100_000_000.0,
        max_volume_participation_pct=0.05,
        default_slippage_bps=20.0  # ۰.۲٪ اسپرد
    )

    # ۱. سهم در صف خرید قفل است؛ خرید نباید پر شود
    blocked_buy = engine.execute_buy(
        symbol_fa="خودرو",
        price=5000.0,
        quantity=1000,
        date="2026-10-02",
        is_locked_limit_up=True
    )
    assert blocked_buy is None, "سفارش در صف خرید قفل نباید اجرا شود"

    # ۲. خرید موفق با لغزش و قید نقدشوندگی
    # حجم روزانه ۲۰,۰۰۰ سهم است؛ سقف ۵٪ یعنی حداکثر ۱,۰۰۰ سهم قابل معامله است
    buy_trade = engine.execute_buy(
        symbol_fa="فولاد",
        price=10_000.0,
        quantity=5000,  # سفارش ۵۰۰۰ تایی
        date="2026-10-02",
        daily_volume=20_000
    )
    assert buy_trade is not None
    assert buy_trade.quantity == 1000, "حجم سفارش باید بر اساس سقف ۵٪ حجم روزانه تعدیل شود"
    assert buy_trade.price > 10_000.0, "قیمت موثر خرید به دلیل لغزش باید بیشتر از قیمت تابلو باشد"

    # ۳. سهم در صف فروش قفل است؛ فروش نباید انجام شود
    blocked_sell = engine.execute_sell(
        symbol_fa="فولاد",
        price=9500.0,
        quantity=1000,
        date="2026-10-03",
        is_locked_limit_down=True
    )
    assert blocked_sell is None, "سفارش خروج در صف فروش قفل نباید پر شود"


def test_extended_database_tables(tmp_path):
    """
    بررسی ایجاد جداول تکمیلی (اقدامات شرکتی، تقویم، ریزمعاملات، آزمایش‌ها) در دیتابیس
    """
    from database.db import DatabaseManager

    db_path = tmp_path / "test_extended.db"
    db = DatabaseManager(db_path=db_path)
    db.init_db()

    # بررسی ثبت یک آزمایش در جدول experiments
    exp_id = db.log_experiment(
        experiment_id="EXP-TEST-001",
        strategy_name="RSI Scalp Test",
        symbols_tested="فولاد,خودرو",
        parameters_json='{"rsi_period": 14, "buy_threshold": 30}',
        metrics_json='{"win_rate": 65.0, "profit_factor": 1.45}'
    )
    assert exp_id is not None

    experiments = db.get_experiments()
    assert len(experiments) == 1
    assert experiments[0]["experiment_id"] == "EXP-TEST-001"

    db.close()


def test_win_rate_accuracy_with_profit_and_loss():
    """
    بررسی حل باگ تجمیع پوزیشن‌ها: یک معامله برنده و یک معامله بازنده روی یک نماد
    باید دقیقاً نرخ برد ۵۰٪ (Win Rate = 50.0%) گزارش کند نه ۱۰۰٪.
    """
    engine = BacktestEngine(initial_capital=100_000_000.0, default_slippage_bps=0.0)

    # معامله ۱ (برنده): خرید در ۱۰,۰۰۰ و فروش در ۱۲,۰۰۰ (سود ۲۰٪)
    engine.execute_buy(symbol_fa="فولاد", price=10_000.0, quantity=1000, date="2026-10-01")
    engine.execute_sell(symbol_fa="فولاد", price=12_000.0, quantity=1000, date="2026-10-02")

    # معامله ۲ (بازنده): خرید در ۱۲,۰۰۰ و فروش در ۱۰,۰۰۰ (زیان)
    engine.execute_buy(symbol_fa="فولاد", price=12_000.0, quantity=1000, date="2026-10-03")
    engine.execute_sell(symbol_fa="فولاد", price=10_000.0, quantity=1000, date="2026-10-04")

    metrics = engine.calculate_metrics()
    assert len(engine.closed_trades) == 2, "باید دقیقا ۲ معامله بسته‌شده ثبت شده باشد"
    assert metrics.winning_trades == 1, "باید ۱ معامله برنده باشد"
    assert metrics.losing_trades == 1, "باید ۱ معامله بازنده باشد"
    assert metrics.win_rate == 50.0, f"نرخ برد باید ۵۰٪ باشد اما {metrics.win_rate} گزارش شد"


def test_historical_replay_simulation():
    """
    بررسی اجرای حلقه شبیه‌سازی گام‌به‌گام تاریخی (Replay Loop) و ردیابی منحنی رشد سرمایه
    """
    import pandas as pd
    engine = BacktestEngine(initial_capital=50_000_000.0, default_slippage_bps=0.0)

    # ساخت سری داده تاریخی شبیه‌سازی‌شده (۵ روز)
    data = [
        {"date": "2026-10-01", "close": 1000.0, "volume": 100000},
        {"date": "2026-10-02", "close": 1050.0, "volume": 100000},
        {"date": "2026-10-03", "close": 1100.0, "volume": 100000},
        {"date": "2026-10-04", "close": 1020.0, "volume": 100000},
        {"date": "2026-10-05", "close": 1080.0, "volume": 100000},
    ]
    df = pd.DataFrame(data)

    def simple_strategy(row, eng):
        if row["date"] == "2026-10-01":
            return "BUY"
        elif row["date"] == "2026-10-03":
            return "SELL"
        return None

    metrics = engine.run_replay_simulation(df, "خودرو", simple_strategy)
    assert len(metrics.equity_curve) == 5, "منحنی ارزش سرمایه باید به ازای هر ۵ روز ثبت شده باشد"
    assert metrics.winning_trades == 1
    assert metrics.win_rate == 100.0
    assert metrics.final_equity > metrics.initial_capital
