"""Calendar-bounded first-hit labels for a fixed cohort; synthetic growth, never a fill or forecast."""

from datetime import date
from fractions import Fraction
from numbers import Real

import numpy as np
import pandas as pd

from config import settings
from core import market_rules
from core.text import normalize_symbol

VERSION = 'rare-event-cohort-v1'
HORIZONS = (10, 20, 60)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('تاریخ باید ISO صریح باشد')
    return value


def _panel(frame, required):
    if not required <= set(frame) or frame.columns.duplicated().any():
        raise ValueError('ستون‌های یکتای ورودی رخداد لازم‌اند')
    result = frame.loc[:, sorted(required)].copy()
    result['date'] = result.date.map(_day)
    if result.symbol.map(lambda v: not isinstance(v, str) or not normalize_symbol(v)).any():
        raise ValueError('نام نماد معتبر لازم است')
    result['symbol'] = result.symbol.map(normalize_symbol)
    if result.duplicated(['symbol', 'date']).any():
        raise ValueError('فرصت یا مشاهده تکراری پس از یکسان‌سازی نماد')
    return result.sort_values(['symbol', 'date']).reset_index(drop=True)


def _number(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        return np.nan
    return float(value) if np.isfinite(value) else np.nan


def compute_rare_events(prices, decisions, calendar, *, observed_through, horizons=HORIZONS, threshold=2.):
    """Index starts at 1 at the decision close; future official/reference factors define the event.

    Missing data before a crossing prevents confirmation. An established first hit survives later
    missing data. No-hit and the endpoint need every expected session. Raw bars outside the given
    calendar poison the affected prefix. Rows after observed_through or in the holdout are rejected.
    """
    observed_through = _day(observed_through)
    if observed_through >= settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('دوره مهروموم برای این پژوهش باز نمی‌شود')
    calendar = list(calendar)
    if (not calendar or any(_day(d) != d for d in calendar)
            or calendar != sorted(set(calendar))):
        raise ValueError('تقویم مرتب و یکتا لازم است')
    if not isinstance(horizons, (tuple, list)) or not horizons or any(type(h) is not int or h < 1 for h in horizons):
        raise ValueError('افق‌ها باید صحیح مثبت باشند')
    if len(set(horizons)) != len(horizons):
        raise ValueError('افق تکراری مجاز نیست')
    threshold = _number(threshold)
    if not np.isfinite(threshold) or threshold <= 1:
        raise ValueError('ضریب هدف باید متناهی و بزرگ‌تر از یک باشد')
    exact_threshold = Fraction(str(threshold))
    p = _panel(prices, {'symbol', 'date', 'close_final_raw', 'ref_price_raw', 'volume'})
    anchors = _panel(decisions, {'symbol', 'date'})
    if p.date.gt(observed_through).any() or anchors.date.gt(observed_through).any():
        raise ValueError('ورودی پس از زمان مشاهده وارد مطالعه نمی‌شود')
    positions = {day: i for i, day in enumerate(calendar)}
    if not set(anchors.date) <= set(positions):
        raise ValueError('روز تصمیم باید عضو تقویم صریح باشد')
    for col in ('close_final_raw', 'ref_price_raw', 'volume'):
        p[col] = p[col].map(_number)
    p['factor'] = 1 + market_rules.pit_daily_returns(p)
    groups = {name: group.set_index('date').to_dict('index') for name, group in p.groupby('symbol', sort=False)}
    conflicts = {name: sorted(set(rows)-set(positions)) for name, rows in groups.items()}
    output = []
    for anchor in anchors.itertuples(index=False):
        # _panel sorts columns: named fields, never positional assumptions.
        symbol, day = anchor.symbol, anchor.date
        rows, start = groups.get(symbol, {}), positions[day]
        base = rows.get(day)
        issue, issue_date = None, None
        if base is None:
            issue, issue_date = 'missing_anchor', day
        elif not (np.isfinite(base['close_final_raw']) and base['close_final_raw'] > 0
                  and np.isfinite(base['volume']) and base['volume'] > 0 and base['volume'].is_integer()):
            issue, issue_date = 'invalid_anchor', day
        bad_dates = conflicts.get(symbol, [])
        first_conflict = next((d for d in bad_dates if d > day), None)
        multiple, previous_close, hit_session, hit_date = 1., base['close_final_raw'] if base else np.nan, None, None
        exact_multiple = Fraction(1)
        path, action_count, actions = [], 0, []
        problems = []
        for step in range(1, max(horizons)+1):
            expected = calendar[start+step] if start+step < len(calendar) else None
            if issue is None:
                if first_conflict is not None and first_conflict <= min(expected or observed_through, observed_through):
                    issue, issue_date = 'calendar_conflict', first_conflict
                elif expected is None or expected > observed_through:
                    issue, issue_date = 'right_censored', None
                elif expected not in rows:
                    issue, issue_date = 'missing_observation', expected
                else:
                    row = rows[expected]
                    if not (np.isfinite(row['volume']) and row['volume'] > 0 and row['volume'].is_integer()
                            and np.isfinite(row['factor']) and row['factor'] > 0):
                        issue, issue_date = 'invalid_observation', expected
                    else:
                        with np.errstate(over='ignore', invalid='ignore'):
                            candidate = multiple * row['factor']
                        if not np.isfinite(candidate) or candidate <= 0:
                            issue, issue_date = 'nonfinite_path', expected
                        else:
                            multiple = candidate
                            # Compare the stated decimal prices exactly: 114/100 * 200/114 is 2,
                            # although its floating product may be 1.9999999999999998.
                            exact_multiple *= Fraction(str(row['close_final_raw'])) / Fraction(str(row['ref_price_raw']))
                            action_count += int(abs(row['ref_price_raw']/previous_close-1) > 1e-6)
                            previous_close = row['close_final_raw']
                            if hit_session is None and exact_multiple >= exact_threshold:
                                hit_session, hit_date = step, expected
            path.append(multiple if issue is None else np.nan)
            actions.append(action_count)
            problems.append((issue, issue_date))
        for horizon in horizons:
            problem, problem_date = problems[horizon-1]
            hit = hit_session is not None and hit_session <= horizon
            end = calendar[start+horizon] if start+horizon < len(calendar) else None
            status = 'hit' if hit else 'complete_no_hit' if problem is None else problem
            known = hit or problem is None
            valid_prefix = sum(np.isfinite(path[:horizon]))
            output.append(dict(symbol=symbol, date=day, horizon_sessions=horizon, threshold=threshold,
                status=status, outcome=bool(hit) if known else None,
                first_hit_date=hit_date if hit else None, first_hit_session=hit_session if hit else None,
                resolved_at=hit_date if hit else end if known else None, nominal_end_date=end,
                endpoint_multiple=float(path[horizon-1]) if problem is None else None,
                observed_through=observed_through, valid_prefix_sessions=valid_prefix,
                prefix_reference_changes=actions[horizon-1], path_problem=problem, path_problem_date=problem_date,
                economic_double=None, executable_return=None))
    columns = ['symbol', 'date', 'horizon_sessions', 'threshold', 'status', 'outcome', 'first_hit_date',
        'first_hit_session', 'resolved_at', 'nominal_end_date', 'endpoint_multiple', 'observed_through',
        'valid_prefix_sessions', 'prefix_reference_changes', 'path_problem', 'path_problem_date',
        'economic_double', 'executable_return']
    result = pd.DataFrame(output, columns=columns)
    text_columns = ['symbol', 'date', 'status', 'first_hit_date', 'resolved_at', 'nominal_end_date',
                    'observed_through', 'path_problem', 'path_problem_date']
    for column in text_columns:
        result[column] = result[column].astype('string')
    for column in ('first_hit_session', 'endpoint_multiple'):
        result[column] = result[column].astype(float)
    result['outcome'] = pd.array(result.outcome, dtype='boolean')
    return result


def summarize_rare_events(outcomes):
    """Bounds retain all unknown anchors. Descriptive frequencies, not confidence intervals."""
    if outcomes.duplicated(['symbol', 'date', 'horizon_sessions']).any():
        raise ValueError('رخداد تکراری در مخرج مجاز نیست')
    summaries = []
    for horizon, group in outcomes.groupby('horizon_sessions', sort=True):
        if group.threshold.nunique(dropna=False) != 1:
            raise ValueError('هدف‌های متفاوت در یک مخرج ترکیب نمی‌شوند')
        hits = int(group.status.eq('hit').sum())
        no_hits = int(group.status.eq('complete_no_hit').sum())
        n, unknown = len(group), len(group)-hits-no_hits
        overlap = 0
        for _, same_symbol in group.groupby('symbol'):
            # Unknown tails remain potentially overlapping through the snapshot boundary.
            previous_end = None
            for row in same_symbol.sort_values('date').itertuples():
                if previous_end is not None and row.date <= previous_end:
                    overlap += 1
                candidate = row.nominal_end_date if pd.notna(row.nominal_end_date) else row.observed_through
                previous_end = candidate if previous_end is None else max(candidate, previous_end)
        summaries.append(dict(horizon_sessions=int(horizon), threshold=float(group.threshold.iloc[0]),
            opportunities=n, hits=hits, complete_no_hit=no_hits, unknown=unknown,
            observed_hit_fraction_lower=hits/n, possible_hit_fraction_upper=(hits+unknown)/n,
            resolved_subset_hit_fraction=hits/(hits+no_hits) if hits+no_hits else None,
            resolved_fraction=(hits+no_hits)/n, unique_symbols=int(group.symbol.nunique()),
            unique_decision_dates=int(group.date.nunique()), overlapping_anchor_rows=overlap,
            overlap_definition='closed anchor-to-nominal-end intervals, including shared endpoints',
            status_counts={str(k):int(v) for k,v in group.status.value_counts().items()},
            endpoint_unknown=int(group.endpoint_multiple.isna().sum()),
            synthetic_reference_change_paths=int(group.prefix_reference_changes.gt(0).sum()),
            independent_samples=False, historical_membership_verified=False,
            probability_calibrated=False, economic_profit_verified=False, accepted_for_signals=False))
    return summaries
