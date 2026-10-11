# -*- coding: utf-8 -*-
"""
موتور شبیه‌سازی و آزمون تاریخی معاملات (Backtest Engine)

واقعیت‌های معاملاتی بورس تهران و فرابورس که لحاظ می‌شوند:
- کارمزد خرید ۰.۳۷۱۲٪ و فروش ۰.۸۸٪ (از config/default.yaml — منبع یکتا)
- سیگنال در پایان روز t ساخته می‌شود و سفارش در روز t+1 اجرا می‌شود (بدون نگاه به آینده)
- قیمت اجرا = میانگین موزون قیمت روز اجرا (ارزش ÷ حجم) + لغزش؛ هرگز خارج از [کمترین، بیشترین] آن روز
- خرید در روز «قفل صف خرید» و فروش در روز «قفل صف فروش» انجام نمی‌شود
  (سفارش فروش تا اولین روز قابل خروج منتظر می‌ماند)
- سقف مشارکت: حجم سفارش حداکثر ۵٪ حجم معاملات روز
"""

import logging
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from backtest.models import ClosedTrade, OrderSide, PerformanceMetrics, Position, Trade
from config import settings
from core import market_rules

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 240

SignalFn = Callable[[pd.Series, "BacktestEngine"], Optional[str]]


class BacktestEngine:
    """موتور اجرای بکتست واقع‌گرایانه با لحاظ ریزساختار بازار بورس ایران."""

    BUY_COMMISSION_RATE = settings.FEES["buy"]
    SELL_COMMISSION_RATE = settings.FEES["sell"]

    def __init__(
        self,
        initial_capital: Optional[float] = None,
        max_volume_participation_pct: Optional[float] = None,
        default_slippage_bps: Optional[float] = None,
    ):
        cfg = settings.BACKTEST_CONFIG
        self.initial_capital = float(initial_capital if initial_capital is not None else cfg["initial_capital"])
        self.cash = self.initial_capital
        self.max_volume_participation_pct = (
            max_volume_participation_pct if max_volume_participation_pct is not None
            else cfg["max_volume_participation_pct"]
        )
        self.default_slippage_bps = (
            default_slippage_bps if default_slippage_bps is not None else cfg["default_slippage_bps"]
        )
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.closed_trades: List[ClosedTrade] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.last_prices: Dict[str, float] = {}
        self.blocked_orders = 0

    # ═══════════════════════════════════════════════
    # اجرای سفارش
    # ═══════════════════════════════════════════════

    def calculate_slippage_price(self, base_price: float, side: OrderSide, quantity: int,
                                 daily_volume: Optional[int] = None) -> float:
        """قیمت اجرا با احتساب اسپرد و اثر حجم سفارش نسبت به حجم روز (Market Impact)."""
        slippage = self.default_slippage_bps / 10000.0
        if daily_volume and daily_volume > 0:
            slippage += min((quantity / daily_volume) * 0.1, 0.02)
        return base_price * (1.0 + slippage) if side == OrderSide.BUY else base_price * (1.0 - slippage)

    def _participation_cap(self, quantity: int, daily_volume: Optional[int]) -> int:
        if daily_volume and daily_volume > 0:
            return min(quantity, int(daily_volume * self.max_volume_participation_pct))
        return quantity

    def execute_buy(self, symbol_fa: str, price: float, quantity: int, date: str,
                    daily_volume: Optional[int] = None, is_locked_limit_up: bool = False,
                    day_low: Optional[float] = None, day_high: Optional[float] = None) -> Optional[Trade]:
        """اجرای سفارش خرید با بررسی قفل صف خرید، سقف نقدشوندگی، بازه قیمت روز و کارمزد."""
        if quantity <= 0 or price <= 0:
            return None
        if is_locked_limit_up:
            self.blocked_orders += 1
            logger.info("خرید «%s» در %s اجرا نشد: صف خرید قفل.", symbol_fa, date)
            return None

        quantity = self._participation_cap(quantity, daily_volume)
        if quantity <= 0:
            logger.info("حجم معاملات روز «%s» برای این سفارش کافی نیست.", symbol_fa)
            return None

        execution_price = market_rules.apply_execution_bounds(
            self.calculate_slippage_price(price, OrderSide.BUY, quantity, daily_volume), day_low, day_high)
        unit_cost = execution_price * (1 + self.BUY_COMMISSION_RATE)
        if self.cash < unit_cost * quantity:
            quantity = int(self.cash / unit_cost)
            if quantity <= 0:
                logger.info("موجودی نقد برای خرید «%s» کافی نیست.", symbol_fa)
                return None

        gross_value = execution_price * quantity
        commission = gross_value * self.BUY_COMMISSION_RATE
        total_required = gross_value + commission
        self.cash -= total_required

        trade = Trade(symbol_fa=symbol_fa, side=OrderSide.BUY, quantity=quantity, price=round(execution_price, 2),
                      gross_value=round(gross_value, 2), commission=round(commission, 2),
                      net_value=round(total_required, 2), date=date)
        self.trades.append(trade)

        pos = self.positions.setdefault(symbol_fa, Position(symbol_fa=symbol_fa))
        if pos.quantity == 0:
            pos.opened_at = date
        pos.quantity += quantity
        pos.total_cost += total_required
        pos.average_entry_price = pos.total_cost / pos.quantity
        pos.trades.append(trade)
        self.last_prices[symbol_fa] = execution_price
        return trade

    def execute_sell(self, symbol_fa: str, price: float, quantity: int, date: str,
                     daily_volume: Optional[int] = None, is_locked_limit_down: bool = False,
                     day_low: Optional[float] = None, day_high: Optional[float] = None) -> Optional[Trade]:
        """اجرای سفارش فروش با بررسی قفل صف فروش، سقف نقدشوندگی، بازه قیمت روز، مالیات و کارمزد."""
        pos = self.positions.get(symbol_fa)
        if pos is None or pos.quantity <= 0:
            logger.warning("پوزیشن بازی برای فروش «%s» وجود ندارد.", symbol_fa)
            return None
        if is_locked_limit_down:
            self.blocked_orders += 1
            logger.info("فروش «%s» در %s اجرا نشد: صف فروش قفل.", symbol_fa, date)
            return None

        sell_qty = self._participation_cap(min(quantity, pos.quantity), daily_volume)
        if sell_qty <= 0:
            return None

        execution_price = market_rules.apply_execution_bounds(
            self.calculate_slippage_price(price, OrderSide.SELL, sell_qty, daily_volume), day_low, day_high)
        gross_value = execution_price * sell_qty
        commission = gross_value * self.SELL_COMMISSION_RATE
        net_received = gross_value - commission
        self.cash += net_received

        trade = Trade(symbol_fa=symbol_fa, side=OrderSide.SELL, quantity=sell_qty, price=round(execution_price, 2),
                      gross_value=round(gross_value, 2), commission=round(commission, 2),
                      net_value=round(net_received, 2), date=date)
        self.trades.append(trade)

        cost_basis = pos.average_entry_price * sell_qty            # شامل کارمزد خرید
        buy_gross = cost_basis / (1.0 + self.BUY_COMMISSION_RATE)  # ارزش خرید بدون کارمزد
        net_trade_pnl = net_received - cost_basis
        self.closed_trades.append(ClosedTrade(
            symbol_fa=symbol_fa, entry_date=pos.opened_at or date, exit_date=date, quantity=sell_qty,
            entry_price=round(buy_gross / sell_qty, 2), exit_price=round(execution_price, 2),
            gross_pnl=round(gross_value - buy_gross, 2),
            commissions=round((cost_basis - buy_gross) + commission, 2),
            net_pnl=round(net_trade_pnl, 2),
            return_pct=round(net_trade_pnl / cost_basis * 100.0, 4) if cost_basis > 0 else 0.0,
            is_winner=net_trade_pnl > 0,
        ))

        pos.realized_pnl += net_trade_pnl
        pos.quantity -= sell_qty
        if pos.quantity == 0:
            pos.total_cost = 0.0
            pos.average_entry_price = 0.0
            pos.opened_at = None
        else:
            pos.total_cost -= cost_basis
        pos.trades.append(trade)
        self.last_prices[symbol_fa] = execution_price
        return trade

    # ═══════════════════════════════════════════════
    # ارزش پرتفوی و متریک‌ها
    # ═══════════════════════════════════════════════

    def mark_to_market(self) -> float:
        """ارزش کل پرتفوی = نقد + ارزش روز سهام‌ها (با آخرین قیمت شناخته‌شده هر نماد)."""
        holdings = sum(
            pos.quantity * self.last_prices.get(sym, pos.average_entry_price)
            for sym, pos in self.positions.items()
        )
        return self.cash + holdings

    def record_equity(self, date: str) -> None:
        self.equity_curve.append({"date": date, "equity": round(self.mark_to_market(), 2)})

    def calculate_metrics(self) -> PerformanceMetrics:
        """محاسبه شاخص‌های عملکرد از روی معاملات بسته‌شده و منحنی ارزش پرتفوی."""
        final_equity = self.mark_to_market()
        total_return_pct = (final_equity / self.initial_capital - 1.0) * 100.0 if self.initial_capital > 0 else 0.0
        if not self.trades:
            return PerformanceMetrics(initial_capital=self.initial_capital, final_equity=round(final_equity, 2),
                                      total_return_pct=round(total_return_pct, 2),
                                      blocked_orders=self.blocked_orders, equity_curve=self.equity_curve)

        closed = self.closed_trades
        winners = [t for t in closed if t.is_winner]
        losers = [t for t in closed if not t.is_winner]
        gross_profit = sum(t.net_pnl for t in winners)
        gross_loss = sum(abs(t.net_pnl) for t in losers)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        expectancy = sum(t.return_pct for t in closed) / len(closed) if closed else 0.0

        max_dd_pct = 0.0
        equities = [pt["equity"] for pt in self.equity_curve]
        if equities:
            peak = equities[0]
            for eq in equities:
                peak = max(peak, eq)
                if peak > 0:
                    max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)

        sharpe = 0.0
        if len(equities) > 2:
            returns = pd.Series(equities).pct_change().dropna()
            if len(returns) > 1 and returns.std() > 1e-9:
                sharpe = float(returns.mean() / returns.std() * (TRADING_DAYS_PER_YEAR ** 0.5))

        return PerformanceMetrics(
            total_trades=len(self.trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=round(len(winners) / len(closed) * 100.0, 2) if closed else 0.0,
            gross_profit=round(gross_profit, 2),
            gross_loss=round(gross_loss, 2),
            net_pnl=round(gross_profit - gross_loss, 2),
            profit_factor=round(profit_factor, 2),
            expectancy_pct=round(expectancy, 4),
            max_drawdown_pct=round(max_dd_pct, 2),
            total_commissions_paid=round(sum(t.commission for t in self.trades), 2),
            sharpe_ratio=round(sharpe, 2),
            initial_capital=round(self.initial_capital, 2),
            final_equity=round(final_equity, 2),
            total_return_pct=round(total_return_pct, 2),
            blocked_orders=self.blocked_orders,
            equity_curve=self.equity_curve,
        )

    # ═══════════════════════════════════════════════
    # شبیه‌سازی تاریخی روزانه
    # ═══════════════════════════════════════════════

    @staticmethod
    def _prepare(prices_df: pd.DataFrame) -> pd.DataFrame:
        """یکسان‌سازی ستون‌ها: داده دیتابیس (*_raw) یا دیتافریم ساده (date/close/volume)."""
        df = prices_df.sort_values("date").reset_index(drop=True).copy()
        for col in ("open", "high", "low", "close"):
            if f"{col}_raw" not in df.columns and col in df.columns:
                df[f"{col}_raw"] = df[col]
        if "close_raw" not in df.columns:
            raise ValueError("دیتافریم قیمت باید ستون close_raw یا close داشته باشد")
        df["_mark"] = market_rules.official_close(df)
        has_range = {"high_raw", "low_raw"}.issubset(df.columns)
        if has_range and {"value", "volume"}.issubset(df.columns):
            df["_fill"] = market_rules.daily_vwap(df).fillna(df["open_raw"] if "open_raw" in df.columns else df["_mark"])
        else:
            df["_fill"] = df["open_raw"] if "open_raw" in df.columns else df["_mark"]
        if has_range and "volume" in df.columns:
            flags = market_rules.lock_flags(df)
            df["_locked_up"], df["_locked_down"] = flags["locked_up"], flags["locked_down"]
        else:
            df["_locked_up"] = df["_locked_down"] = False
        if "volume" not in df.columns:
            df["volume"] = None
        return df

    def run_replay_simulation(self, prices_df: pd.DataFrame, symbol_fa: str, strategy_signal_fn: SignalFn,
                              allocation_pct: float = 0.95) -> PerformanceMetrics:
        """
        حلقه شبیه‌سازی تاریخی روزانه بدون نگاه به آینده:
        روز t: ابتدا سفارش ثبت‌شده در روز t-1 اجرا می‌شود ← سپس استراتژی با داده‌های تا پایان روز t
        تصمیم می‌گیرد ← سفارش جدید برای روز t+1 ثبت می‌شود ← ارزش پرتفوی با قیمت پایانی روز t ثبت می‌شود.

        strategy_signal_fn(row, engine) باید 'BUY'، 'SELL' یا None برگرداند.
        """
        df = self._prepare(prices_df)
        pending: Optional[str] = None
        prev_volume: Optional[float] = None

        for _, row in df.iterrows():
            date_str = str(row["date"])
            fill_price = float(row["_fill"]) if pd.notna(row["_fill"]) else None
            day_volume = int(row["volume"]) if pd.notna(row["volume"]) else None
            # سقف مشارکت با کمترین حجم بین روز تصمیم و روز اجرا (محافظه‌کارانه)
            cap_volume = min(v for v in (day_volume, prev_volume) if v) if (day_volume or prev_volume) else None
            low = row["low_raw"] if "low_raw" in row and pd.notna(row["low_raw"]) else None
            high = row["high_raw"] if "high_raw" in row and pd.notna(row["high_raw"]) else None

            # ۱. اجرای سفارش روز قبل
            if pending == "BUY" and fill_price:
                qty = int(self.cash * allocation_pct / (fill_price * (1.0 + self.BUY_COMMISSION_RATE)))
                if qty > 0:
                    self.execute_buy(symbol_fa, fill_price, qty, date_str, daily_volume=cap_volume,
                                     is_locked_limit_up=bool(row["_locked_up"]), day_low=low, day_high=high)
                pending = None  # سیگنال خرید کهنه نمی‌ماند؛ استراتژی می‌تواند دوباره سیگنال بدهد
            elif pending == "SELL" and fill_price:
                pos = self.positions.get(symbol_fa)
                if pos and pos.quantity > 0:
                    self.execute_sell(symbol_fa, fill_price, pos.quantity, date_str, daily_volume=cap_volume,
                                      is_locked_limit_down=bool(row["_locked_down"]), day_low=low, day_high=high)
                pos = self.positions.get(symbol_fa)
                if not pos or pos.quantity == 0:
                    pending = None  # تا خروج کامل، سفارش فروش باقی می‌ماند

            # ۲. تصمیم استراتژی با داده‌های تا پایان امروز
            signal = strategy_signal_fn(row.drop(labels=[c for c in row.index if c.startswith("_")]), self)
            if signal in ("BUY", "SELL"):
                pending = signal

            # ۳. ارزش‌گذاری پرتفوی با قیمت پایانی امروز
            if pd.notna(row["_mark"]):
                self.last_prices[symbol_fa] = float(row["_mark"])
            self.record_equity(date_str)
            prev_volume = day_volume

        return self.calculate_metrics()
