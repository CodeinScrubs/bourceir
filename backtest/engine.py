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
import math
from numbers import Real
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
        allow_unverified_fills: bool = False,
        periods_per_year: Optional[float] = None,
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
        if not math.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError("سرمایه اولیه باید مثبت و متناهی باشد")
        if not 0 < self.max_volume_participation_pct <= 0.05 or not 0 <= self.default_slippage_bps < 10000:
            raise ValueError("سقف مشارکت یا لغزش نامعتبر است")
        if periods_per_year is not None and (not math.isfinite(periods_per_year) or periods_per_year <= 0):
            raise ValueError("تناوب بازده نامعتبر است")
        self.allow_unverified_fills = allow_unverified_fills
        self.periods_per_year = periods_per_year
        self._used = {}
        self._episodes = {}
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

    @staticmethod
    def _positive(value):
        return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) and value > 0

    def _sized_fill(self, symbol, price, quantity, date, daily_volume, daily_value, side, low, high):
        if not self._positive(price) or not self._positive(quantity) or not float(quantity).is_integer():
            return 0, 0.0
        if any(v is not None and not self._positive(v) for v in (daily_volume, daily_value, low, high)):
            return 0, 0.0
        if not self.allow_unverified_fills and any(v is None for v in (daily_volume, daily_value, low, high)):
            return 0, 0.0
        if low is not None and high is not None and not low <= price <= high:
            return 0, 0.0
        key = (symbol, str(date)[:10])
        used_volume, used_value = self._used.get(key, (0, 0.0))
        cap = int(quantity)
        if daily_volume is not None:
            cap = min(cap, max(0, int(daily_volume * self.max_volume_participation_pct) - used_volume))
        value_budget = float("inf") if daily_value is None else max(
            0.0, daily_value * self.max_volume_participation_pct - used_value)
        if side == OrderSide.BUY:
            value_budget = min(value_budget, self.cash / (1 + self.BUY_COMMISSION_RATE))
        # لغزش تابع مقدار نهایی است؛ کاهش حجم نباید با لغزش حجم بزرگ قبلی محاسبه شود.
        left, right = 0, cap
        while left < right:
            q = (left + right + 1) // 2
            fill = market_rules.apply_execution_bounds(self.calculate_slippage_price(price, side, q, daily_volume), low, high)
            if q * fill <= value_budget:
                left = q
            else:
                right = q - 1
        fill = market_rules.apply_execution_bounds(self.calculate_slippage_price(price, side, left, daily_volume), low, high)
        return left, fill

    def _record_participation(self, symbol, date, quantity, value):
        key = (symbol, str(date)[:10])
        used_volume, used_value = self._used.get(key, (0, 0.0))
        self._used[key] = used_volume + quantity, used_value + value

    def execute_buy(self, symbol_fa: str, price: float, quantity: int, date: str,
                    daily_volume: Optional[int] = None, is_locked_limit_up: Optional[bool] = None,
                    day_low: Optional[float] = None, day_high: Optional[float] = None,
                    daily_value: Optional[float] = None) -> Optional[Trade]:
        """اجرای سفارش خرید با بررسی قفل صف خرید، سقف نقدشوندگی، بازه قیمت روز و کارمزد."""
        if is_locked_limit_up is True or (is_locked_limit_up is None and not self.allow_unverified_fills):
            self.blocked_orders += 1
            logger.info("خرید «%s» در %s اجرا نشد: صف خرید قفل.", symbol_fa, date)
            return None

        quantity, execution_price = self._sized_fill(symbol_fa, price, quantity, date, daily_volume,
                                                       daily_value, OrderSide.BUY, day_low, day_high)
        if quantity <= 0:
            return None

        gross_value = execution_price * quantity
        commission = gross_value * self.BUY_COMMISSION_RATE
        total_required = gross_value + commission
        self.cash -= total_required
        self._record_participation(symbol_fa, date, quantity, gross_value)

        trade = Trade(symbol_fa=symbol_fa, side=OrderSide.BUY, quantity=quantity, price=round(execution_price, 2),
                      gross_value=round(gross_value, 2), commission=round(commission, 2),
                      net_value=round(total_required, 2), date=date)
        self.trades.append(trade)

        pos = self.positions.setdefault(symbol_fa, Position(symbol_fa=symbol_fa))
        if pos.quantity == 0:
            pos.opened_at = date
            self._episodes[symbol_fa] = {"cost": 0.0, "buy_gross": 0.0, "sell_gross": 0.0,
                                        "sell_fees": 0.0, "qty": 0, "entry": date}
        self._episodes[symbol_fa]["cost"] += total_required
        self._episodes[symbol_fa]["buy_gross"] += gross_value
        pos.quantity += quantity
        pos.total_cost += total_required
        pos.average_entry_price = pos.total_cost / pos.quantity
        pos.trades.append(trade)
        self.last_prices[symbol_fa] = execution_price
        return trade

    def execute_sell(self, symbol_fa: str, price: float, quantity: int, date: str,
                     daily_volume: Optional[int] = None, is_locked_limit_down: Optional[bool] = None,
                     day_low: Optional[float] = None, day_high: Optional[float] = None,
                    daily_value: Optional[float] = None) -> Optional[Trade]:
        """اجرای سفارش فروش با بررسی قفل صف فروش، سقف نقدشوندگی، بازه قیمت روز، مالیات و کارمزد."""
        pos = self.positions.get(symbol_fa)
        if pos is None or pos.quantity <= 0:
            logger.warning("پوزیشن بازی برای فروش «%s» وجود ندارد.", symbol_fa)
            return None
        if is_locked_limit_down is True or (is_locked_limit_down is None and not self.allow_unverified_fills):
            self.blocked_orders += 1
            logger.info("فروش «%s» در %s اجرا نشد: صف فروش قفل.", symbol_fa, date)
            return None

        if not self._positive(quantity) or not float(quantity).is_integer():
            return None
        sell_qty, execution_price = self._sized_fill(symbol_fa, price, min(quantity, pos.quantity), date,
                                                       daily_volume, daily_value, OrderSide.SELL, day_low, day_high)
        if sell_qty <= 0:
            return None

        gross_value = execution_price * sell_qty
        commission = gross_value * self.SELL_COMMISSION_RATE
        net_received = gross_value - commission
        self.cash += net_received
        self._record_participation(symbol_fa, date, sell_qty, gross_value)

        trade = Trade(symbol_fa=symbol_fa, side=OrderSide.SELL, quantity=sell_qty, price=round(execution_price, 2),
                      gross_value=round(gross_value, 2), commission=round(commission, 2),
                      net_value=round(net_received, 2), date=date)
        self.trades.append(trade)

        cost_basis = pos.average_entry_price * sell_qty            # شامل کارمزد خرید
        net_trade_pnl = net_received - cost_basis
        episode = self._episodes[symbol_fa]
        episode["sell_gross"] += gross_value
        episode["sell_fees"] += commission
        episode["qty"] += sell_qty
        if pos.quantity == sell_qty:
            pnl = episode["sell_gross"] - episode["sell_fees"] - episode["cost"]
            self.closed_trades.append(ClosedTrade(
                symbol_fa=symbol_fa, entry_date=episode["entry"], exit_date=date, quantity=episode["qty"],
                entry_price=episode["buy_gross"] / episode["qty"],
                exit_price=episode["sell_gross"] / episode["qty"],
                gross_pnl=episode["sell_gross"] - episode["buy_gross"],
                commissions=episode["cost"] - episode["buy_gross"] + episode["sell_fees"],
                net_pnl=pnl, return_pct=pnl / episode["cost"] * 100, is_winner=pnl > 0,
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
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        expectancy = sum(t.return_pct for t in closed) / len(closed) if closed else 0.0

        max_dd_pct = 0.0
        equities = [self.initial_capital] + [pt["equity"] for pt in self.equity_curve]
        if equities:
            peak = equities[0]
            for eq in equities:
                peak = max(peak, eq)
                if peak > 0:
                    max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)

        sharpe = None
        if self.periods_per_year is not None and len(equities) > 2:
            returns = pd.Series(equities).pct_change().dropna()
            if len(returns) > 1 and returns.std() > 1e-9:
                sharpe = float(returns.mean() / returns.std() * (self.periods_per_year ** 0.5))

        return PerformanceMetrics(
            total_trades=len(self.trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=round(len(winners) / len(closed) * 100.0, 2) if closed else 0.0,
            gross_profit=round(gross_profit, 2),
            gross_loss=round(gross_loss, 2),
            net_pnl=round(final_equity - self.initial_capital, 2),
            realized_pnl=round(sum(p.realized_pnl for p in self.positions.values()), 2),
            unverified_fill_mode=self.allow_unverified_fills,
            profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
            expectancy_pct=round(expectancy, 4),
            max_drawdown_pct=round(max_dd_pct, 2),
            total_commissions_paid=round(sum(t.commission for t in self.trades), 2),
            sharpe_ratio=round(sharpe, 2) if sharpe is not None else None,
            initial_capital=round(self.initial_capital, 2),
            final_equity=round(final_equity, 2),
            total_return_pct=round(total_return_pct, 2),
            blocked_orders=self.blocked_orders,
            equity_curve=self.equity_curve,
        )

    # ═══════════════════════════════════════════════
    # شبیه‌سازی تاریخی روزانه
    # ═══════════════════════════════════════════════

    def _prepare(self, prices_df: pd.DataFrame) -> pd.DataFrame:
        """یکسان‌سازی ستون‌ها: داده دیتابیس (*_raw) یا دیتافریم ساده (date/close/volume)."""
        if prices_df["date"].duplicated().any():
            raise ValueError("تاریخ تکراری در ورودی بکتست")
        required = {"high_raw", "low_raw", "close_raw", "close_final_raw", "ref_price_raw", "volume", "value"}
        if not self.allow_unverified_fills and not required.issubset(prices_df.columns):
            raise ValueError("قیمت خام، مرجع، بازه و نقدشوندگی برای بکتست لازم‌اند")
        df = prices_df.sort_values("date").reset_index(drop=True).copy()
        if not self.allow_unverified_fills:
            for column in ("high_raw", "low_raw", "close_final_raw", "ref_price_raw", "volume", "value"):
                values = pd.to_numeric(df[column], errors="coerce")
                valid = values.map(lambda v: math.isfinite(v) and (v >= 0 if column in {"volume", "value"} else v > 0))
                if not valid.all():
                    raise ValueError(f"داده اجرای معتبر در ستون {column} موجود نیست")
        for col in ("open", "high", "low", "close"):
            if f"{col}_raw" not in df.columns and col in df.columns:
                df[f"{col}_raw"] = df[col]
        if "close_raw" not in df.columns:
            raise ValueError("دیتافریم قیمت باید ستون close_raw یا close داشته باشد")
        df["_mark"] = market_rules.official_close(df)
        has_range = {"high_raw", "low_raw"}.issubset(df.columns)
        if has_range and {"value", "volume"}.issubset(df.columns):
            df["_fill"] = market_rules.daily_vwap(df)
            if self.allow_unverified_fills:
                df["_fill"] = df["_fill"].fillna(df["open_raw"] if "open_raw" in df.columns else df["_mark"])
            elif (df["_fill"].isna() & (df["volume"] > 0)).any():
                raise ValueError("ارزش/حجم با بازه قیمت روز سازگار نیست؛ اجرای ساختگی ممنوع است")
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
        if not 0 < allocation_pct <= 1:
            raise ValueError("سهم سرمایه تخصیص‌یافته باید بین صفر و یک باشد")
        df = self._prepare(prices_df)
        pending: Optional[str] = None
        prev_volume: Optional[float] = None

        for _, row in df.iterrows():
            date_str = str(row["date"])
            fill_price = float(row["_fill"]) if pd.notna(row["_fill"]) else None
            day_volume = int(row["volume"]) if pd.notna(row["volume"]) else None
            # سقف مشارکت با کمترین حجم بین روز تصمیم و روز اجرا (محافظه‌کارانه)
            cap_volume = min(day_volume, prev_volume) if day_volume is not None and prev_volume is not None else day_volume
            low = row["low_raw"] if "low_raw" in row and pd.notna(row["low_raw"]) else None
            high = row["high_raw"] if "high_raw" in row and pd.notna(row["high_raw"]) else None

            if not self.allow_unverified_fills and self.positions.get(symbol_fa, Position(symbol_fa)).quantity:
                ref = row.get("ref_price_raw")
                prior = self.last_prices.get(symbol_fa)
                if self._positive(ref) and self._positive(prior) and abs(ref / prior - 1) > 1e-6:
                    raise ValueError("قیمت مرجع تغییر کرده است؛ حسابداری اقدام شرکتی برای پوزیشن باز هنوز پشتیبانی نمی‌شود")

            # ۱. اجرای سفارش روز قبل
            if pending == "BUY" and fill_price:
                qty = int(self.cash * allocation_pct / (fill_price * (1.0 + self.BUY_COMMISSION_RATE)))
                if qty > 0:
                    self.execute_buy(symbol_fa, fill_price, qty, date_str, daily_volume=cap_volume,
                                     is_locked_limit_up=bool(row["_locked_up"]), day_low=low, day_high=high,
                                     daily_value=row.get("value"))
                pending = None  # سیگنال خرید کهنه نمی‌ماند؛ استراتژی می‌تواند دوباره سیگنال بدهد
            elif pending == "SELL" and fill_price:
                pos = self.positions.get(symbol_fa)
                if pos and pos.quantity > 0:
                    self.execute_sell(symbol_fa, fill_price, pos.quantity, date_str, daily_volume=cap_volume,
                                      is_locked_limit_down=bool(row["_locked_down"]), day_low=low, day_high=high,
                                      daily_value=row.get("value"))
                pos = self.positions.get(symbol_fa)
                if not pos or pos.quantity == 0:
                    pending = None  # تا خروج کامل، سفارش فروش باقی می‌ماند

            # ۲. تصمیم استراتژی با داده‌های تا پایان امروز
            signal = strategy_signal_fn(row.drop(labels=[c for c in row.index if c.startswith("_") or c in {"adj_close", "adjustment_factor", "open", "high", "low", "close"}]), self)
            if signal in ("BUY", "SELL"):
                pending = signal

            # ۳. ارزش‌گذاری پرتفوی با قیمت پایانی امروز
            if pd.notna(row["_mark"]):
                self.last_prices[symbol_fa] = float(row["_mark"])
            self.record_equity(date_str)
            prev_volume = day_volume

        return self.calculate_metrics()
