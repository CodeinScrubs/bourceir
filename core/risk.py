"""Manual plan arithmetic in rial. No stock selector, signal or brokerage action."""

import math
from dataclasses import dataclass
from typing import Optional

from config import settings
from core.market_rules import breakeven_move
from core.screening import current_snapshot
from core.timeutils import is_market_open, now_tehran


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


def intraday_entry_constraints(snapshot, entry_rial, *, at=None, buy_price_rial=None, calendar=None):
    """A fresh visible ask is necessary, never proof of a fill or a future exit.

    Provider time is the quote/trade time supplied by TSETMC; no independent book clock,
    instrument trading-state feed or queue priority is available in this snapshot format.
    """
    at = at or now_tehran()
    blockers, caps = [], {}
    if at.tzinfo is None:
        raise ValueError('زمان باید دارای منطقه زمانی باشد')
    if not is_market_open(at, calendar=calendar):
        blockers.append('بازار خارج از ساعات معاملات است')
    if not snapshot:
        return ['اسنپ‌شات تازه عمق بازار موجود نیست'] + blockers, caps
    for name in ('provider_timestamp', 'captured_at'):
        if not current_snapshot(snapshot.get(name), at):
            blockers.append('زمان قیمت یا دریافت عمق بازار تازه تأیید نشده است')
            break
    try:
        keys = ('price_limit_down', 'price_limit_up', 'ask_price_1', 'ask_vol_1',
                'ask_count_1', 'bid_price_1', 'bid_vol_1', 'bid_count_1', 'last_price', 'value', 'volume')
        numbers = {key: finite_number(snapshot.get(key)) for key in keys}
        lower, upper, ask, bid = [numbers[key] for key in ('price_limit_down', 'price_limit_up', 'ask_price_1', 'bid_price_1')]
        if not lower <= bid <= ask <= upper or not lower <= entry_rial <= upper or not lower <= numbers['last_price'] <= upper:
            blockers.append('مظنه یا قیمت ورود با دامنه مجاز امروز سازگار نیست')
        if entry_rial < ask:
            blockers.append('در قیمت ورود انتخابی عرضه قابل مشاهده وجود ندارد')
        caps['displayed_ask'] = math.floor(numbers['ask_vol_1'])
        participation = min(0.05, settings.BACKTEST_CONFIG['max_volume_participation_pct'])
        participation_price = max(entry_rial, finite_number(buy_price_rial) if buy_price_rial is not None else entry_rial)
        caps['intraday_value'] = math.floor(numbers['value'] * participation / participation_price)
        caps['intraday_volume'] = math.floor(numbers['volume'] * participation)
        for key in ('buy_queue_volume', 'sell_queue_volume'):
            if finite_number(snapshot.get(key), positive=False) > 0:
                blockers.append('صف در داده تازه مشاهده شد؛ امکان اجرای برنامه درون‌روزی تأیید نشده است')
                break
    except ValueError:
        blockers.append('عرضه، تقاضا یا دامنه/ارزش معاملات امروز نامعلوم یا نامعتبر است')
    return blockers, caps


