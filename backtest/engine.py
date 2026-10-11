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

from backtest.models import Order, Trade, Position, OrderSide, PerformanceMetrics, ClosedTrade

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
        self.closed_trades: List[ClosedTrade] = []
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

        # ایجاد رکورد معامله بسته شده کامل (ClosedTrade) برای محاسبه دقیق Win Rate
        entry_price = pos.average_entry_price
        cost_basis = entry_price * sell_qty
        net_trade_pnl = net_received - cost_basis
        gross_trade_pnl = (execution_price * sell_qty) - (cost_basis / (1.0 + self.BUY_COMMISSION_RATE))
        total_trade_comm = (cost_basis - (cost_basis / (1.0 + self.BUY_COMMISSION_RATE))) + commission
        ret_pct = (net_trade_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

        entry_date = pos.trades[0].date if pos.trades else date
        closed_trade = ClosedTrade(
            symbol_fa=symbol_fa,
            entry_date=entry_date,
            exit_date=date,
            quantity=sell_qty,
            entry_price=round(cost_basis / sell_qty / (1.0 + self.BUY_COMMISSION_RATE), 2),
            exit_price=round(execution_price, 2),
            gross_pnl=round(gross_trade_pnl, 2),
            commissions=round(total_trade_comm, 2),
            net_pnl=round(net_trade_pnl, 2),
            return_pct=round(ret_pct, 2),
            is_winner=net_trade_pnl > 0
        )
        self.closed_trades.append(closed_trade)

        # به‌روزرسانی پوزیشن
        cost_of_sold = pos.average_entry_price * sell_qty
        pos.realized_pnl += net_trade_pnl

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
        محاسبه دقیق شاخص‌های آماری عملکرد بکتست از روی معاملات بسته‌شده و منحنی ارزش پرتفوی.
        """
        if not self.trades:
            return PerformanceMetrics(
                initial_capital=self.initial_capital,
                final_equity=self.cash,
                total_return_pct=0.0
            )

        total_closed = len(self.closed_trades)
        winning_trades = sum(1 for t in self.closed_trades if t.is_winner)
        losing_trades = sum(1 for t in self.closed_trades if not t.is_winner)
        win_rate = (winning_trades / total_closed * 100.0) if total_closed > 0 else 0.0

        gross_profit = sum(t.net_pnl for t in self.closed_trades if t.is_winner)
        gross_loss = sum(abs(t.net_pnl) for t in self.closed_trades if not t.is_winner)
        net_pnl = gross_profit - gross_loss
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        total_commissions = sum(t.commission for t in self.trades)

        # محاسبه Maximum Drawdown از روی Equity Curve
        max_dd_pct = 0.0
        if self.equity_curve:
            equities = [pt["equity"] for pt in self.equity_curve]
            peak = equities[0]
            for eq in equities:
                if eq > peak:
                    peak = eq
                if peak > 0:
                    dd = ((peak - eq) / peak) * 100.0
                    if dd > max_dd_pct:
                        max_dd_pct = dd

        # محاسبه نسبت شارپ سالانه (مبنای ۲۴۰ روز کاری بورس تهران)
        sharpe = 0.0
        if len(self.equity_curve) > 2:
            equities = [pt["equity"] for pt in self.equity_curve]
            returns = [
                (equities[i] - equities[i - 1]) / equities[i - 1]
                for i in range(1, len(equities))
                if equities[i - 1] > 0
            ]
            if len(returns) > 1:
                mean_ret = sum(returns) / len(returns)
                variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
                std_ret = variance ** 0.5
                if std_ret > 1e-6:
                    sharpe = round((mean_ret / std_ret) * (240 ** 0.5), 2)

        # محاسبه ارزش جاری پوزیشن‌های باز بر مبنای بهای تمام‌شده
        open_positions_val = sum(pos.quantity * pos.average_entry_price for pos in self.positions.values())
        final_equity = self.cash + open_positions_val
        total_return_pct = (
            ((final_equity - self.initial_capital) / self.initial_capital) * 100.0
            if self.initial_capital > 0 else 0.0
        )

        return PerformanceMetrics(
            total_trades=len(self.trades),
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=round(win_rate, 2),
            gross_profit=round(gross_profit, 2),
            gross_loss=round(gross_loss, 2),
            net_pnl=round(net_pnl, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            total_commissions_paid=round(total_commissions, 2),
            sharpe_ratio=round(sharpe, 2),
            initial_capital=round(self.initial_capital, 2),
            final_equity=round(final_equity, 2),
            total_return_pct=round(total_return_pct, 2),
            equity_curve=self.equity_curve
        )

    def run_replay_simulation(
        self,
        prices_df: pd.DataFrame,
        symbol_fa: str,
        strategy_signal_fn: Any
    ) -> PerformanceMetrics:
        """
        اجرای حلقه شبیه‌سازی تاریخی (Historical Replay Loop) گام‌به‌گام روی داده‌های روزانه یا کندل‌ها.
        
        پارامترها:
            prices_df: دیتافریم حاوی داده‌های تاریخی مرتب‌شده بر اساس تاریخ
            symbol_fa: نام فارسی نماد
            strategy_signal_fn: تابعی که به ازای هر گام (row, engine) سیگنال صادر می‌کند ('BUY', 'SELL', None)
        """
        sorted_df = prices_df.sort_values(by="date").reset_index(drop=True)

        for _, row in sorted_df.iterrows():
            date_str = str(row["date"])
            close_p = float(row["close_raw"]) if ("close_raw" in row and pd.notna(row["close_raw"])) else float(row["close"])
            vol = int(row["volume"]) if ("volume" in row and pd.notna(row["volume"])) else None

            # دریافت تصمیم استراتژی
            signal = strategy_signal_fn(row, self)

            is_limit_up = False
            is_limit_down = False
            if "high" in row and "low" in row and row["high"] == row["low"]:
                if "yesterday" in row and row["close"] > row["yesterday"]:
                    is_limit_up = True
                elif "yesterday" in row and row["close"] < row["yesterday"]:
                    is_limit_down = True

            if signal == "BUY":
                alloc_cash = self.cash * 0.95
                if alloc_cash > 0 and close_p > 0:
                    target_qty = int(alloc_cash / (close_p * (1.0 + self.BUY_COMMISSION_RATE)))
                    if target_qty > 0:
                        self.execute_buy(
                            symbol_fa=symbol_fa,
                            price=close_p,
                            quantity=target_qty,
                            date=date_str,
                            daily_volume=vol,
                            is_locked_limit_up=is_limit_up
                        )
            elif signal == "SELL":
                if symbol_fa in self.positions and self.positions[symbol_fa].quantity > 0:
                    qty_to_sell = self.positions[symbol_fa].quantity
                    self.execute_sell(
                        symbol_fa=symbol_fa,
                        price=close_p,
                        quantity=qty_to_sell,
                        date=date_str,
                        daily_volume=vol,
                        is_locked_limit_down=is_limit_down
                    )

            # به‌روزرسانی منحنی ارزش کل سرمایه (Mark-to-Market Equity)
            current_holdings_val = sum(pos.quantity * close_p for pos in self.positions.values())
            current_equity = self.cash + current_holdings_val
            self.equity_curve.append({
                "date": date_str,
                "equity": round(current_equity, 2)
            })

        return self.calculate_metrics()
