"""Explicit, descriptive mark of a hypothetical purchase and subscription rights.

This does not read accounts, establish entitlement, simulate a fill or create
an order. Prices and the issue ratio are supplied assumptions, not evidence.
"""

from datetime import date
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from core.text import normalize_symbol


def _positive_integer(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _day(value, name):
    if not isinstance(value, str):
        raise ValueError(f'{name} must be an ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be an ISO date') from exc
    if parsed.isoformat() != value:
        raise ValueError(f'{name} must be a canonical ISO date')
    return parsed


def _fee(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f'{name} must be an exact decimal rate')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be an exact decimal rate') from exc
    if not result.is_finite() or not Decimal(0) <= result < Decimal(1):
        raise ValueError(f'{name} must be finite and between zero and one')
    return result


def hypothetical_rights_mark(spec):
    """Value one assumed issue at same-date last prices, with explicit costs.

    Rights are rounded down for the scenario only; neither that convention nor
    actual ownership, execution, omitted distributions, or quote identity is
    independently verified. Monetary result strings are in rial.
    """
    required = {
        'symbol', 'rights_symbol', 'budget_rial', 'purchase_date', 'purchase_price_rial',
        'purchase_fee_rate', 'old_shares', 'new_rights', 'valuation_date',
        'share_quote_date', 'share_quote_rial', 'rights_quote_date', 'rights_quote_rial',
        'assumed_sale_fee_rate',
    }
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError('Rights mark requires the exact explicit input fields')
    symbols = []
    for field in ('symbol', 'rights_symbol'):
        value = spec[field]
        if not isinstance(value, str) or not normalize_symbol(value):
            raise ValueError(f'{field} is required')
        symbols.append(normalize_symbol(value))
    if symbols[0] == symbols[1]:
        raise ValueError('Share and rights symbols must differ')
    start = _day(spec['purchase_date'], 'purchase_date')
    valuation = _day(spec['valuation_date'], 'valuation_date')
    if start > valuation or any(_day(spec[key], key) != valuation for key in ('share_quote_date', 'rights_quote_date')):
        raise ValueError('Purchase chronology and both quote dates must match the valuation')
    numbers = {key: _positive_integer(spec[key], key) for key in (
        'budget_rial', 'purchase_price_rial', 'old_shares', 'new_rights', 'share_quote_rial', 'rights_quote_rial')}
    buy_fee = _fee(spec['purchase_fee_rate'], 'purchase_fee_rate')
    sell_fee = None if spec['assumed_sale_fee_rate'] is None else _fee(spec['assumed_sale_fee_rate'], 'assumed_sale_fee_rate')
    budget, price = Decimal(numbers['budget_rial']), Decimal(numbers['purchase_price_rial'])
    shares = int((budget / (price * (1 + buy_fee))).to_integral_value(rounding=ROUND_FLOOR))
    if shares == 0:
        raise ValueError('Budget is insufficient for one share including the assumed purchase fee')
    rights = shares * numbers['new_rights'] // numbers['old_shares']
    spent = shares * price * (1 + buy_fee)
    cash = budget - spent
    share_value = Decimal(shares * numbers['share_quote_rial'])
    rights_value = Decimal(rights * numbers['rights_quote_rial'])
    mark = cash + share_value + rights_value
    assumed_liquidation = None if sell_fee is None else cash + (share_value + rights_value) * (1 - sell_fee)
    result = dict(
        version='hypothetical-rights-mark-v1', symbol=symbols[0], rights_symbol=symbols[1],
        valuation_date=valuation.isoformat(), shares=shares, assumed_rights=rights,
        purchase_cost_rial=str(spent), residual_cash_rial=str(cash),
        share_mark_rial=str(share_value), rights_mark_rial=str(rights_value),
        total_mark_rial=str(mark), marked_profit_rial=str(mark-budget),
        marked_return_pct=str((mark/budget-1)*100),
        assumed_liquidation_rial=str(assumed_liquidation) if assumed_liquidation is not None else None,
        assumed_liquidation_profit_rial=str(assumed_liquidation-budget) if assumed_liquidation is not None else None,
        rights_rounding='floor_assumption_only', quote_kind='user_supplied_assumption',
        entitlement_verified=False, quote_identity_verified=False, same_time_verified=False,
        execution_verified=False, liquidity_verified=False, dividends_included=False,
        other_corporate_actions_included=False, realized_profit=False, signals_enabled=False,
    )
    return result
