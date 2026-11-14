"""Dated rights marks: no-activity carry values and later trades are not quotes."""

import math
import re

from core.timeutils import deven_to_iso, is_canonical_iso_date

VERSION = 'dated-rights-quote-v1'


def dated_rights_quote(observation, *, ins_code, day, issue_start):
    """Read one day of one issue from an archived native-history projection.

    The caller establishes the issue identity/window independently. This checks
    the selected row and never uses a later row's numbers as a terminal mark.
    A carried official close with zero activity is retained only as an excluded
    source field, not a trade, liquidation price, or zero-valued entitlement.
    """
    if (type(ins_code) is not str or not re.fullmatch(r'[1-9][0-9]{0,29}', ins_code)
            or not is_canonical_iso_date(day) or not is_canonical_iso_date(issue_start)
            or issue_start > day):
        raise ValueError('One canonical rights identity and a date within its issue are required')
    result = dict(version=VERSION, ins_code=ins_code, day=day, issue_start=issue_start,
        status='unknown_source', market_mark_rial=None, last_trade_rial=None,
        excluded_nontrade_close_rial=None, execution_price_rial=None,
        historical_coverage_verified=False, actual_execution_verified=False,
        feature_eligible=False, accepted_for_signals=False)
    if observation is None:
        return result
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc'
            or str(observation.get('ins_code')) != ins_code or observation.get('price_unit') != 'Rial'
            or not is_canonical_iso_date(observation.get('start_date'))
            or not is_canonical_iso_date(observation.get('end_date_exclusive'))
            or not observation['start_date'] <= day < observation['end_date_exclusive']):
        raise ValueError('The source identity, Rial unit and dated projection must match')
    payload = observation.get('payload')
    rows = payload.get('closingPriceDaily') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ValueError('Bounded native history rows are required')
    selected = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('dEven')) is not int:
            raise ValueError('Every source row needs a definite numeric date')
        native_day = deven_to_iso(row['dEven'])
        if not is_canonical_iso_date(native_day):
            raise ValueError('A malformed date cannot be silently excluded from the source')
        if native_day == day:
            selected.append(row)
    if not selected:
        return dict(result, status='no_observed_row')
    if len(selected) != 1:
        return dict(result, status='ambiguous_duplicate_day')
    row = selected[0]
    if type(row.get('insCode')) not in (str, int) or str(row['insCode']) != ins_code:
        return dict(result, status='invalid_row_identity')

    def number(field):
        value = row.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 2**53:
            raise ValueError('Finite nonnegative native numbers are required')
        return value

    try:
        volume, value, trades = [number(f) for f in ('qTotTran5J', 'qTotCap', 'zTotTran')]
        opening, high, low, last, official = [number(f) for f in
            ('priceFirst', 'priceMax', 'priceMin', 'pDrCotVal', 'pClosing')]
        if trades % 1 or volume % 1:
            raise ValueError('Trade count and rights volume must be integral')
        if volume == value == trades == 0:
            if opening != 0 or high != 0 or low != 0:
                return dict(result, status='inconsistent_zero_activity')
            return dict(result, status='observed_zero_activity_not_a_quote',
                        excluded_nontrade_close_rial=official if official > 0 else None)
        if (min(volume, value, trades, opening, high, low, last, official) <= 0
                or trades > volume or not low <= opening <= high
                or not low <= last <= high or not low <= value / volume <= high):
            return dict(result, status='inconsistent_trade_row')
        return dict(result, status='observed_dated_trade_mark', market_mark_rial=official,
                    last_trade_rial=last)
    except (ValueError, OverflowError, TypeError):
        return dict(result, status='invalid_numeric_row')
