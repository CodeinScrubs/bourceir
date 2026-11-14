"""Outcome-free liquidity-capacity check for the saved Momentum 20D study."""

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.price_quality import RAW_PRICES, numeric_prices, price_quality
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.validation_run import write_state
from research.baselines import STRATEGIES, eligible_strategy_score, pick_top
from research.run import _sha256
from research.strategy_execution import load_publications

VERSION = 'capacity-momentum-preflight-v2-bounded-missingness'
LOOKBACK, MIN_OBSERVATIONS = 20, 15
CAPITAL_RIAL, SLOTS = 6_700_000_000, 5
THRESHOLD_RIAL = CAPITAL_RIAL // SLOTS * 100
HORIZONS = (2, 1)
MOMENTUM = next(rule for rule in STRATEGIES if rule.name == 'momentum_20d')


def liquidity_observations(prices, zero_receipts):
    """Accept coherent trade rows and post-first-trade native zero evidence."""
    key = ['date', 'symbol']
    if prices.duplicated(key).any() or zero_receipts.duplicated(key).any():
        raise ValueError('Duplicate instrument/session observations')
    numbers, flags = price_quality(prices)
    count = numeric_prices(prices, ('trade_count',)).trade_count
    valid = ~flags.any(axis=1) & count.gt(0) & count.mod(1).eq(0)
    positive = prices.loc[valid, key].assign(value=numbers.loc[valid, 'value'])
    active_keys = prices.loc[numbers.volume.gt(0) | numbers.value.gt(0) | count.gt(0), key]
    zeros = zero_receipts.merge(active_keys.assign(conflict=True), on=key, how='left', validate='one_to_one')
    conflict = zeros.conflict.eq(True)
    # Conflicting zero and activity claims invalidate the value instead of picking a winner.
    conflicts = pd.MultiIndex.from_frame(zeros.loc[conflict, key])
    positive = positive.loc[~pd.MultiIndex.from_frame(positive[key]).isin(conflicts)]
    first_trade = zeros.symbol.map(positive.groupby('symbol').date.min())
    early = first_trade.isna() | zeros.date.lt(first_trade)
    admitted = zeros.loc[~early & ~conflict, key].assign(value=0.)
    return pd.concat([positive, admitted], ignore_index=True), dict(
        admitted_zero_keys=len(admitted), before_first_observed_trade_or_unknown_zeros=int(early.sum()),
        zero_conflicts=int(conflict.sum()))


def capacity_bounds(grid):
    """Sharp median bounds for nonnegative unknown turnover, without imputing data.

    Missing values have domain [0, +infinity). A complete 20-session window
    with at least 15 known observations has finite lower AND upper medians:
    the two middle order statistics never reach the at most five infinities.
    """
    values = grid.to_numpy(dtype=float)
    known = ~np.isnan(values)
    if (not np.isfinite(values[known]).all() or (values[known] < 0).any()
            or len(grid) < LOOKBACK):
        raise ValueError('Nonnegative finite observations and a complete session window are required')
    count = grid.rolling(LOOKBACK, min_periods=1).count().fillna(0).astype(int)
    observed = grid.rolling(LOOKBACK, min_periods=MIN_OBSERVATIONS).median()
    lower = grid.fillna(0).rolling(LOOKBACK, min_periods=LOOKBACK).median()
    windows = np.lib.stride_tricks.sliding_window_view(
        np.where(known, values, np.inf), LOOKBACK, axis=0)
    middle = np.partition(windows, (9, 10), axis=-1)[..., (9, 10)]
    upper = pd.DataFrame(np.nan, index=grid.index, columns=grid.columns)
    upper.iloc[LOOKBACK-1:] = middle.mean(axis=-1)
    eligible = count.ge(MIN_OBSERVATIONS) & lower.notna()
    lower, upper = lower.where(eligible), upper.where(eligible)
    status = pd.DataFrame('UNKNOWN', index=grid.index, columns=grid.columns)
    status = status.mask(eligible & upper.lt(THRESHOLD_RIAL), 'FAIL')
    status = status.mask(eligible & lower.ge(THRESHOLD_RIAL), 'PASS')
    return count, observed, lower, upper, status


