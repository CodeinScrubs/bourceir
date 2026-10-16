"""Weighted average cost ledger for explicitly recorded manual executions, in rial."""

from config import settings
from core.risk import RiskProfile, finite_number


def summarize_executions(events: list, profile: RiskProfile, marks: dict, *, sell_fee_rate=None,
                         opening_positions=None, opening_cash_rial=None) -> dict:
    sell_fee = finite_number(settings.FEES['sell'] if sell_fee_rate is None else sell_fee_rate, positive=False)
    if sell_fee >= 1:
        raise ValueError('نرخ کارمزد فروش باید کمتر از یک باشد')
    if opening_positions is not None and opening_cash_rial is None:
        raise ValueError('نقد آغازین باید جدا از ارزش موجودی آغازین مشخص باشد')
    cash = float(profile.capital_rial) if opening_cash_rial is None else finite_number(opening_cash_rial, positive=False)
    realized, fees = 0.0, 0.0
    holdings = {}
    for symbol, position in (opening_positions or {}).items():
        quantity = finite_number(position['quantity'])
        cost = finite_number(position['cost_rial'])
        if not quantity.is_integer():
            raise ValueError('تعداد موجودی آغازین باید صحیح باشد')
        holdings[symbol] = dict(quantity=int(quantity), cost_rial=cost, plan_ids=[])
    for event in events:
        symbol = event["symbol_fa"]
        quantity, price = finite_number(event["quantity"]), finite_number(event["price_rial"])
        fee = finite_number(event["fee_rial"], positive=False)
        if not quantity.is_integer() or event["side"] not in ("buy", "sell"):
            raise ValueError("اجرای دفتر معتبر نیست")
        pos = holdings.setdefault(symbol, {"quantity": 0, "cost_rial": 0.0, "plan_ids": []})
        gross = quantity * price
        if fee > gross:
            raise ValueError("کارمزد ثبت‌شده از ارزش اجرا بیشتر است")
        if event["side"] == "buy":
            if gross + fee > cash + 1e-6:
                raise ValueError("خرید ثبت‌شده از نقدینگی دفتر بیشتر است")
            cash = max(0.0, cash - (gross + fee))
            pos["quantity"] += int(quantity)
            pos["cost_rial"] += gross + fee
            if event.get("plan_id") not in pos["plan_ids"]:
                pos["plan_ids"].append(event.get("plan_id"))
        else:
            if quantity > pos["quantity"]:
                raise ValueError("فروش بیش از موجودی سهم مجاز نیست")
            basis = pos["cost_rial"] * quantity / pos["quantity"]
            cash += gross - fee
            realized += gross - fee - basis
            pos["quantity"] -= int(quantity)
            pos["cost_rial"] -= basis
            if not pos["quantity"]:
                pos["cost_rial"], pos["plan_ids"] = 0.0, []
        fees += fee
    holdings = {s: p for s, p in holdings.items() if p["quantity"]}
    unknown, marked = [], 0.0
    for symbol, pos in holdings.items():
        try:
            price = finite_number(marks.get(symbol))
        except ValueError:
            unknown.append(symbol)
            continue
        # Mark to net liquidation value; the price is observed, an immediate fill is not promised.
        value = pos["quantity"] * price * (1 - sell_fee)
        pos["mark_rial"], pos["liquidation_value_rial"] = price, value
        pos["unrealized_net_rial"] = value - pos["cost_rial"]
        marked += value
    equity = None if unknown else cash + marked
    return {"cash_rial": cash, "realized_net_rial": realized, "fees_rial": fees,
            "positions": holdings, "equity_rial": equity, "unknown_marks": unknown,
            "total_net_rial": None if equity is None else equity - profile.capital_rial,
            "loss_limit_reached": None if equity is None else equity <= profile.equity_floor_rial,
            "estimated_fee_events": sum(e.get("fee_source") == "config_estimate" for e in events)}
