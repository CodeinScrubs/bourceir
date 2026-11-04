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
    days = np.asarray(calendar).astype(str)
    if not len(days) or list(days) != sorted(set(days)):
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
    status, exit_date = f"label_status_{horizon}d", f"exit_date_{horizon}d"
    within = (panel["date"] >= fold.train_start) & (panel["date"] < fold.train_end_exclusive)
    complete = (panel[status] == "complete") & (panel[exit_date] != "") & (panel[exit_date] < fold.train_end_exclusive)
    blocked = (panel[status] == "entry_blocked") & (panel["entry_date"] != "") & (panel["entry_date"] < fold.train_end_exclusive)
    return panel.loc[within & (complete | blocked)].copy()
