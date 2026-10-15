"""Shared-cash multi-equity daily replay. Research proxy, not proof of brokerage fills."""

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from config import settings
from core import market_rules
from core.risk import finite_number

PORTFOLIO_VERSION = "portfolio-v1"


@dataclass(frozen=True)
class PortfolioConfig:
    initial_capital_rial: float
    horizon: int = 2
    max_positions: int = 5
    participation: float = 0.05
    slippage_bps: float = 10
    max_loss_pct: float = 0.10
    stress: bool = False

    def __post_init__(self):
        finite_number(self.initial_capital_rial)
        for value in (self.horizon, self.max_positions):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("افق و تعداد پوزیشن باید صحیح و مثبت باشند")
        if not 0 < finite_number(self.participation) <= 0.05 or not 0 < finite_number(self.max_loss_pct) < 1:
            raise ValueError("مشارکت حداکثر ۵٪ است؛ سقف زیان بین صفر و یک است")
        if finite_number(self.slippage_bps, positive=False) >= 10000:
            raise ValueError("لغزش نامعتبر است")


@dataclass
class PortfolioResult:
    equity: pd.DataFrame
    executions: pd.DataFrame
    decisions: pd.DataFrame
    metrics: dict
    assumptions: dict


def _calendar(values):
    result = np.asarray(values).astype(str)
    if result.ndim != 1 or not len(result) or list(result) != sorted(set(result)):
        raise ValueError("تقویم باید یکتا، مرتب و غیرخالی باشد")
    if pd.to_datetime(result, format="%Y-%m-%d", errors="coerce").isna().any():
        raise ValueError("تاریخ تقویم معتبر نیست")
    return result


