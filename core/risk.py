"""Manual plan arithmetic in rial. No stock selector, signal or brokerage action."""

import math
from dataclasses import dataclass
from typing import Optional

from config import settings
from core.market_rules import breakeven_move


def finite_number(value, *, positive=True) -> float:
    if isinstance(value, (bool, str)):
        raise ValueError("مقدار عددی معتبر لازم است")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("مقدار عددی معتبر لازم است") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError("مقدار باید محدود و مثبت/نامنفی باشد")
    return result


@dataclass(frozen=True)
class RiskProfile:
    capital_rial: int
    max_loss_rial: int

    def __post_init__(self):
        capital, loss = finite_number(self.capital_rial), finite_number(self.max_loss_rial)
        if not capital.is_integer() or not loss.is_integer() or loss >= capital:
            raise ValueError("سرمایه و سقف زیان باید ریال صحیح باشند؛ سقف زیان کمتر از سرمایه است")

    @property
    def equity_floor_rial(self) -> int:
        return self.capital_rial - self.max_loss_rial


def plan_trade(*, profile: RiskProfile, entry_rial: float, stop_rial: float, target_rial: float,
               trade_loss_budget_rial: float, available_cash_rial: float, equity_rial: float,
               daily_value_rial: Optional[float], reserved_risk_rial: float = 0,
               requested_quantity: Optional[int] = None, data_current: bool = False,
               account_complete: bool = False, slippage_bps: Optional[float] = None,
               locked_up: bool = False, locked_down: bool = False) -> dict:
    """Size an explicitly user-specified plan under cash, total-loss and participation constraints.

    Stop execution is a scenario, not a bound on real losses. Unknown liquidity/freshness/account
    state blocks sizing. Per-trade loss budget is supplied by the user, never inferred from capital.
    """
    entry, stop, target = map(finite_number, (entry_rial, stop_rial, target_rial))
    if not stop < entry < target:
        raise ValueError("برای برنامه خرید، حد زیان < ورود < هدف لازم است")
    budget = finite_number(trade_loss_budget_rial)
    cash, equity, reserved = [finite_number(v, positive=False) for v in
                              (available_cash_rial, equity_rial, reserved_risk_rial)]
    slip = finite_number(settings.BACKTEST_CONFIG["default_slippage_bps"] if slippage_bps is None else slippage_bps,
                         positive=False) / 10000
    if slip >= 1:
        raise ValueError("لغزش باید کمتر از صد درصد باشد")
    buy, sell = settings.FEES["buy"], settings.FEES["sell"]
    buy_price, exit_stop, exit_target = entry * (1 + slip), stop * (1 - slip), target * (1 - slip)
    cost = buy_price * (1 + buy)
    loss = cost - exit_stop * (1 - sell)
    reward = exit_target * (1 - sell) - cost
    remaining = max(0, equity - profile.equity_floor_rial - reserved)
    blockers = []
    if not data_current:
        blockers.append("داده قیمت/نقدشوندگی به‌روز تأیید نشده است")
    if not account_complete:
        blockers.append("کامل بودن نقدینگی و پوزیشن‌های این دفتر تأیید نشده است")
    if locked_up or locked_down:
        blockers.append("قفل صف در آخرین داده مشاهده شده است؛ امکان ورود/خروج تأیید نشده است")
    if remaining <= 0:
        blockers.append("سقف زیان کل یا بودجه ریسک باقیمانده مصرف شده است")
    participation = min(0.05, settings.BACKTEST_CONFIG["max_volume_participation_pct"])
    try:
        daily_value = finite_number(daily_value_rial)
    except ValueError:
        daily_value = None
        blockers.append("ارزش معاملات معتبر برای کنترل نقدشوندگی موجود نیست")
    caps = {"cash": math.floor(cash / cost), "loss_budget": math.floor(min(budget, remaining) / loss),
            "liquidity": math.floor(daily_value * participation / buy_price) if daily_value else 0}
    if requested_quantity is not None:
        qty = finite_number(requested_quantity)
        if not qty.is_integer():
            raise ValueError("تعداد سهم باید صحیح باشد")
        caps["requested"] = int(qty)
    quantity = 0 if blockers else min(caps.values())
    if not blockers and quantity < 1:
        blockers.append("نقدینگی، نقدشوندگی یا بودجه ریسک برای یک سهم کافی نیست")
    return {"quantity": quantity, "blockers": blockers, "caps": caps, "remaining_loss_budget_rial": remaining,
            "entry_cost_rial": quantity * cost, "stop_loss_scenario_rial": quantity * loss,
            "target_net_scenario_rial": quantity * reward, "net_reward_risk": reward / loss,
            "breakeven_exit_rial": cost / ((1 - sell) * (1 - slip)),
            "fee_only_breakeven_pct": breakeven_move(), "participation_cap": participation,
            "fees": {"buy": buy, "sell": sell}, "slippage_bps": slip * 10000,
            "equity_floor_rial": profile.equity_floor_rial, "capital_at_risk_rial": quantity * cost,
            "stop_guaranteed": False, "calculation_version": "manual-plan-v1"}
