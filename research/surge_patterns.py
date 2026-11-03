"""Fixed pre-move hypotheses, censored outcomes and dated controls; never a signal.

Only development dates are accepted. Price features are rebuilt from raw PIT bars,
on the supplied market calendar. The recent winner list is never an input.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import settings
from core import market_rules
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date
from research.uncertainty import _indices

VERSION = 'surge-pattern-diagnostics-v1'
HORIZONS = (20, 60)
RAW = ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
       'ref_price_raw', 'volume', 'value')


@dataclass(frozen=True)
class Hypothesis:
    name: str
    label_fa: str
    definition: str


# Freeze the family before reading outcomes. These are hypotheses, not fitted rules.
HYPOTHESES = (
    Hypothesis('momentum', 'رشد قبلی قوی', 'past20 PIT return percentile >= .9'),
    Hypothesis('reversal', 'افت قبلی شدید', 'past20 PIT return percentile <= .1'),
    Hypothesis('breakout', 'عبور از سقف گذشته', 'close PIT index > every previous60 close index'),
    Hypothesis('value_surge', 'جهش ارزش معاملات با روز مثبت', 'today value / previous20 mean >= 2 and daily return > 0'),
    Hypothesis('quiet_value', 'رشد آرام همراه افزایش ارزش معاملات', '0 <= past20 return <= .1 and last5 / preceding20 mean value >= 1.5'),
    Hypothesis('compression', 'کاهش نوسان پیشین', 'past5 return std / past20 std <= .5 and past20 return >= 0'),
    Hypothesis('breakout_value', 'شکست همراه ارزش معاملات', 'breakout AND value_surge'),
    Hypothesis('relative_strength', 'قدرت نسبی مثبت', 'past20 return > same-date cross-sectional median AND > 0'),
    Hypothesis('broad_momentum', 'رشد قوی در موج فراگیر', 'momentum AND >= .6 of observed valid past20 returns > 0'),
    Hypothesis('locked_up', 'معامله تک‌قیمتی مثبت', 'market_rules.lock_flags locked_up today'),
)


def _keys(frame):
    if not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any() or not {'date', 'symbol'} <= set(frame):
        raise ValueError('Unique date and symbol columns are required')
    # Reject the sealed boundary before inspecting any numerical values.
    if any(not is_canonical_iso_date(d) or d >= settings.RESEARCH_CONFIG['holdout_start'] for d in frame.date.unique()):
        raise ValueError('Only canonical development dates are allowed; holdout stays sealed')
    names = frame.symbol.unique()
    if any(not isinstance(s, str) or not normalize_symbol(s) for s in names):
        raise ValueError('Explicit symbol identities are required')
    out = frame.copy()
    out['symbol'] = out.symbol.map({s: normalize_symbol(s) for s in names})
    if out.duplicated(['symbol', 'date']).any():
        raise ValueError('Duplicate normalized symbol/date')
    return out


def _numeric(series):
    if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(series.dtype):
        return series.replace([np.inf, -np.inf], np.nan)
    clean = series.mask(series.map(lambda v: isinstance(v, (bool, np.bool_))))
    return pd.to_numeric(clean, errors='coerce').replace([np.inf, -np.inf], np.nan)


def build_pre_move_features(prices, decisions, calendar, *, min_cross_section=20):
    """Past-only strict session windows. A missing/invalid bar is never forward-filled.

    Current industry, current market cap, adjusted chart prices, outcomes and future
    winner identities are deliberately absent. Full historical membership remains
    a separate data requirement, not something this computation establishes.
    """
    p, anchors = _keys(prices), _keys(decisions)
    days = list(calendar)
    if (not days or days != sorted(set(days)) or any(not is_canonical_iso_date(d)
            or d >= settings.RESEARCH_CONFIG['holdout_start'] for d in days)):
        raise ValueError('A sorted unique development calendar is required')
    if type(min_cross_section) is not int or min_cross_section < 1 or not set(anchors.date) <= set(days):
        raise ValueError('Invalid cross section or decision outside calendar')
    if not set(RAW) <= set(p):
        raise ValueError('All raw price, volume and value columns are required')
    p = p[['date', 'symbol', *RAW]].sort_values(['symbol', 'date'])
    for col in RAW:
        p[col] = _numeric(p[col])
    frames = []
    anchor_days = set(anchors.date)
    for name, own in p.groupby('symbol', sort=False):
        frame = own.set_index('date').reindex(days)
        frame['symbol'] = name
        valid = (frame[list(RAW)].notna().all(axis=1) & frame.volume.gt(0)
                 & frame.volume.mod(1).eq(0) & frame.value.gt(0)
                 & frame.low_raw.gt(0) & frame.high_raw.ge(frame.low_raw)
                 & frame.open_raw.between(frame.low_raw, frame.high_raw)
                 & frame.close_raw.between(frame.low_raw, frame.high_raw)
                 & frame.close_final_raw.gt(0) & frame.ref_price_raw.gt(0))
        # Value/volume must also describe a possible traded price. A large corrupt
        # value must never turn into apparent unusual activity or a breakout filter.
        valid &= market_rules.daily_vwap(frame).notna()
        # An off-calendar observation cannot be discarded to improve the next window.
        for day in own.loc[~own.date.isin(days), 'date']:
            pos = np.searchsorted(days, day)
            if pos < len(days):
                valid.iloc[pos] = False
        ret = market_rules.pit_daily_returns(frame).where(valid)
        if own.date.iloc[0] in frame.index:
            ret.loc[own.date.iloc[0]] = np.nan  # the first archive reference may be an IPO nominal price
        logged = np.log1p(ret)
        index = np.exp(logged.fillna(0.).cumsum())
        value = frame.value.where(valid)
        features = pd.DataFrame(index=frame.index)
        features['symbol'] = name
        features['ret_1'] = ret
        features['ret_20'] = np.expm1(logged.rolling(20, min_periods=20).sum())
        complete61 = valid.rolling(61, min_periods=61).sum().eq(61)
        features['breakout_60'] = (index / index.shift(1).rolling(60, min_periods=60).max() - 1).where(complete61)
        denominator = value.shift(1).rolling(20, min_periods=20).mean()
        features['value_ratio'] = (value / denominator).where(denominator.gt(0))
        earlier = value.shift(5).rolling(20, min_periods=20).mean()
        features['value_5_vs_prior20'] = (value.rolling(5, min_periods=5).mean() / earlier).where(earlier.gt(0))
        vol20 = ret.rolling(20, min_periods=20).std()
        features['vol_5_vs_20'] = (ret.rolling(5, min_periods=5).std() / vol20).where(vol20.gt(0))
        features['locked_up_today'] = market_rules.lock_flags(frame).locked_up.astype(float).where(valid)
        features['history_60_complete'] = valid.rolling(60, min_periods=60).sum().eq(60)
        features.index.name = 'date'
        frames.append(features.reset_index().loc[lambda f: f.date.isin(anchor_days)])
    if not frames:
        raise ValueError('No raw histories for pattern diagnostics')
    features = pd.concat(frames, ignore_index=True)
    # Only observed decision rows belong to the frozen census; reindexing adds no opportunities.
    features = anchors[['date', 'symbol']].merge(features, on=['date', 'symbol'], how='left', validate='one_to_one')
    groups = features.groupby('date').ret_20
    enough = groups.transform('count').ge(min_cross_section)
    features['ret_20_percentile'] = groups.rank(pct=True, method='average').where(enough)
    features['market_median_20'] = groups.transform('median').where(enough)
    positive = features.ret_20.gt(0).astype(float).where(features.ret_20.notna())
    features['market_positive_20'] = positive.groupby(features.date).transform('mean').where(enough)
    return features.sort_values(['symbol', 'date']).reset_index(drop=True)


def _condition(series, predicate):
    return predicate(series).astype('boolean').mask(series.isna())


def pattern_masks(features):
    """Three-state flags: observed false, observed true, or unknown."""
    f = features
    masks = pd.DataFrame(index=f.index)
    masks['momentum'] = _condition(f.ret_20_percentile, lambda x: x.ge(.9))
    masks['reversal'] = _condition(f.ret_20_percentile, lambda x: x.le(.1))
    masks['breakout'] = _condition(f.breakout_60, lambda x: x.gt(0))
    masks['value_surge'] = _condition(f.value_ratio, lambda x: x.ge(2)) & _condition(f.ret_1, lambda x: x.gt(0))
    masks['quiet_value'] = (_condition(f.ret_20, lambda x: x.between(0, .1))
                            & _condition(f.value_5_vs_prior20, lambda x: x.ge(1.5)))
    masks['compression'] = (_condition(f.vol_5_vs_20, lambda x: x.le(.5))
                            & _condition(f.ret_20, lambda x: x.ge(0)))
    masks['breakout_value'] = masks.breakout & masks.value_surge
    masks['relative_strength'] = (_condition(f.ret_20 - f.market_median_20, lambda x: x.gt(0))
                                  & _condition(f.ret_20, lambda x: x.gt(0)))
    masks['broad_momentum'] = masks.momentum & _condition(f.market_positive_20, lambda x: x.ge(.6))
    masks['locked_up'] = _condition(f.locked_up_today, lambda x: x.eq(1))
    return masks[[h.name for h in HYPOTHESES]]


def censor_at(outcomes, end_exclusive):
    """A test fold cannot learn outcomes resolved after its boundary."""
    out = _keys(outcomes)
    if not {'status', 'outcome', 'resolved_at'} <= set(out):
        raise ValueError('An explicit rare-event outcome contract is required')
    out['outcome'] = out.outcome.astype(object)
    if not is_canonical_iso_date(end_exclusive) or end_exclusive > settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('Invalid development observation boundary')
    late = out.resolved_at.notna() & out.resolved_at.ge(end_exclusive)
    out.loc[late, 'status'] = 'fold_right_censored'
    out.loc[late, 'outcome'] = pd.NA
    out.loc[late, 'resolved_at'] = pd.NA
    return out


def event_counts(rows):
    n = len(rows)
    hits = int(rows.status.eq('hit').sum())
    misses = int(rows.status.eq('complete_no_hit').sum())
    unknown = n - hits - misses
    return dict(opportunities=n, hits=hits, complete_no_hit=misses, unknown=unknown,
                hit_fraction_lower=hits/n if n else None,
                hit_fraction_upper=(hits+unknown)/n if n else None,
                resolved_subset_hit_fraction=hits/(hits+misses) if hits+misses else None,
                resolved_fraction=(hits+misses)/n if n else None,
                unique_symbols=int(rows.symbol.nunique()), decision_dates=int(rows.date.nunique()))


def execution_counts(rows, h, *, end_exclusive):
    """Equal notional opportunity diagnostics, not a shared-cash portfolio.

    Known blocked entries retain idle cash. Invalid/censored returns stay unknown.
    Synthetic corporate-action accounting and daily OHLC do not establish fills.
    """
    status = rows[f'label_status_{h}d']
    exit_day = rows[f'exit_date_{h}d']
    complete = status.eq('complete') & exit_day.map(is_canonical_iso_date).astype(bool) & exit_day.lt(end_exclusive)
    blocked = status.eq('entry_blocked') & rows.entry_date.map(is_canonical_iso_date).astype(bool) & rows.entry_date.lt(end_exclusive)
    known = complete | blocked
    result = dict(opportunities=len(rows), complete=int(complete.sum()), entry_blocked=int(blocked.sum()),
                  unknown=int((~known).sum()), shared_cash_simulated=False, capacity_verified=False,
                  economic_accounting_verified=False)
    for key, col in [('normal', f'net_ret_{h}d'), ('stress', f'net_ret_{h}d_worst')]:
        values = pd.to_numeric(rows.loc[complete, col], errors='coerce')
        if not np.isfinite(values).all():
            raise ValueError('A complete execution label has an unknown return')
        result[key+'_mean_known_opportunities'] = float(values.sum()/known.sum()) if known.any() else None
        result[key+'_median_completed'] = float(values.median()) if len(values) else None
        result[key+'_positive_completed'] = int(values.gt(0).sum())
    actions = rows.loc[complete, f'corporate_actions_{h}d']
    result['completed_with_synthetic_actions'] = int(actions.gt(0).sum())
    delays = rows.loc[complete, f'exit_delay_{h}d']
    result['completed_with_exit_delay'] = int(delays.gt(0).sum())
    result['max_exit_delay_sessions'] = int(delays.max()) if len(delays) else None
    return result


def block_diagnostic(rows, mask, *, family_comparisons, replications=10000, block_lengths=(4, 8)):
    """Resample whole dated cross sections in contiguous stationary blocks.

    The target is resolved-subset event-rate difference, NOT all-opportunity success
    probability. Selection history, nonstationarity and missingness remain blockers.
    No observation dates are removed, including dates with zero selected cases.
    """
    if type(family_comparisons) is not int or family_comparisons < 1:
        raise ValueError('Explicit positive comparison family required')
    if type(replications) is not int or not 1000 <= replications <= 100000:
        raise ValueError('Invalid resampling budget')
    if not block_lengths or any(type(b) is not int or b < 2 for b in block_lengths):
        raise ValueError('Positive time blocks of at least two anchor dates required')
    selected = mask.fillna(False).astype(bool)
    control = (~mask).fillna(False).astype(bool)
    known = rows.status.isin(['hit', 'complete_no_hit'])
    hit = rows.status.eq('hit')
    daily = pd.DataFrame(dict(date=rows.date, sh=(selected & hit).astype(int),
        sn=(selected & known).astype(int), ch=(control & hit).astype(int), cn=(control & known).astype(int)))
    counts = daily.groupby('date', sort=True)[['sh', 'sn', 'ch', 'cn']].sum().to_numpy(dtype=float, copy=True)
    total = counts.sum(axis=0)
    if len(counts) < 24 or total[1] == 0 or total[3] == 0 or max(block_lengths) >= len(counts):
        return dict(status='insufficient_dated_controls', intervals=[], calibrated_probability=False)
    missing_control = (counts[:, 1] > 0) & (counts[:, 3] == 0)
    if missing_control.any():
        return dict(status='missing_same_date_controls', missing_control_dates=int(missing_control.sum()),
                    intervals=[], calibrated_probability=False)
    # Standardize controls to the selected group's dated exposure. A rule that is
    # active mostly in bull markets must not receive credit for that alone.
    control_rate = np.divide(counts[:, 2], counts[:, 3], out=np.zeros(len(counts)), where=counts[:, 3] > 0)
    counts[:, 2] = control_rate * counts[:, 1]
    counts[:, 3] = counts[:, 1]
    total = counts.sum(axis=0)
    difference = total[0]/total[1] - total[2]/total[3]
    intervals = []
    for length in block_lengths:
        rng = np.random.default_rng(712 + length)
        estimates = []
        for _ in range(replications):
            sample = counts[_indices(rng, len(counts), length)].sum(axis=0)
            estimates.append(sample[0]/sample[1]-sample[2]/sample[3] if sample[1] and sample[3] else np.nan)
        values = np.asarray(estimates)
        # A degenerate resample is retained as a blocker, not dropped to sharpen a CI.
        if not np.isfinite(values).all():
            intervals.append(dict(anchor_block_length=length, status='undefined_resample',
                                  undefined=int((~np.isfinite(values)).sum())))
            continue
        alpha = .05/family_comparisons
        intervals.append(dict(anchor_block_length=length, status='computed_conditional_diagnostic',
            ci95=np.quantile(values, [.025, .975]).tolist(),
            family_ci=np.quantile(values, [alpha/2, 1-alpha/2]).tolist()))
    return dict(status='conditional_resolved_subset_only', difference=float(difference), intervals=intervals,
                dated_cross_sections=len(counts), replications=replications, family_comparisons=family_comparisons,
                controls='same-date complement standardized to selected resolved-case counts',
                calibrated_probability=False, full_search_history_adjusted=False,
                all_opportunity_advantage_established=False)


def summarize_patterns(features, outcomes, *, executions=None, end_exclusive=None, bootstrap=False, replications=10000):
    """Every fixed hypothesis and both horizons are retained, including failures."""
    f = _keys(features)
    boundary = end_exclusive or settings.RESEARCH_CONFIG['holdout_start']
    # Outcome keys intentionally repeat across horizons; validate each horizon independently.
    parts = []
    for horizon in HORIZONS:
        rows = censor_at(outcomes.loc[outcomes.horizon_sessions.eq(horizon)], boundary)
        required = {'status', 'outcome', 'resolved_at'}
        if not required <= set(rows) or not rows.status.isin(['hit', 'complete_no_hit', 'fold_right_censored',
                'missing_observation', 'right_censored', 'invalid_observation', 'calendar_conflict',
                'invalid_anchor', 'missing_anchor', 'nonfinite_path', 'unusable_no_trade_evidence',
                'no_trade_reference_conflict']).all():
            raise ValueError('Unrecognized rare-event contract')
        if rows.status.eq('hit').ne(rows.outcome.eq(True).fillna(False)).any():
            raise ValueError('Hit status and event outcome disagree')
        known = rows.status.isin(['hit', 'complete_no_hit'])
        valid_type = rows.outcome.map(lambda v: isinstance(v, (bool, np.bool_)))
        if ((known & ~valid_type).any() or (~known & rows.outcome.notna()).any()
                or (known & ~rows.resolved_at.map(is_canonical_iso_date)).any()
                or (known & rows.resolved_at.le(rows.date)).any()):
            raise ValueError('Resolved event labels need explicit boolean outcomes and later resolution dates')
        if len(rows) != len(f) or set(map(tuple, rows[['date', 'symbol']].to_numpy())) != set(map(tuple, f[['date', 'symbol']].to_numpy())):
            raise ValueError('Features must retain the full frozen opportunity cohort')
        merged = f.merge(rows, on=['date', 'symbol'], how='left', validate='one_to_one')
        masks = pattern_masks(merged)
        if executions is not None:
            merged = merged.merge(_keys(executions), on=['date', 'symbol'], how='left', validate='one_to_one')
        base = event_counts(merged)
        for hypothesis in HYPOTHESES:
            mask = masks[hypothesis.name]
            selected = merged.loc[mask.fillna(False)]
            controls = merged.loc[(~mask).fillna(False)]
            s, c = event_counts(selected), event_counts(controls)
            bounds = None
            if s['opportunities'] and c['opportunities']:
                bounds = [s['hit_fraction_lower']-c['hit_fraction_upper'],
                          s['hit_fraction_upper']-c['hit_fraction_lower']]
            result = dict(hypothesis=hypothesis.name, label_fa=hypothesis.label_fa, horizon_sessions=horizon,
                selected=s, control=c, baseline=base, feature_unknown=int(mask.isna().sum()),
                all_opportunity_difference_bounds=bounds,
                retrospective_development=True, independent_future_test=False,
                accepted_for_signals=False, profitability_proven=False)
            if executions is not None:
                result['execution'] = execution_counts(selected, horizon-1, end_exclusive=boundary)
                result['control_execution'] = execution_counts(controls, horizon-1, end_exclusive=boundary)
            if bootstrap:
                result['block_diagnostic'] = block_diagnostic(merged, mask, family_comparisons=len(HYPOTHESES)*len(HORIZONS)*2,
                                                             replications=replications)
            parts.append(result)
    return parts
