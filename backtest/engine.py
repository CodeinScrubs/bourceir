# -*- coding: utf-8 -*-
"""
موتور شبیه‌سازی و آزمون تاریخی معاملات (Backtest Engine v0.4)
با پشتیبانی دقیق از واقعیت‌های معاملاتی بورس اوراق بهادار تهران و فرابورس ایران:
- نرخ کارمزد قانونی خرید: ۰.۳۷۱۲ درصد
- نرخ کارمزد و مالیات فروش: ۰.۸۸۰۰ درصد (مجموع رفت و برگشت: ۱.۲۵۱۲ درصد)
- مدل شبیه‌سازی نوبت و پر شدن در صفوف خرید و فروش (Queue Fill Model)
- مدل لغزش قیمت بر اساس اندازه معامله و اسپرد (Slippage Model)
- محدودیت نقدشوندگی (Liquidity Constraints / Participation Rate)
"""

import math
import logging
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np

from backtest.models import Order, Trade, Position, OrderSide, PerformanceMetrics

logger = logging.getLogger(__name__)


class BacktestEngine:
    """موتور اجرای بکتست واقع‌گرایانه با لحاظ ریزساختار بازار بورس ایران."""

    # کارمزدهای رسمی معاملات سهام در بورس تهران
    BUY_COMMISSION_RATE = 0.003712   # ۰.۳۷۱۲٪ کارمزد خرید
    SELL_COMMISSION_RATE = 0.008800  # ۰.۸۸۰۰٪ کارمزد و مالیات فروش

    def __init__(
        self,
        initial_capital: float = 100_000_000.0,
        max_volume_participation_pct: float = 0.05,  # حداکثر ۵٪ از حجم روزانه قابل معامله است
        default_slippage_bps: float = 10.0           # ۱۰ پیپ یا ۰.۱٪ لغزش پیش‌فرض اسپرد
    ):
        """
        مقداردهی اولیه سرمایه و قیود معاملاتی.
        """
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.max_volume_participation_pct = max_volume_participation_pct
        self.default_slippage_bps = default_slippage_bps
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict[str, Any]] = []

    def calculate_slippage_price(
        self,
        base_price: float,
        side: OrderSide,
        quantity: int,
        daily_volume: Optional[int] = None
    ) -> float:
        """
        محاسبه قیمت واقعی اجرا با احتساب اسپرد عرضه/تقاضا و فشار حجمی معامله (Market Impact).
        """
        # اسپرد پایه
        base_slippage = (self.default_slippage_bps / 10000.0)

        # فشار حجمی نسبت به حجم روزانه
        impact = 0.0
        if daily_volume and daily_volume > 0:
            participation = quantity / daily_volume
            impact = min(participation * 0.1, 0.02)  # حداکثر ۲٪ لغزش به ازای حجم سنگین

        total_slippage = base_slippage + impact

        if side == OrderSide.BUY:
            return base_price * (1.0 + total_slippage)
        else:
            return base_price * (1.0 - total_slippage)

    def execute_buy(
        self,
        symbol_fa: str,
        price: float,
        quantity: int,
        date: str,
        daily_volume: Optional[int] = None,
        is_locked_limit_up: bool = False
    ) -> Optional[Trade]:
        """
        اجرای سفارش خرید با بررسی قفل صف خرید، محدودیت نقدشوندگی و کسر کارمزد.
        """
        if quantity <= 0 or price <= 0:
            return None

        # ۱. قفل صف خرید: اگر سهم صف خرید قفل باشد و معامله‌ای نشود، سفارش پر نمی‌شود
        if is_locked_limit_up:
            logger.warning("سفارش خرید نماد '%s' در صف خرید قفل شد و اجرا نگردید.", symbol_fa)
            return None

        # ۲. قید نقدشوندگی: حجم سفارش نمی‌تواند از درصد معقولی از حجم روزانه بیشتر باشد
        if daily_volume and daily_volume > 0:
            max_allowed = int(daily_volume * self.max_volume_participation_pct)
            if max_allowed <= 0:
                logger.warning("حجم معاملات روز نماد '%s' برای این حجم کافی نیست.", symbol_fa)
                return None
            if quantity > max_allowed:
                logger.info("حجم سفارش '%s' از %d به سقف نقدشوندگی %d تعدیل شد.", symbol_fa, quantity, max_allowed)
                quantity = max_allowed

        # ۳. محاسبه قیمت موثر پس از لغزش (Slippage)
        execution_price = self.calculate_slippage_price(price, OrderSide.BUY, quantity, daily_volume)

        gross_value = execution_price * quantity
        commission = gross_value * self.BUY_COMMISSION_RATE
        total_required = gross_value + commission

        # بررسی موجودی نقد
        if self.cash < total_required:
            max_qty = int(self.cash / (execution_price * (1 + self.BUY_COMMISSION_RATE)))
            if max_qty <= 0:
                logger.warning("موجودی نقد برای خرید نماد '%s' ناکافی است.", symbol_fa)
                return None
            quantity = max_qty
            gross_value = execution_price * quantity
            commission = gross_value * self.BUY_COMMISSION_RATE
            total_required = gross_value + commission

        self.cash -= total_required

        trade = Trade(
            symbol_fa=symbol_fa,
            side=OrderSide.BUY,
            quantity=quantity,
            price=round(execution_price, 2),
            gross_value=round(gross_value, 2),
            commission=round(commission, 2),
            net_value=round(total_required, 2),
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

    def execute_sell(
        self,
        symbol_fa: str,
        price: float,
        quantity: int,
        date: str,
        daily_volume: Optional[int] = None,
        is_locked_limit_down: bool = False
    ) -> Optional[Trade]:
        """
        اجرای سفارش فروش با بررسی قفل صف فروش، کسر مالیات و کارمزد.
        """
        if symbol_fa not in self.positions or self.positions[symbol_fa].quantity <= 0:
            logger.warning("پوزیشن بازی برای فروش نماد '%s' وجود ندارد.", symbol_fa)
            return None

        # ۱. قفل صف فروش: اگر سهم صف فروش قفل باشد، خروج ناممکن است
        if is_locked_limit_down:
            logger.warning("فروش نماد '%s' به دلیل صف فروش قفل ناموفق بود و پوزیشن باقی ماند.", symbol_fa)
            return None

        pos = self.positions[symbol_fa]
        sell_qty = min(quantity, pos.quantity)

        # ۲. قید نقدشوندگی
        if daily_volume and daily_volume > 0:
            max_allowed = int(daily_volume * self.max_volume_participation_pct)
            if max_allowed > 0 and sell_qty > max_allowed:
                sell_qty = max_allowed

        # ۳. قیمت موثر با لغزش فروش
        execution_price = self.calculate_slippage_price(price, OrderSide.SELL, sell_qty, daily_volume)

        gross_value = execution_price * sell_qty
        commission = gross_value * self.SELL_COMMISSION_RATE
        net_received = gross_value - commission

        self.cash += net_received

        trade = Trade(
            symbol_fa=symbol_fa,
            side=OrderSide.SELL,
            quantity=sell_qty,
            price=round(execution_price, 2),
            gross_value=round(gross_value, 2),
            commission=round(commission, 2),
            net_value=round(net_received, 2),
            date=date
        )
        self.trades.append(trade)

        # محاسبه سود/زیان
        cost_of_sold = pos.average_entry_price * sell_qty
        realized_pnl = net_received - cost_of_sold
        pos.realized_pnl += realized_pnl

        pos.quantity -= sell_qty
        if pos.quantity == 0:
            pos.total_cost = 0.0
            pos.average_entry_price = 0.0
        else:
            pos.total_cost -= cost_of_sold

        pos.trades.append(trade)
        return trade

    def calculate_metrics(self) -> PerformanceMetrics:
        """
        محاسبه شاخص‌های آماری عملکرد بکتست.
        """
        if not self.trades:
            return PerformanceMetrics()

        sell_trades = [t for t in self.trades if t.side == OrderSide.SELL]
        total_trades = len(sell_trades)
        winning_trades = 0
        losing_trades = 0
        gross_profit = 0.0
        gross_loss = 0.0
        total_commissions = sum(t.commission for t in self.trades)

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
