"""Chronological expanding-window folds with realized-label purge and a session embargo."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.timeutils import is_canonical_iso_date


@dataclass(frozen=True)
class TimeFold:
    train_start: str
    train_end_exclusive: str
    test_start: str
    test_end_exclusive: str
    embargo_sessions: int

    def __post_init__(self):
        dates = (self.train_start, self.train_end_exclusive, self.test_start, self.test_end_exclusive)
        if (not all(is_canonical_iso_date(day) for day in dates)
                or not self.train_start < self.train_end_exclusive <= self.test_start < self.test_end_exclusive
                or type(self.embargo_sessions) is not int or self.embargo_sessions < 0):
            raise ValueError('Canonical ordered temporal boundaries and a nonnegative session embargo are required')


def preceding_price_observations(prices: pd.DataFrame, before: str) -> pd.DataFrame:
    """Return each symbol's latest actual prior bar, independently of source row order."""
    if (not isinstance(prices, pd.DataFrame) or prices.columns.duplicated().any()
            or not {'date', 'symbol'} <= set(prices) or not is_canonical_iso_date(before)):
        raise ValueError('ستون‌های نماد و تاریخ و مرز معتبر برای قیمت پیشین لازم است')
    if any(not is_canonical_iso_date(day) for day in prices.date.unique()):
        raise ValueError('تاریخ قیمت پیشین باید میلادی معتبر باشد')
    earlier = prices.loc[prices.date.lt(before)]
    if earlier.duplicated(['symbol', 'date']).any():
        raise ValueError('قیمت پیشین با نماد/تاریخ تکراری نامعتبر است')
    return earlier.sort_values(['symbol', 'date']).groupby('symbol', sort=False).tail(1)


def time_folds(calendar, *, min_train_sessions=504, test_sessions=252, embargo_sessions=6, end_exclusive):
    days = np.asarray(calendar)
    if (days.ndim != 1 or not len(days) or not is_canonical_iso_date(end_exclusive)
            or any(not is_canonical_iso_date(day) for day in days)
            or list(days) != sorted(set(days))):
        raise ValueError("تقویم یکتا و مرتب لازم است")
    for value in (min_train_sessions, test_sessions, embargo_sessions):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("اندازه پنجره باید عدد صحیح نامنفی باشد")
    if min_train_sessions < 1 or test_sessions < 1:
        raise ValueError("پنجره آموزش و آزمون باید مثبت باشد")
    days = days[days < end_exclusive]
    offset = min_train_sessions + embargo_sessions
    folds = []
    while offset < len(days):
        stop = min(offset + test_sessions, len(days))
        # Skip an undersized last fold rather than adjust windows after seeing its performance.
        if stop - offset < max(20, test_sessions // 2):
            break
        folds.append(TimeFold(days[0], days[offset - embargo_sessions], days[offset],
                              days[stop] if stop < len(days) else end_exclusive, embargo_sessions))
        offset = stop
    if not folds:
        raise ValueError("جلسه کافی برای آموزش/آزمون زمانی وجود ندارد")
    return folds


def purged_training(panel: pd.DataFrame, fold: TimeFold, horizon: int) -> pd.DataFrame:
    """Only complete realized labels ending strictly before the training boundary are used.

    A halted/locked position's actual exit may be months later than the nominal horizon, so purge
    uses actual exit dates. Entry-blocked labels are known idle cash if their entry date is known.
    """
    if type(horizon) is not int or horizon < 1:
        raise ValueError('A positive integer realized-label horizon is required')
    status, exit_date = f"label_status_{horizon}d", f"exit_date_{horizon}d"
    if (not isinstance(panel, pd.DataFrame) or panel.columns.duplicated().any()
            or not {'date', 'entry_date', status, exit_date} <= set(panel)
            or not panel.date.map(is_canonical_iso_date).all()):
        raise ValueError('Canonical decision dates and explicit realized-label columns are required')
    # A nonempty malformed date (e.g. "0") sorts before a real ISO boundary.
    # It is unknown evidence, never a mature label. Do not coerce it to a date.
    entry = panel.entry_date.where(panel.entry_date.map(is_canonical_iso_date), '')
    exit_at = panel[exit_date].where(panel[exit_date].map(is_canonical_iso_date), '')
    within = (panel["date"] >= fold.train_start) & (panel["date"] < fold.train_end_exclusive)
    entry_known = entry.gt(panel.date) & entry.lt(fold.train_end_exclusive)
    complete = ((panel[status] == 'complete') & entry_known & exit_at.ne('')
                & exit_at.ge(entry) & exit_at.lt(fold.train_end_exclusive))
    blocked = (panel[status] == 'entry_blocked') & entry_known
    return panel.loc[within & (complete | blocked)].copy()
