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
    بررسی دقیق کسر کارمزدهای قانونی بورس تهران در خرید و فروش
    """
    engine = BacktestEngine(initial_capital=100_000_000.0)  # ۱۰۰ میلیون ریال

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
    # سود خالص باید بعد از کسر هر دو کارمزد محاسبه شده باشد
    expected_net = (11_000_000.0 - expected_sell_commission) - (10_000_000.0 + expected_buy_commission)
    assert abs(metrics.net_pnl - expected_net) < 2.0


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
