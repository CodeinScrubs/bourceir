"""Fixed, daily paper-research rule recovered from the Aban07 study."""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.market_rules import pit_daily_returns

GENOME = (0, -3, 2, 2, 0, 0, 5, 0)
STRIDE = 21
COEFFICIENTS = np.array([-3.0, 2.0, 2.0])
LOOKBACKS = (21, 63)
MINIMUM_COUNTS = (17, 50)


def score_inputs(market: pd.DataFrame, sessions, instruments) -> tuple[pd.DataFrame, ...]:
    """Form features on the supplied market calendar and stable instrument IDs."""
    if len(set(instruments)) != len(instruments) or len(sessions) == 0 or list(sessions) != sorted(set(sessions)):
        raise ValueError('A sorted market calendar and unique instrument IDs are required')
    rows = market.loc[market.ins_code.isin(instruments) & market.date.isin(sessions)]
    if rows.duplicated(['date', 'ins_code']).any():
        raise ValueError('Duplicate market rows')
    eligible = rows.volume.gt(0) & rows.trade_count.gt(0)
    daily = pit_daily_returns(rows).where(eligible)
    log_returns = np.log1p(daily.where(daily.gt(-1)))
    panel = rows.assign(log_return=log_returns).pivot(index='date', columns='ins_code', values='log_return')
    panel = panel.reindex(index=sessions, columns=instruments)
    m21 = panel.rolling(LOOKBACKS[0], min_periods=MINIMUM_COUNTS[0])
    m63 = panel.rolling(LOOKBACKS[1], min_periods=MINIMUM_COUNTS[1])
    return tuple(feature.where(panel.notna()) for feature in (m21.sum(), m63.sum(), -m21.std()))


def rank_scores(features: tuple[pd.DataFrame, ...]) -> np.ndarray:
    """Rank each input separately. Missing inputs remain ineligible for selection."""
    ranks = [feature.rank(axis=1, method='average', pct=True).to_numpy(float) - .5 for feature in features]
    return np.stack(ranks, axis=-1) @ COEFFICIENTS / np.abs(COEFFICIENTS).sum()


def targets(scores: np.ndarray, *, exposure: float = 1.0, include_latest: bool = False) -> np.ndarray:
    """Emit a target at session indices 0, 21, ... . Ties follow the input ID order."""
    if scores.ndim != 2 or not 0 < exposure <= 1:
        raise ValueError('Invalid rotation score grid or exposure')
    result = np.full(scores.shape, np.nan)
    limit = len(scores) if include_latest else len(scores) - 1
    for i in range(0, limit, STRIDE):
        eligible = np.flatnonzero(np.isfinite(scores[i]))
        if eligible.size:
            winner = eligible[np.argmax(scores[i, eligible])]
            result[i] = 0.
            result[i, winner] = exposure
    return result
