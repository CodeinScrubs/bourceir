# -*- coding: utf-8 -*-
"""
موتور شبیه‌سازی و آزمون تاریخی معاملات (Backtest Engine)
با پشتیبانی دقیق از قوانین مالی بورس اوراق بهادار تهران و فرابورس ایران:
- نرخ کارمزد خرید: ۰.۳۷۱۲ درصد
- نرخ کارمزد و مالیات فروش: ۰.۸۸۰۰ درصد (شامل ۰.۵ درصد مالیات مقطوع نقل و انتقال)
- مجموع هزینه رفت و برگشت: حدود ۱.۲۵ درصد
"""

import math
import logging
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np

from backtest.models import Order, Trade, Position, OrderSide, PerformanceMetrics

logger = logging.getLogger(__name__)


class BacktestEngine:
    """موتور اجرای بکتست استراتژی‌های معاملاتی در بازار بورس ایران."""

    # کارمزدهای رسمی معاملات سهام در بورس تهران
    BUY_COMMISSION_RATE = 0.003712   # ۰.۳۷۱۲٪ کارمزد خرید
    SELL_COMMISSION_RATE = 0.008800  # ۰.۸۸۰۰٪ کارمزد و مالیات فروش

    def __init__(self, initial_capital: float = 100_000_000.0):
        """
        مقداردهی اولیه سرمایه به ریال (پیش‌فرض: ۱۰۰ میلیون ریال = ۱۰ میلیون تومان).
        """
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict[str, Any]] = []

    def execute_buy(self, symbol_fa: str, price: float, quantity: int, date: str) -> Optional[Trade]:
        """
        اجرای سفارش خرید با کسر کارمزد رسمی خرید بورس تهران.
        """
        if quantity <= 0 or price <= 0:
            return None

        gross_value = price * quantity
        commission = gross_value * self.BUY_COMMISSION_RATE
        total_required = gross_value + commission

        if self.cash < total_required:
            logger.warning("موجودی نقد کافی نیست: نیاز به %s ریال، موجودی: %s ریال", total_required, self.cash)
            # تعدیل حجم بر اساس حداکثر موجودی نقد
            max_qty = int(self.cash / (price * (1 + self.BUY_COMMISSION_RATE)))
            if max_qty <= 0:
                return None
            quantity = max_qty
            gross_value = price * quantity
            commission = gross_value * self.BUY_COMMISSION_RATE
            total_required = gross_value + commission

        self.cash -= total_required

        trade = Trade(
            symbol_fa=symbol_fa,
            side=OrderSide.BUY,
            quantity=quantity,
            price=price,
            gross_value=gross_value,
            commission=commission,
            net_value=total_required,
            date=date
        )
        self.trades.append(trade)

        # به‌روزرسانی پوزیشن
        if symbol_fa not in self.positions:
            self.positions[symbol_fa] = Position(symbol_fa=symbol_fa)

        pos = self.positions[symbol_fa]
        new_total_qty = pos.quantity + quantity
        pos.total_cost += total_required
        pos.average_entry_price = pos.total_cost / new_total_qty if new_total_qty > 0 else 0.0
        pos.quantity = new_total_qty
        pos.trades.append(trade)

        return trade

    def execute_sell(self, symbol_fa: str, price: float, quantity: int, date: str) -> Optional[Trade]:
        """
        اجرای سفارش فروش با کسر کارمزد و مالیات فروش بورس تهران.
        """
        if symbol_fa not in self.positions or self.positions[symbol_fa].quantity <= 0:
            logger.warning("پوزیشن باز برای فروش نماد '%s' وجود ندارد.", symbol_fa)
            return None

        pos = self.positions[symbol_fa]
        sell_qty = min(quantity, pos.quantity)

        gross_value = price * sell_qty
        commission = gross_value * self.SELL_COMMISSION_RATE
        net_received = gross_value - commission

        self.cash += net_received

        trade = Trade(
            symbol_fa=symbol_fa,
            side=OrderSide.SELL,
            quantity=sell_qty,
            price=price,
            gross_value=gross_value,
            commission=commission,
            net_value=net_received,
            date=date
        )
        self.trades.append(trade)

        # محاسبه سود یا زیان شناسایی‌شده
        cost_of_sold_shares = pos.average_entry_price * sell_qty
        realized_pnl = net_received - cost_of_sold_shares
        pos.realized_pnl += realized_pnl

        pos.quantity -= sell_qty
        if pos.quantity == 0:
            pos.total_cost = 0.0
            pos.average_entry_price = 0.0
        else:
            pos.total_cost -= cost_of_sold_shares

        pos.trades.append(trade)
        return trade

    def calculate_metrics(self) -> PerformanceMetrics:
        """
        محاسبه شاخص‌های آماری و عملکرد بکتست.
        """
        if not self.trades:
            return PerformanceMetrics()

        sell_trades = [t for t in self.trades if t.side == OrderSide.SELL]
        buy_trades = [t for t in self.trades if t.side == OrderSide.BUY]

        total_trades = len(sell_trades)
        winning_trades = 0
        losing_trades = 0
        gross_profit = 0.0
        gross_loss = 0.0
        total_commissions = sum(t.commission for t in self.trades)

        # محاسبه سود هر جفت معامله
        for pos in self.positions.values():
            if pos.realized_pnl > 0:
                winning_trades += 1
                gross_profit += pos.realized_pnl
            elif pos.realized_pnl < 0:
                losing_trades += 1
                gross_loss += abs(pos.realized_pnl)

        total_closed = winning_trades + losing_trades
        win_rate = (winning_trades / total_closed * 100.0) if total_closed > 0 else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        net_pnl = gross_profit - gross_loss

        return PerformanceMetrics(
            total_trades=len(self.trades),
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=round(win_rate, 2),
            gross_profit=round(gross_profit, 2),
            gross_loss=round(gross_loss, 2),
            net_pnl=round(net_pnl, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=0.0,
            total_commissions_paid=round(total_commissions, 2),
            sharpe_ratio=0.0
        )
