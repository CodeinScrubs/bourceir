"""Checks on observed book prices; a passing check never proves a live fill."""

import math
from numbers import Real


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def book_bound_violations(snapshot):
    """Return contradictions only when both positive price bounds are known.

    Missing/zero bounds do not make an out-of-range level provable. Zero prices
    denote empty levels and are not promoted into real book observations.
    """
    if not isinstance(snapshot, dict):
        return []
    low, high = (_number(snapshot.get(key)) for key in ('price_limit_down', 'price_limit_up'))
    if low is None or high is None or not 0 < low <= high:
        return []
    violations = []
    for side in ('bid', 'ask'):
        for level in range(1, 6):
            field = f'{side}_price_{level}'
            price = _number(snapshot.get(field))
            if price is not None and price > 0 and not low <= price <= high:
                violations.append(dict(field=field, price_rial=price, lower_rial=low, upper_rial=high))
    return violations
