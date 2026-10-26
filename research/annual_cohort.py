"""Historical first-observation census for a fixed annual research calendar.

Use only independently prevalidated, symbol-normalized source keys. Unlike an
anchor-day inner filter, a previously observed symbol without a bar on the
anchor remains an opportunity with a missing-anchor/unknown event label.
This is NOT a historical listing or active-membership registry: older delisted
symbols are intentionally retained as unknown until primary evidence exists.
"""

import pandas as pd

VERSION = "first-observed-anchor-cohort-v1"


def first_observed_cohort(validated_prices, anchor_dates):
    """Build all (previously-seen symbol, anchor-date) pairs without future membership.

    The caller must pass `rare_events._panel`'s normalized and unique price
    keys; numeric price/volume quality does not affect census membership.
    The same ordered anchor dates are used for every symbol, including days
    with no matching source row. This avoids conditioning on an observed
    anchor-day bar (a potentially outcome-dependent selection mechanism).
    """
    if (not isinstance(validated_prices, pd.DataFrame)
            or validated_prices.columns.duplicated().any()
            or not {'symbol', 'date'} <= set(validated_prices)):
        raise ValueError("Normalized source symbol/date keys are required")
    if (not isinstance(anchor_dates, list) or not anchor_dates
            or any(type(day) is not str for day in anchor_dates)
            or anchor_dates != sorted(set(anchor_dates))):
        raise ValueError("Anchor dates must be an ordered unique nonempty list")
    keys = validated_prices[['symbol', 'date']]
    if (keys.isna().any().any()
            or not keys['symbol'].map(lambda symbol: type(symbol) is str and bool(symbol)).all()
            or not keys['date'].map(lambda day: type(day) is str and bool(day)).all()
            or keys.duplicated(['symbol', 'date']).any()):
        raise ValueError("Source keys must be complete and unique")
    first_seen = keys.groupby('symbol', sort=True)['date'].min()
    opportunities = []
    for anchor in anchor_dates:
        symbols = first_seen.index[first_seen.le(anchor)].tolist()
        if symbols:
            opportunities.append(pd.DataFrame({'symbol': symbols, 'date': anchor}))
    if not opportunities:
        raise ValueError("No historically observed symbol exists at the selected anchors")
    return pd.concat(opportunities, ignore_index=True).sort_values(
        ['symbol', 'date']).reset_index(drop=True)
