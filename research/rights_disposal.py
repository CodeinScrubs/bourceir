"""Observed-bar rights-sale scenario; no broker receipt, share credit or zero imputation."""

from fractions import Fraction

import pandas as pd

from core.market_rules import lock_flags
from core.price_quality import price_quality
from core.timeutils import is_canonical_iso_date

VERSION = 'observed-rights-disposal-v1'


def _ratio(value, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Fraction)):
        raise ValueError('Explicit bounded numeric scenario inputs required')
    try:
        number = value if isinstance(value, Fraction) else Fraction(str(value))
    except (ValueError, ZeroDivisionError, OverflowError) as error:
        raise ValueError('Finite numeric inputs required') from error
    if number < 0 or (not allow_zero and number == 0) or number >= 2**53:
        raise ValueError('Positive scenario input outside the exact numeric domain')
    return number


def _rational(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def model_rights_disposal(prices, *, units, start, end_exclusive, sell_fee, participation=.05,
                          stress=False, slippage_bps=0):
    """Sell whole rights across earliest usable observed bars within capacity.

Ownership/credit and completeness of the supplied history must be evidenced by
the caller. Existing daily lock heuristics are a conservative execution proxy,
not observed queues. Normal sells use open, stress uses low, on identical input
dates and budgets. Fractional or unpriced remaining units retain unknown value.
No exercise/payment or ordinary-share substitution exists in this path.
"""
    units, fee, cap = _ratio(units), _ratio(sell_fee, allow_zero=True), _ratio(participation)
    slip = _ratio(slippage_bps, allow_zero=True)/10000
    if not 0 <= fee < 1 or not 0 < cap <= Fraction(1, 20) or type(stress) is not bool or slip >= 1:
        raise ValueError('Fee, 5% capacity ceiling or execution scenario is invalid')
    if not is_canonical_iso_date(start) or not is_canonical_iso_date(end_exclusive) or start >= end_exclusive:
        raise ValueError('Canonical ordered sale interval required')
    required = {'date', 'open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
                'ref_price_raw', 'volume', 'value'}
    if (not isinstance(prices, pd.DataFrame) or prices.columns.duplicated().any()
            or not required <= set(prices) or len(prices) > 50000 or prices.date.duplicated().any()
            or any(not is_canonical_iso_date(d) for d in prices.date)):
        raise ValueError('Unique bounded daily bars for one rights identity are required')
    for identity in ('symbol', 'ins_code'):
        if identity in prices and (prices[identity].isna().any() or prices[identity].nunique() > 1):
            raise ValueError('Different rights identities cannot share a disposal tape')
    frame = prices.loc[prices.date.ge(start) & prices.date.lt(end_exclusive)].sort_values('date').copy()
    numeric, flags = price_quality(frame)
    locks = lock_flags(frame)
    remaining, gross, fees = units, Fraction(0), Fraction(0)
    fills, decisions = [], []
    unknown = False
    for index, row in frame.iterrows():
        if remaining < 1:
            break
        vol, value = numeric.loc[index, ['volume', 'value']]
        if pd.isna(vol) or pd.isna(value) or vol < 0 or value < 0:
            decisions.append(dict(date=row.date, status='unknown_activity'))
            unknown = True
            break
        if vol == 0 and value == 0:
            decisions.append(dict(date=row.date, status='observed_zero_activity'))
            continue
        if flags.loc[index].any():
            decisions.append(dict(date=row.date, status='unknown_or_inconsistent_raw_bar'))
            unknown = True
            break
        if bool(locks.loc[index, 'locked_down']):
            decisions.append(dict(date=row.date, status='no_fill_assumed_locked_sell_proxy'))
            continue
        quoted = _ratio(row.low_raw if stress else row.open_raw)
        price = max(_ratio(row.low_raw), min(_ratio(row.high_raw), quoted*(1-slip)))
        volume, value = _ratio(vol), _ratio(value)
        quantity = int(min(remaining, volume * cap, value * cap / price))
        if quantity <= 0:
            decisions.append(dict(date=row.date, status='capacity_below_one_right'))
            continue
        amount = quantity * price
        charge = amount * fee
        fills.append(dict(date=row.date, quantity=quantity, price_rial=float(price),
            gross_rial=float(amount), fee_rial=float(charge), net_rial=float(amount-charge),
            participation_volume=float(Fraction(quantity)/volume),
            participation_value=float(amount/value), execution_verified=False))
        remaining -= quantity
        gross += amount
        fees += charge
    net = gross-fees
    return dict(version=VERSION, status='unknown_input_path' if unknown else
        'modeled_complete_whole_units' if remaining < 1 else 'unpriced_or_unsold_units',
        scenario='stress_low' if stress else 'normal_open',
        initial_units=_rational(units), sold_units=sum(fill['quantity'] for fill in fills),
        remaining_units=_rational(remaining), modeled_gross_rial=float(gross),
        modeled_fee_rial=float(fees), modeled_net_cash_rial=float(net), modeled_net_cash_exact=_rational(net),
        cash_added_for_exercise_rial=0, ordinary_shares_created=0,
        remaining_value_rial=None if remaining else 0,
        entire_path_proceeds_rial=None if unknown or remaining else float(net),
        first_fill_date=fills[0]['date'] if fills else None, last_fill_date=fills[-1]['date'] if fills else None,
        fills=fills, decisions=decisions, actual_receipt_verified=False, actual_fills_verified=False,
        entitlement_verified=False, complete_source_coverage_verified=False, accepted_for_signals=False)
