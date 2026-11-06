"""Exact-session index controls for pre-holdout research accounts."""

from collections.abc import Mapping

import numpy as np
import pandas as pd

from config import settings
from core.timeutils import is_canonical_iso_date

TREND_SESSIONS = 20
TARGET_RETURN = .30
MAX_BASE_LAG_DAYS = 10


def _session_dates(calendar):
    days = list(calendar)
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (not days or any(not is_canonical_iso_date(day) or day >= cutoff for day in days)
            or days != sorted(set(days))):
        raise ValueError('Expected a unique ordered development calendar')
    return pd.Index(days, name='date')


def _align_levels(days, levels):
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (not isinstance(levels, Mapping)
            or any(not is_canonical_iso_date(day) or day >= cutoff for day in levels)
            or any(type(value) not in (int, float) or not 0 < value < 2**53 for value in levels.values())):
        raise ValueError('Expected positive finite index points keyed by development dates')
    closing = pd.Series(levels, dtype=float).reindex(days)
    extra = sorted(day for day in set(levels).difference(days) if days[0] < day < days[-1])
    # An index date between two expected sessions belongs to their closing-to-closing interval.
    extra_before = pd.Series(False, index=days)
    if extra:
        extra_before.iloc[days.searchsorted(extra)] = True
    return closing, extra, extra_before


def dual_exchange_trend(calendar, tse_levels, ifb_levels):
    """Use only levels through each decision close, with no fill across calendar disagreements."""
    days = _session_dates(calendar)
    result = pd.DataFrame(index=days)
    for column, levels in (('tse_return_20', tse_levels), ('ifb_return_20', ifb_levels)):
        closing, _, extra_before = _align_levels(days, levels)
        complete = (closing.rolling(TREND_SESSIONS + 1).count().eq(TREND_SESSIONS + 1)
                    & extra_before.rolling(TREND_SESSIONS).sum().eq(0))
        result[column] = (closing / closing.shift(TREND_SESSIONS) - 1).where(complete)
    known = result.notna().all(axis=1)
    result['gate'] = (result.gt(0).all(axis=1)).astype('boolean').where(known)
    return result


def _interval_return(values, start, end):
    path = values.iloc[start:end+1]
    if path.isna().any() or path.iloc[0] <= 0:
        return None
    return float(path.iloc[-1] / path.iloc[0] - 1)


def compare_index_path(equity, calendar, levels):
    """Compare the same closing dates. Missing sessions and stale marks stay unknown."""
    days = _session_dates(calendar)
    closing, extra, extra_before = _align_levels(days, levels)
    columns = ['date', 'equity_rial', 'stale_positions', 'stale_fractional_exposures']
    if (not isinstance(equity, pd.DataFrame) or equity.columns.duplicated().any()
            or not set(columns) <= set(equity) or len(equity) > len(days)
            or list(equity.date) != list(days[:len(equity)])):
        raise ValueError('Account dates must be the exact prefix of the expected calendar')
    account = equity.set_index('date').reindex(days)
    marked = pd.to_numeric(account.equity_rial, errors='raise')
    fresh = account.stale_positions.eq(0) & account.stale_fractional_exposures.eq(0)
    marked = marked.where(fresh & np.isfinite(marked) & marked.ge(0))
    dates = pd.to_datetime(days)
    rows = []
    for end, target in enumerate(dates - pd.DateOffset(years=1)):
        start = dates.searchsorted(target, side='right') - 1
        if start < 0:
            continue
        lag = (target - dates[start]).days
        usable_anchor = lag <= MAX_BASE_LAG_DAYS
        account_return = _interval_return(marked, start, end) if usable_anchor else None
        index_return = (_interval_return(closing, start, end)
                        if usable_anchor and not extra_before.iloc[start+1:end+1].any() else None)
        paired = account_return is not None and index_return is not None
        rows.append(dict(date=days[end], base_date=days[start], base_lag_days=lag,
                         account_return=account_return, index_return=index_return,
                         account_minus_index=account_return-index_return if paired else None,
                         target_met=None if account_return is None else account_return >= TARGET_RETURN))
    account_return = _interval_return(marked, 0, len(days)-1)
    index_return = _interval_return(closing, 0, len(days)-1) if not extra else None
    paired = account_return is not None and index_return is not None
    summary = dict(
        expected_sessions=len(days), missing_index_dates=closing.index[closing.isna()].tolist(),
        additional_index_dates=extra, account_unavailable_sessions=int(marked.isna().sum()),
        account_first_close_to_last_close=account_return, index_first_close_to_last_close=index_return,
        account_minus_index=account_return-index_return if paired else None,
        rolling_12month_windows=len(rows),
        rolling_account_unknown=sum(row['account_return'] is None for row in rows),
        rolling_index_unknown=sum(row['index_return'] is None for row in rows),
        rolling_account_below_target=sum(row['target_met'] is False for row in rows),
        rolling_paired=sum(row['account_minus_index'] is not None for row in rows),
        rolling_account_below_index=sum(row['account_minus_index'] is not None
                                        and row['account_minus_index'] < 0 for row in rows),
    )
    return summary, rows