def decision_preflight(panel, observations, calendar, registry, folds, min_daily_value, *, with_candidates=False):
    """Compute selection-only diagnostics on the registered session grid."""
    if (not calendar or calendar != sorted(set(calendar))
            or any(not is_canonical_iso_date(day) for day in calendar)):
        raise ValueError('A sorted, unique session calendar is required')
    if panel.duplicated(['date', 'symbol']).any() or len(registry) != len(set(registry)):
        raise ValueError('Unique decision and registry keys are required')
    day_index = {day: i for i, day in enumerate(calendar)}
    fold_days = [[day for day in calendar if fold['test_start'] <= day < fold['test_end_exclusive']]
                 for fold in folds]
    decision_days = [day for days in fold_days for day in days[:-2]]
    if (any(len(days) < 4 for days in fold_days) or decision_days != sorted(set(decision_days))
            or not decision_days or day_index[decision_days[0]] < LOOKBACK - 1):
        raise ValueError('Nonoverlapping folds and a complete lookback grid are required')
    grid = observations.pivot(index='date', columns='symbol', values='value').reindex(
        index=calendar[day_index[decision_days[0]] - LOOKBACK + 1:day_index[decision_days[-1]] + 1], columns=registry)
    valid, median, lower, upper, status = capacity_bounds(grid)
    zero = grid.eq(0).rolling(LOOKBACK, min_periods=1).sum().astype(int)
    sample = panel.loc[panel.date.isin(decision_days) & panel.value.ge(min_daily_value)].copy()
    sample['score'] = eligible_strategy_score(sample, MOMENTUM)
    threshold_pass = status.eq('PASS')
    row_ids = median.index.get_indexer(sample.date)
    col_ids = median.columns.get_indexer(sample.symbol)
    if (row_ids < 0).any() or (col_ids < 0).any():
        raise ValueError('Decision rows must be inside the declared registry and calendar')
    sample['capacity_pass'] = threshold_pass.to_numpy()[row_ids, col_ids]
    for name, frame in [('known_observations', valid), ('observed_median_rial', median),
                        ('lower_median_rial', lower), ('upper_median_rial', upper), ('capacity_status', status)]:
        sample[name] = frame.to_numpy()[row_ids, col_ids]
    sample['observed_median_pass'] = sample.observed_median_rial.ge(THRESHOLD_RIAL)
    control = pick_top(sample, sample.score, SLOTS)
    treatment = pick_top(sample, sample.score.where(sample.capacity_pass), SLOTS)
    prior = pick_top(sample, sample.score.where(sample.observed_median_pass), SLOTS)
    control_by_day = control.groupby('date').symbol.agg(list).to_dict()
    treatment_by_day = treatment.groupby('date').symbol.agg(list).to_dict()
    prior_by_day = prior.groupby('date').symbol.agg(list).to_dict()
    candidate_counts = sample.groupby('date').agg(
        base_daily_value_eligible=('symbol', 'size'),
        base_known_momentum=('score', 'count'))
    filtered_counts = sample.loc[sample.capacity_pass & sample.score.notna()].groupby('date').size()
    rows = []
    for index, days in enumerate(fold_days):
        for offset, day in enumerate(days[:-2]):
            counts = valid.loc[day]
            before = control_by_day.get(day, [])
            after = treatment_by_day.get(day, [])
            rows.append(dict(fold=index, decision_date=day, next_session=calendar[day_index[day]+1],
                planned_primary_exit=days[offset+3] if offset+3 < len(days) else None,
                lookback_start=calendar[day_index[day]-19], registry_count=len(registry),
                full_20=int(counts.eq(20).sum()), valid_15_to_19=int(counts.between(15, 19).sum()),
                insufficient=int(counts.lt(15).sum()), valid_zero_cells=int(zero.loc[day].sum()),
                missing_or_invalid_cells=int((LOOKBACK-counts).sum()),
                capacity_pass_all=int(threshold_pass.loc[day].sum()),
                capacity_fail_all=int(status.loc[day].eq('FAIL').sum()),
                capacity_unknown_all=int(status.loc[day].eq('UNKNOWN').sum()),
                base_daily_value_eligible=int(candidate_counts['base_daily_value_eligible'].get(day, 0)),
                base_known_momentum=int(candidate_counts['base_known_momentum'].get(day, 0)),
                capacity_pass_with_momentum=int(filtered_counts.get(day, 0)),
                control_top5=before, filtered_top5=after, tape_changed=before != after,
                observed_median_top5=prior_by_day.get(day, []),
                missingness_bounds_changed_tape=after != prior_by_day.get(day, [])))
    return (rows, sample) if with_candidates else rows


def _verified_inputs(folder, names):
    manifest = json.loads((folder/'manifest.json').read_bytes())
    result = {name: _sha256(folder/name) for name in names}
    if any(manifest.get(name) != digest for name, digest in result.items()):
        raise ValueError('Saved research input hash mismatch')
    return result