def replay_portfolio(prices: pd.DataFrame, selections: pd.DataFrame, calendar,
                     config: PortfolioConfig, start: str | None = None, end: str | None = None) -> PortfolioResult:
    """Decisions t -> next session; shared cash, slots, partial fills and delayed exits.

    Purchases precede sales in daily bookkeeping, so same-day sale proceeds cannot fund an earlier
    purchase without intraday ordering evidence. Position size uses decision-day equity and value.
    Stress buys at high and sells at low. No lookahead columns enter selection or sizing.
    Unknown action/mark for an open position invalidates returns and halts the replay.
    """
    calendar = _calendar(calendar)
    if prices.duplicated(["symbol", "date"]).any() or selections.duplicated(["symbol", "date"]).any():
        raise ValueError("نماد/تاریخ تکراری مجاز نیست")
    required = {"symbol", "date", "low_raw", "high_raw", "close_raw", "close_final_raw", "ref_price_raw", "volume", "value"}
    if not required <= set(prices):
        raise ValueError("ستون‌های خام لازم قیمت موجود نیستند")
    prices = prices.sort_values(["symbol", "date"]).copy()
    prices["vwap"] = market_rules.daily_vwap(prices)
    prices["mark"] = market_rules.official_close(prices)
    prices["action"] = market_rules.corporate_action_factors(prices)
    locks = market_rules.lock_flags(prices)
    prices["up"], prices["down"] = locks["locked_up"], locks["locked_down"]
    prices["valid"] = np.isfinite(prices[["vwap", "ref_price_raw", "mark"]]).all(axis=1)
    prices["valid"] &= (prices["ref_price_raw"] > 0) & (prices["mark"] > 0)
    by_day = {d: g.set_index("symbol").to_dict("index") for d, g in prices.groupby("date", sort=False)}
    order = selections.sort_values(["date", "rank", "symbol"]) if "rank" in selections else selections.sort_values(["date", "symbol"])
    selected = {d: g["symbol"].tolist() for d, g in order.groupby("date")}
    days = [d for d in calendar if (start is None or d >= start) and (end is None or d < end)]
    if not days:
        raise ValueError("بازه اجرای سبد خالی است")
    positions, pending, executions, decisions, curve = {}, [], [], [], []
    cash, realized, fees = float(config.initial_capital_rial), 0.0, 0.0
    equity = cash
    buy_fee, sell_fee = settings.FEES["buy"], settings.FEES["sell"]
    slip, stopped, invalid, action_count = config.slippage_bps / 10000, False, False, 0
    day_position = {day: i for i, day in enumerate(calendar)}

    def note(day, symbol, status, **kwargs):
        decisions.append(dict(date=day, symbol=symbol, status=status, **kwargs))

    def execute(day, symbol, side, quantity, price, cost=0):
        nonlocal fees
        fee = quantity * price * (buy_fee if side == "buy" else sell_fee)
        fees += fee
        executions.append(dict(date=day, symbol=symbol, side=side, quantity=quantity, price_rial=price,
                               fee_rial=fee, realized_net_rial=quantity * price - fee - cost if side == "sell" else None))
        return fee

    for day in days:
        session, used = by_day.get(day, {}), {}
        index = day_position[day]
        # Adjust only already-held shares, before any current-day fills. Halts retain the last observed mark.
        for symbol, pos in positions.items():
            row = session.get(symbol)
            if row is None:
                continue
            factor = row["action"]
            if not row["valid"] or not math.isfinite(factor) or factor <= 0:
                note(day, symbol, "unknown_open_position")
                invalid = True
                break
            if abs(factor - 1) > 1e-6:
                pos["quantity"] /= factor
                action_count += 1
            pos["mark"], pos["mark_date"] = row["mark"], day
        if invalid:
            curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions), stale_positions=0))
            break
        # Pending purchase budget was fixed after the preceding session, not today's high/low/close.
        for decision in pending:
            symbol = decision["symbol"]
            if stopped or symbol in positions or len(positions) >= config.max_positions:
                note(day, symbol, "risk_stop" if stopped else "slots_full", decision_date=decision["date"])
                continue
            row = session.get(symbol)
            if row is None:
                note(day, symbol, "no_session", decision_date=decision["date"])
                continue
            if not row["valid"]:
                note(day, symbol, "unknown_entry_execution", decision_date=decision["date"])
                invalid = True
                break
            if row["up"]:
                note(day, symbol, "locked_buy", decision_date=decision["date"])
                continue
            price = row["high_raw"] if config.stress else min(row["vwap"] * (1 + slip), row["high_raw"])
            cap_volume = min(row["volume"], decision["volume"]) * config.participation
            cap_value = min(row["value"], decision["value"]) * config.participation
            quantity = math.floor(min(cap_volume, cap_value / price, cash / (price * (1 + buy_fee)),
                                      decision["budget"] / (price * (1 + buy_fee))))
            if quantity < 1:
                note(day, symbol, "insufficient_capacity", decision_date=decision["date"])
                continue
            fee = execute(day, symbol, "buy", quantity, price)
            cost = quantity * price + fee
            cash -= cost
            positions[symbol] = dict(quantity=float(quantity), cost=cost, mark=row["mark"], mark_date=day,
                                     entry_date=day, due=index + config.horizon)
            used[symbol] = (float(quantity), quantity * price)
        pending = []
        if invalid:
            curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions), stale_positions=0))
            break
        for symbol, pos in list(positions.items()):
            if index < pos["due"]:
                continue
            row = session.get(symbol)
            if row is None or not row["valid"] or row["down"]:
                note(day, symbol, "exit_delayed")
                continue
            price = row["low_raw"] if config.stress else max(row["vwap"] * (1 - slip), row["low_raw"])
            used_volume, used_value = used.get(symbol, (0, 0))
            volume_left = max(0, row["volume"] * config.participation - used_volume)
            value_left = max(0, row["value"] * config.participation - used_value)
            quantity = min(pos["quantity"], math.floor(min(volume_left, value_left / price)))
            if quantity <= 0:
                note(day, symbol, "partial_exit_wait")
                continue
            cost = pos["cost"] * quantity / pos["quantity"]
            fee = execute(day, symbol, "sell", quantity, price, cost)
            cash += quantity * price - fee
            realized += quantity * price - fee - cost
            pos["quantity"] -= quantity
            pos["cost"] -= cost
            if pos["quantity"] <= 1e-8:
                del positions[symbol]
            else:
                note(day, symbol, "partial_exit_wait")
        equity = cash + sum(p["quantity"] * p["mark"] * (1 - sell_fee) for p in positions.values())
        # Research circuit breaker: no new buys after breaching the user's starting-capital loss floor.
        # It does not assume that held positions can be sold at the floor.
        stopped |= equity <= config.initial_capital_rial * (1 - config.max_loss_pct)
        curve.append(dict(date=day, equity_rial=equity, cash_rial=cash, positions=len(positions),
                          stale_positions=sum(p["mark_date"] != day for p in positions.values()), risk_stopped=stopped))
        if index + 1 < len(calendar) and (end is None or calendar[index + 1] < end) and not stopped:
            for symbol in selected.get(day, []):
                row = session.get(symbol)
                if row is None or not row["valid"]:
                    note(day, symbol, "invalid_decision")
                    continue
                pending.append(dict(date=day, symbol=symbol, value=row["value"], volume=row["volume"],
                                    budget=equity / config.max_positions))
    equity_frame = pd.DataFrame(curve)
    values = pd.concat([pd.Series([config.initial_capital_rial]), equity_frame["equity_rial"]], ignore_index=True)
    drawdown = values / values.cummax() - 1
    final = None if invalid else equity
    metrics = dict(status="invalid_data" if invalid else "complete", portfolio_simulated=True,
                   total_return=None if invalid else equity / config.initial_capital_rial - 1,
                   max_drawdown=None if invalid else float(-drawdown.min()), final_equity_rial=final,
                   initial_capital_rial=config.initial_capital_rial, realized_net_rial=realized, fees_rial=fees,
                   executions=len(executions), open_positions=len(positions), corporate_action_events=action_count,
                   stopped_new_entries=stopped, stale_mark_days=int(equity_frame["stale_positions"].gt(0).sum()))
    assumptions = dict(version=PORTFOLIO_VERSION, config=asdict(config), fees=dict(settings.FEES),
        execution_model="bounded_daily_vwap_proxy" if not config.stress else "buy_high_sell_low_stress",
        corporate_action_model="synthetic_fractional_share_multiplier", same_day_sale_cash_reused=False,
        mark_basis="last_observed_official_close_net_sell_fee", shorting=False, queue_position_proven=False,
        limitations=["historical universe incomplete", "synthetic corporate actions are not actual entitlements",
                     "halting days retain stale marks", "daily volume is not proof of own fills", "no intraday stop execution"])
    return PortfolioResult(equity_frame, pd.DataFrame(executions), pd.DataFrame(decisions), metrics, assumptions)