def plan_trade(*, profile: RiskProfile, entry_rial: float, stop_rial: float, target_rial: float,
               trade_loss_budget_rial: float, available_cash_rial: float, equity_rial: Optional[float],
               daily_value_rial: Optional[float], reserved_risk_rial: Optional[float] = 0,
               requested_quantity: Optional[int] = None, data_current: bool = False,
               account_complete: bool = False, slippage_bps: Optional[float] = None,
               locked_up: bool = False, locked_down: bool = False,
               intraday: bool = False, intraday_snapshot: Optional[dict] = None, at=None, calendar=None) -> dict:
    """Size an explicitly user-specified plan under cash, total-loss and participation constraints.

    Stop execution is a scenario, not a bound on real losses. Unknown liquidity/freshness/account
    state blocks sizing. Per-trade loss budget is supplied by the user, never inferred from capital.
    """
    entry, stop, target = map(finite_number, (entry_rial, stop_rial, target_rial))
    if not stop < entry < target:
        raise ValueError("برای برنامه خرید، حد زیان < ورود < هدف لازم است")
    budget = finite_number(trade_loss_budget_rial)
    cash = finite_number(available_cash_rial, positive=False)
    equity = None if equity_rial is None else finite_number(equity_rial, positive=False)
    reserved = None if reserved_risk_rial is None else finite_number(reserved_risk_rial, positive=False)
    slip = finite_number(settings.BACKTEST_CONFIG["default_slippage_bps"] if slippage_bps is None else slippage_bps,
                         positive=False) / 10000
    if slip >= 1:
        raise ValueError("لغزش باید کمتر از صد درصد باشد")
    buy, sell = settings.FEES["buy"], settings.FEES["sell"]
    buy_price, exit_stop, exit_target = entry * (1 + slip), stop * (1 - slip), target * (1 - slip)
    if intraday and intraday_snapshot:
        try:
            buy_price = min(buy_price, finite_number(intraday_snapshot.get('price_limit_up')))
        except ValueError:
            pass  # The live guard below blocks an unknown/invalid band.
    cost = buy_price * (1 + buy)
    loss = cost - exit_stop * (1 - sell)
    reward = exit_target * (1 - sell) - cost
    remaining = None if equity is None or reserved is None else max(0, equity - profile.equity_floor_rial - reserved)
    blockers = []
    if loss <= 0:
        blockers.append('بهای ورود و سناریوی زیان با داده موجود سازگار نیست؛ بودجه این معامله نامعلوم است')
    if not data_current:
        blockers.append("داده قیمت/نقدشوندگی به‌روز تأیید نشده است")
    if not account_complete:
        blockers.append("کامل بودن نقدینگی و پوزیشن‌های این دفتر تأیید نشده است")
    if not intraday and (locked_up or locked_down):
        blockers.append("قفل صف در آخرین داده مشاهده شده است؛ امکان ورود/خروج تأیید نشده است")
    if remaining is None:
        blockers.append("ارزش روز دارایی‌ها یا ریسک باز دفتر نامعلوم است؛ بودجه ریسک باقیمانده قابل محاسبه نیست")
    elif remaining <= 0:
        blockers.append("سقف زیان کل یا بودجه ریسک باقیمانده مصرف شده است")
    participation = min(0.05, settings.BACKTEST_CONFIG["max_volume_participation_pct"])
    try:
        daily_value = finite_number(daily_value_rial)
    except ValueError:
        daily_value = None
        blockers.append("ارزش معاملات معتبر برای کنترل نقدشوندگی موجود نیست")
    caps = {"cash": math.floor(cash / cost), "loss_budget": math.floor(min(budget, remaining) / loss) if remaining is not None and loss > 0 else None,
            "liquidity": math.floor(daily_value * participation / buy_price) if daily_value is not None else None}
    if intraday:
        live_blockers, live_caps = intraday_entry_constraints(intraday_snapshot, entry, at=at, buy_price_rial=buy_price,
                                                            calendar=calendar)
        blockers.extend(live_blockers)
        caps.update(live_caps)
        if intraday_snapshot:
            try:
                lower = finite_number(intraday_snapshot.get('price_limit_down'))
                upper = finite_number(intraday_snapshot.get('price_limit_up'))
                if not lower <= stop < entry < target <= upper:
                    blockers.append('حد خروج یا هدف در دامنه مجاز امروز نیست؛ برنامه همان‌روز قابل محاسبه نیست')
            except ValueError:
                pass
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
            "target_net_scenario_rial": quantity * reward, "net_reward_risk": reward / loss if loss > 0 else None,
            "breakeven_exit_rial": cost / ((1 - sell) * (1 - slip)),
            "fee_only_breakeven_pct": breakeven_move(), "participation_cap": participation,
            "fees": {"buy": buy, "sell": sell}, "slippage_bps": slip * 10000,
            "equity_floor_rial": profile.equity_floor_rial, "capital_at_risk_rial": quantity * cost,
            "stop_guaranteed": False, "fill_guaranteed": False, "calculation_version": "manual-plan-v5",
            "calendar_exceptions": dict(calendar or {}),
            "intraday_evidence": dict(intraday_snapshot) if intraday and intraday_snapshot else None}