def run_preflight(suite, execution, output):
    private = (settings.DATA_DIR/'private').resolve()
    suite, execution, output = (Path(p).resolve() for p in (suite, execution, output))
    if any(p == private or not p.is_relative_to(private) for p in (suite, execution, output)):
        raise ValueError('Explicit paths under data/private are required')
    inputs = _verified_inputs(suite, ('protocol.json', 'calendar.json', 'registry.parquet',
                                      'prices.parquet', 'features.parquet'))
    source = _verified_inputs(execution, ('protocol.json', 'publications.json'))
    protocol = json.loads((suite/'protocol.json').read_bytes())
    followup = json.loads((execution/'protocol.json').read_bytes())
    calendar = json.loads((suite/'calendar.json').read_bytes())
    plan, folds = protocol['plan'], protocol['folds']
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (followup['parent_manifest_sha256'] != _sha256(suite/'manifest.json')
            or followup['publications_sha256'] != source['publications.json']
            or plan['end_exclusive'] > cutoff or protocol['holdout_loaded'] is not False
            or any(day >= plan['end_exclusive'] for day in calendar)
            or any(fold['test_end_exclusive'] > plan['end_exclusive'] for fold in folds)
            or len(folds) != 5 or plan['initial_capital_rial'] != CAPITAL_RIAL
            or plan['max_positions'] != SLOTS or plan['stress_participation'] != .01):
        raise ValueError('The registered five-fold development contract or source linkage differs')
    output.mkdir(parents=True, exist_ok=False)
    code_paths = ['research/capacity_preflight.py', 'research/strategy_suite.py', 'research/baselines.py',
                  'research/strategy_execution.py', 'features/daily.py', 'core/market_rules.py',
                  'core/price_quality.py', 'backtest/portfolio.py']
    code = {name: _sha256(settings.BASE_DIR/name) for name in code_paths}
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=settings.BASE_DIR, text=True).strip()
    write_state(output/'scope.json', dict(version=VERSION, created_at=now_tehran_iso(), git_head=commit,
        code_sha256=code, suite=str(suite.relative_to(private)), execution=str(execution.relative_to(private)),
        suite_inputs_sha256=inputs, execution_inputs_sha256=source, folds=folds,
        lookback=LOOKBACK, minimum_observations=MIN_OBSERVATIONS, primary_horizon=HORIZONS[0],
        secondary_horizon=HORIZONS[1], threshold_rial=THRESHOLD_RIAL, holdout_start=cutoff,
        official_calendar_verified=False, historical_membership_verified=False,
        experimental_registration=False, financial_replay_authorized_by_this_record=False))
    registry = pd.read_parquet(suite/'registry.parquet')
    if not registry.instrument_type_code.isin((300, 303)).all():
        raise ValueError('Ordinary shares are required')
    prices = pd.read_parquet(suite/'prices.parquet', columns=['symbol', 'date', *RAW_PRICES, 'trade_count'])
    scores = pd.read_parquet(suite/'features.parquet', columns=['symbol', 'date', 'ret_20d'])
    if (not set(prices.date.unique()) <= set(calendar)
            or not set(prices.symbol.unique()) <= set(registry.symbol_fa)
            or registry.symbol_fa.duplicated().any()):
        raise ValueError('Market keys are outside the registered development scope')
    _, _, receipts = load_publications((execution/'publications.json').read_bytes(), registry, calendar,
                                       plan['end_exclusive'])
    zeros = pd.DataFrame([(r['date'], r['symbol']) for r in receipts if r['status']=='observed_zero_activity'],
                         columns=['date', 'symbol'])
    observations, zero_qa = liquidity_observations(prices, zeros)
    panel = prices[['date', 'symbol', 'value']].merge(scores, on=['date', 'symbol'], validate='one_to_one')
    if len(panel) != len(prices) or len(panel) != len(scores):
        raise ValueError('Frozen raw and feature keys differ')
    rows, candidates = decision_preflight(panel, observations, calendar, registry.symbol_fa.tolist(), folds,
                                        plan['min_daily_value_rial'], with_candidates=True)
    summaries = []
    for index, fold in enumerate(folds):
        own = [r for r in rows if r['fold']==index]
        primary = [r for r in own if r['planned_primary_exit'] is not None]
        summaries.append(dict(fold=index, test_start=fold['test_start'], primary_decisions=len(primary),
            secondary_decisions=len(own), primary_changed_tapes=sum(r['tape_changed'] for r in primary),
            primary_days_fewer_than_five=sum(len(r['filtered_top5']) < SLOTS for r in primary),
            primary_days_no_filtered_candidates=sum(not r['filtered_top5'] for r in primary),
            primary_bounds_changed_tapes=sum(r['missingness_bounds_changed_tape'] for r in primary),
            first_primary_decision=primary[0]))
    write_state(output/'decisions.json', rows)
    candidates.to_parquet(output/'candidate-capacity.parquet', index=False)
    result = dict(status='conditional_preflight_complete', created_at=now_tehran_iso(),
        official_calendar_verified=False, historical_membership_verified=False,
        historical_first_publication_times_verified=False, receipt_records_classified=len(receipts),
        receipt_status_counts=dict(Counter(row['status'] for row in receipts)),
        zero_observation_checks=zero_qa, decision_rows=len(rows), folds=summaries,
        capacity_candidate_status_counts={str(k): int(v) for k, v in candidates.capacity_status.value_counts().items()},
        observed_pass_now_unknown=int((candidates.observed_median_pass & ~candidates.capacity_pass).sum()),
        capacity_missingness_policy='sharp nonnegative median bounds, 20 sessions, minimum 15 known; PASS only',
        financial_outcomes_loaded=False, financial_replays=0, holdout_loaded=False, accepted_for_signals=False)
    write_state(output/'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('suite', 'execution', 'output'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    result = run_preflight(args.suite, args.execution, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != 'folds'}))


if __name__ == '__main__':
    main()
