"""Preregistered fixed-budget DIP comparison, all controls and auditable shared cash.

The two sized-target variants and the unit-reference selector are retrospective
research comparisons. No result of this runner authorizes a trading signal.
"""

import argparse
import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.research_review import read_development_parquet, sha256
from research.baselines import STRATEGIES, decision_score_inputs, eligible_strategy_score, pick_top
from research.dip_expectancy import fit_dip_model, forecast_dip
from research.dip_expectancy_run import (
    DIP_BASELINE_RESOLUTION_VERSION,
    _dump,
    _empty_column_axis,
    _past_choice,
    _training,
    _unknown_as_nan,
)
from research.dip_outcomes import RULE, dip_attempt_outcomes
from research.expectancy import PREDICTORS
from research.expectancy_run import _random, _rank
from research.labels import market_calendar
from research.portfolio_run import BASELINE_SELECTION_VERSION, verified_baseline_inputs
from research.run import _compact_metrics, _json_safe, archive_research_code, research_code_fingerprint
from research.sized_dip_expectancy import CAPACITY_FORMULA, CAPACITY_NAME, VARIANTS, fit_sized_model, forecast_sized
from research.sized_dip_outcomes import budget_value, execution_contract, sized_dip_attempt_outcomes
from research.surge_portfolio_run import _registry
from research.walkforward import time_folds

VERSION = 'sized-dip-study-v1'
SEEDS = (41, 97, 173)
FALSE_FLAGS = ('accepted_for_signals', 'economic_profit_verified', 'holdout_opened',
               'statistical_superiority_claim', 'unseen_test')


def modes():
    return [name for variant in VARIANTS for name in
        [variant, variant+'-stress', *[f'{variant}-random{seed}{suffix}' for seed in SEEDS for suffix in ('', '-stress')]]
        ]+['unit-reference', 'unit-reference-stress', 'best-past', 'best-past-stress',
           'buyer-power', 'buyer-power-stress', 'liquidity', 'liquidity-stress', 'idle-cash']


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError('An independently registered sized-DIP plan is required')
    expected = dict(version='sized-dip-pre-code-v1', rule=asdict(RULE), predictors=list(PREDICTORS),
        variants=list(VARIANTS), modes=modes(), fees=dict(settings.FEES), entry_day_exit=False,
        holdout_opened=False, accepted_for_signals=False, parameter_search=False,
        additional_feature_or_threshold_search=False, ridge_penalty=.01,
        holdout_start=settings.RESEARCH_CONFIG['holdout_start'],
        development_start=settings.RESEARCH_CONFIG['dev_start'], max_positions=5, participation=.05,
        loss_floor_exit=True, entry_allocation_fraction=1.0, random_seeds=list(SEEDS),
        prespecified_feature_variants=2, new_financial_modes=25, horizon_variants=1,
        capital_is_owner_account=False, target_global_account_interactions_modeled=False,
        target_account_loss_floor_modeled=False, outcome_labels_are_portfolio_returns=False,
        probability_of_profit=None, statistical_superiority_claim_permitted=False,
        slippage_bps=10, max_loss_pct=.1)
    if any(plan.get(k) != v or isinstance(v, bool) and type(plan.get(k)) is not bool
            for k, v in expected.items()):
        raise ValueError('The registered sized-DIP plan differs from the fixed research contract')
    capacity = plan.get('capacity_feature')
    if not isinstance(capacity, dict) or capacity.get('name') != CAPACITY_NAME or capacity.get('formula') != CAPACITY_FORMULA:
        raise ValueError('The predeclared raw capacity feature differs')
    budget = budget_value(plan.get('budget_rial'))
    if type(plan.get('initial_capital_rial')) is not int or plan['initial_capital_rial'] != budget*5:
        raise ValueError('The reserved budget must be one fifth of the declared research capital')
    training = plan.get('training')
    if not isinstance(training, dict) or any(type(training.get(k)) is not int or training[k] < 3
            for k in ('initial_sessions', 'test_sessions', 'embargo_sessions')):
        raise ValueError('Explicit chronological training/test/embargo settings are required')
    if any(type(plan.get(k)) is not int or plan[k] < 3 for k in ('min_model_sessions', 'min_model_rows')):
        raise ValueError('Explicit model sample minima are required')
    return PortfolioConfig(initial_capital_rial=plan['initial_capital_rial'], horizon=RULE.hold,
        max_positions=5, participation=.05, slippage_bps=plan['slippage_bps'], max_loss_pct=plan['max_loss_pct'])


def _targets(outcomes, fold):
    return _training(outcomes.loc[outcomes.date.ge(fold.train_start)
        &outcomes.date.lt(fold.train_end_exclusive)], fold.train_end_exclusive)


def _conditional(forecast, end):
    gate = (forecast.status.eq('forecasted')&forecast.normal_expected_net.gt(0)
        &forecast.scenario_floor_expected_net.gt(0)&forecast.date.lt(end))
    return _rank(forecast, forecast.scenario_floor_expected_net, gate, 5)


def fold_candidates(train, test, sized, unit, calendar, fold, index, plan):
    targets = _targets(sized, fold)
    unit_targets = _targets(unit, fold)
    xcols = ['date', 'symbol', *PREDICTORS]
    models, forecasts, picks = {}, {}, {}
    days = calendar[(calendar >= fold.test_start)&(calendar < fold.test_end_exclusive)]
    if len(days) < 3:
        raise ValueError('The DIP test needs decision, entry and next-session disposal dates')
    end = days[-2]
    pool = test.loc[test.date.lt(end)]
    for variant in VARIANTS:
        columns = xcols+(['value'] if variant == 'sized11' else [])
        models[variant] = fit_sized_model(train[columns], targets,
            train_end_exclusive=fold.train_end_exclusive, budget_rial=plan['budget_rial'], variant=variant,
            min_sessions=plan['min_model_sessions'], min_rows=plan['min_model_rows'])
        forecasts[variant] = forecast_sized(test[columns], models[variant])
        picks[variant] = _conditional(forecasts[variant], end)
        for seed in SEEDS:
            picks[f'{variant}-random{seed}'] = _random(pool, picks[variant], 5, seed+index)
    models['unit-reference'] = fit_dip_model(train[xcols], unit_targets,
        train_end_exclusive=fold.train_end_exclusive, min_sessions=plan['min_model_sessions'],
        min_rows=plan['min_model_rows'])
    forecasts['unit-reference'] = forecast_dip(test[xcols], models['unit-reference'])
    picks['unit-reference'] = _conditional(forecasts['unit-reference'], end)
    best, diagnostics = _past_choice(train, targets, fold, calendar)
    picks['best-past'] = (pick_top(pool, eligible_strategy_score(decision_score_inputs(pool),
        next(s for s in STRATEGIES if s.name == best['strategy'])), 5)
        [['date', 'symbol']] if best else pool.iloc[:0][['date', 'symbol']])
    picks.update({'buyer-power': pick_top(pool, pool.buyer_power, 5)[['date', 'symbol']],
        'liquidity': pick_top(pool, pool.value, 5)[['date', 'symbol']],
        'idle-cash': pool.iloc[:0][['date', 'symbol']]})
    return models, forecasts, {name: picks[name.removesuffix('-stress')].reset_index(drop=True)
        for name in modes()}, days, best, diagnostics


def _summary(records):
    summary = {}
    for name in modes():
        cases = [r['modes'][name] for r in records]
        known = [r['metrics']['total_return'] for r in cases if r['metrics']['total_return'] is not None]
        summary[name] = dict(folds=len(cases), known_proxy_return_folds=len(known),
            positive_proxy_folds=sum(v > 0 for v in known),
            mean_known_proxy_fold_return=float(np.mean(known)) if known else None,
            mean_conditions_on_known_folds_only=True,
            unknown_selected_outcomes=sum(r['unknown_selected_outcomes'] for r in cases),
            selected_outcomes_resolved_after_fold_end=sum(r['selected_outcomes_resolved_after_fold_end'] for r in cases),
            corporate_action_events=sum(r['metrics']['corporate_action_events'] for r in cases),
            stale_mark_days=sum(r['metrics']['stale_mark_days'] for r in cases),
            open_position_folds=sum(r['metrics']['open_positions'] > 0 for r in cases),
            economic_contract_complete_folds=sum(r['economic_contract_complete'] for r in cases),
            buy_executions=sum(r['buy_executions'] for r in cases),
            max_known_proxy_drawdown=max((r['metrics']['max_drawdown'] for r in cases
                if r['metrics']['max_drawdown'] is not None), default=None))
    return summary


def _panel(prices, features, plan, minimum):
    panel = features.merge(prices[['date', 'symbol', 'value']], on=['date', 'symbol'], validate='one_to_one')
    return panel.loc[panel.date.ge(plan['development_start'])&panel.date.lt(plan['holdout_start'])
        &panel.value.ge(minimum)].reset_index(drop=True)


def _folds(calendar, plan):
    training = plan['training']
    return time_folds(calendar[calendar >= plan['development_start']],
        min_train_sessions=training['initial_sessions'], test_sessions=training['test_sessions'],
        embargo_sessions=training['embargo_sessions'], end_exclusive=plan['holdout_start'])


def _replay(prepared, picks, calendar, config, fold, name):
    return replay_portfolio(prepared, picks, calendar, replace(config, stress=name.endswith('-stress')),
        start=fold.test_start, end=fold.test_end_exclusive, dip_rule=RULE,
        entry_allocation_fraction=1., loss_floor_exit=True)


def _financial_record(replay, picks, sized, days):
    if not len(days):
        raise ValueError('A nonempty financial evaluation calendar is required')
    selected = picks.merge(sized[['date', 'symbol', 'state', 'resolved_at']],
        on=['date', 'symbol'], how='left', validate='one_to_one')
    if selected.state.isna().any():
        raise ValueError('A sized selection outcome is missing from the evaluation denominator')
    known=selected.state.ne('unknown')
    if (not selected.state.isin(['complete','entry_blocked','unknown']).all()
            or not selected.resolved_at.notna().equals(known)
            or any(not is_canonical_iso_date(day) for day in selected.loc[known,'resolved_at'])
            or selected.loc[known,'resolved_at'].le(selected.loc[known,'date']).any()):
        raise ValueError('Selected outcome status or resolution date is inconsistent')
    late=known & selected.resolved_at.fillna('').gt(days[-1])
    unknown=int((~known | late).sum())
    m = replay.metrics
    # This is only a local accounting-path completeness flag, never validated
    # historical fees, membership, real fills, or economic profitability.
    economic = bool(m['status'] == 'complete' and not unknown and not m['corporate_action_events']
        and not m['stale_mark_days'] and not m['open_positions'] and replay.equity.date.tolist() == list(days))
    return dict(metrics=m, assumptions=replay.assumptions, selections=len(picks), unknown_selected_outcomes=unknown,
        selected_outcomes_resolved_after_fold_end=int(late.sum()),
        economic_contract_complete=economic,
        buy_executions=int(replay.executions.side.eq('buy').sum()) if len(replay.executions) else 0)


def _outcomes(prepared, panel, calendar, plan):
    common = dict(fees=plan['fees'], participation=plan['participation'],
        slippage_bps=plan['slippage_bps'], end_exclusive=plan['holdout_start'])
    return (sized_dip_attempt_outcomes(prepared, panel[['date', 'symbol']], calendar,
        budget_rial=plan['budget_rial'], **common),
        dip_attempt_outcomes(prepared, panel[['date', 'symbol']], calendar, **common))


def run_sized_study(registry, source_db, *, plan, root):
    config = validate_plan(plan)
    _registry(registry, plan['experiment_id']+'-PLAN', plan, {}, 'registered')
    source_root = settings.DATA_DIR/'research'
    source_file = source_root/(plan['source_id']+'.json')
    if sha256(source_file) != plan['source_report_sha256']:
        raise ValueError('The pinned source report differs')
    source = json.loads(source_file.read_text(encoding='utf-8'))
    _registry(source_db, plan['source_id'], source['params'], _compact_metrics(_unknown_as_nan(source['results'])), 'completed')
    _, inputs, digests = verified_baseline_inputs(source_root, plan['source_id'])
    if digests != plan['source_inputs_sha256']:
        raise ValueError('The pinned source inputs differ')
    root = Path(root).resolve()
    from ops.report_files import private_report_path

    private_report_path(root/'result.json', forbidden_paths=(settings.DB_PATH,))
    root.mkdir(parents=True, exist_ok=False)
    code_hash = research_code_fingerprint()
    code = archive_research_code(root/'code')
    protocol = dict(version=VERSION, registered_at=now_tehran_iso(), pre_code_plan=plan,
        code_sha256=code_hash, source_files=code, execution_contract=execution_contract(plan['budget_rial']),
        baseline_selection_version=BASELINE_SELECTION_VERSION,
        baseline_resolution_version=DIP_BASELINE_RESOLUTION_VERSION,
        min_daily_value_rial=source['params']['min_daily_value_rial'], config=asdict(config),
        empty_column_serialization='object-index-v1', last_test_decision_sessions_skipped=2,
        independent_accounts_per_fold=True, economic_contract_flag_is_local_only=True,
        accepted_for_signals=False, holdout_opened=False)
    _dump(root/'protocol.json', protocol)

    def register(suffix, metrics, status):
        row = registry.log_experiment(experiment_id=plan['experiment_id']+suffix, strategy_name=VERSION,
            symbols_tested='fixed frozen public liquid development rows',
            parameters_json=json.dumps(protocol, ensure_ascii=False),
            metrics_json=json.dumps(_json_safe(metrics), ensure_ascii=False, allow_nan=False), status=status,
            dataset_version=plan['source_report_sha256'])
        if row is None:
            raise RuntimeError('Independent sized-DIP registration failed')
        _registry(registry, plan['experiment_id']+suffix, protocol, metrics, status)

    register('-PROTOCOL', {}, 'registered')
    for name in ('prices', 'features'):
        shutil.copyfile(source_root/plan['source_id']/(name+'.parquet'), root/(name+'.parquet'))
    prices = inputs['prices']
    panel = _panel(prices, inputs['features'], plan, protocol['min_daily_value_rial'])
    calendar = market_calendar(prices)
    prepared = prepare_portfolio_prices(prices)
    sized, unit = _outcomes(prepared, panel, calendar, plan)
    for name, frame in [('sized-outcomes', sized), ('unit-outcomes', unit)]:
        frame.to_parquet(root/(name+'.parquet'), index=False)
    _dump(root/'price-preparation.json', dict(version=prepared.version, rows=prepared.row_count,
        frame_fingerprint=prepared.frame_fingerprint))
    folds = _folds(calendar, plan)
    if not folds:
        raise ValueError('No chronological sized-DIP research folds')
    records = []
    for index, fold in enumerate(folds):
        train = panel.loc[panel.date.ge(fold.train_start)&panel.date.lt(fold.train_end_exclusive)].reset_index(drop=True)
        test = panel.loc[panel.date.ge(fold.test_start)&panel.date.lt(fold.test_end_exclusive)].reset_index(drop=True)
        models, forecasts, jobs, days, best, diagnostics = fold_candidates(
            train, test, sized, unit, calendar, fold, index, plan)
        for variant, model in models.items():
            _dump(root/f'fold-{index:02d}-{variant}-model.json', model)
            forecasts[variant].to_parquet(root/f'fold-{index:02d}-{variant}-forecast.parquet', index=False)
        # All candidates and controls are frozen before reading this fold's
        # portfolio returns. A failed or unknown mode is never replaced.
        for name, picks in jobs.items():
            picks.to_parquet(root/f'fold-{index:02d}-{name}-picks.parquet', index=False)
        record = dict(fold=asdict(fold), index=index, best_past=best, past_selection=diagnostics, modes={})
        for name, picks in jobs.items():
            replay = _replay(prepared, picks, calendar, config, fold, name)
            for kind in ('equity', 'executions', 'decisions'):
                _empty_column_axis(getattr(replay, kind)).to_parquet(root/f'fold-{index:02d}-{name}-{kind}.parquet', index=False)
            record['modes'][name] = _financial_record(replay, picks, sized, days)
        records.append(record)
        print(json.dumps(dict(fold=index+1, total_folds=len(folds),
            picks={k: len(jobs[k]) for k in [*VARIANTS, 'unit-reference']}), ensure_ascii=False), flush=True)
    if code_hash != research_code_fingerprint():
        raise ValueError('Research source changed during the sized-DIP study')
    result = dict(protocol=protocol, completed_at=now_tehran_iso(), records=records, summary=_summary(records),
        outcome_rows=len(sized), sized_outcome_coverage=sized.state.value_counts().to_dict(),
        unit_outcome_coverage=unit.state.value_counts().to_dict(), **dict.fromkeys(FALSE_FLAGS, False))
    result['artifacts_sha256'] = {p.name: sha256(p) for p in root.iterdir() if p.is_file()}
    _dump(root/'result.json', result)
    register('', result['summary'], 'completed')
    return result


def review_sized_study(registry, folder):
    """Recompute raw targets, all models, selections, controls and cash ledgers offline."""
    folder = Path(folder).resolve()
    from ops.report_files import private_report_path

    private_report_path(folder/'result.json', forbidden_paths=(settings.DB_PATH,))
    result = json.loads((folder/'result.json').read_text(encoding='utf-8'))
    protocol = result['protocol']
    plan = protocol['pre_code_plan']
    config = validate_plan(plan)
    if (protocol['version'] != VERSION or protocol['execution_contract'] != execution_contract(plan['budget_rial'])
            or protocol['code_sha256'] != research_code_fingerprint() or protocol['config'] != asdict(config)
            or protocol.get('baseline_selection_version') != BASELINE_SELECTION_VERSION
            or protocol.get('baseline_resolution_version') != DIP_BASELINE_RESOLUTION_VERSION
            or protocol['last_test_decision_sessions_skipped'] != 2
            or protocol.get('empty_column_serialization') != 'object-index-v1'
            or protocol.get('economic_contract_flag_is_local_only') is not True
            or any(result.get(k) is not False for k in FALSE_FLAGS)):
        raise ValueError('The archived sized-DIP contract differs')
    for suffix, params, metrics, status in [('-PLAN', plan, {}, 'registered'),
            ('-PROTOCOL', protocol, {}, 'registered'), ('', protocol, result['summary'], 'completed')]:
        _registry(registry, plan['experiment_id']+suffix, params, metrics, status)
    if protocol != json.loads((folder/'protocol.json').read_text(encoding='utf-8')):
        raise ValueError('The sized-DIP protocol copy differs')
    for base, hashes in [(folder/'code', protocol['source_files']), (folder, result['artifacts_sha256'])]:
        for name, digest in hashes.items():
            path = (base/name).resolve()
            if not path.is_relative_to(base) or sha256(path) != digest:
                raise ValueError('An archived sized-DIP source artifact differs')
    prices, features = [read_development_parquet(folder/(name+'.parquet'),
        plan['source_inputs_sha256'][name], plan['holdout_start']) for name in ('prices', 'features')]
    panel = _panel(prices, features, plan, protocol['min_daily_value_rial'])
    calendar = market_calendar(prices)
    prepared = prepare_portfolio_prices(prices)
    if json.loads((folder/'price-preparation.json').read_text(encoding='utf-8')) != dict(
            version=prepared.version, rows=prepared.row_count, frame_fingerprint=prepared.frame_fingerprint):
        raise ValueError('The prepared sized-DIP raw price view differs')
    sized, unit = _outcomes(prepared, panel, calendar, plan)
    for name, frame in [('sized-outcomes', sized), ('unit-outcomes', unit)]:
        pd.testing.assert_frame_equal(frame, pd.read_parquet(folder/(name+'.parquet')), check_exact=True)
    folds = _folds(calendar, plan)
    if len(folds) != len(result['records']):
        raise ValueError('The chronological fold denominator differs')
    reviewed = 0
    for index, (fold, record) in enumerate(zip(folds, result['records'], strict=True)):
        if record['index'] != index or record['fold'] != asdict(fold) or set(record['modes']) != set(modes()):
            raise ValueError('The sized-DIP fold identity or mode denominator differs')
        train = panel.loc[panel.date.ge(fold.train_start)&panel.date.lt(fold.train_end_exclusive)].reset_index(drop=True)
        test = panel.loc[panel.date.ge(fold.test_start)&panel.date.lt(fold.test_end_exclusive)].reset_index(drop=True)
        models, forecasts, jobs, days, best, diagnostics = fold_candidates(
            train, test, sized, unit, calendar, fold, index, plan)
        if _json_safe(best) != record['best_past'] or _json_safe(diagnostics) != record['past_selection']:
            raise ValueError('The past-only sized baseline selection differs')
        for variant, model in models.items():
            if _json_safe(model) != json.loads((folder/f'fold-{index:02d}-{variant}-model.json').read_text(encoding='utf-8')):
                raise ValueError('A sized-DIP refitted model differs')
            pd.testing.assert_frame_equal(forecasts[variant],
                pd.read_parquet(folder/f'fold-{index:02d}-{variant}-forecast.parquet'), check_exact=True)
        for name, picks in jobs.items():
            pd.testing.assert_frame_equal(picks, pd.read_parquet(folder/f'fold-{index:02d}-{name}-picks.parquet'), check_exact=True)
            replay = _replay(prepared, picks, calendar, config, fold, name)
            for kind in ('equity', 'executions', 'decisions'):
                pd.testing.assert_frame_equal(_empty_column_axis(getattr(replay, kind)),
                    pd.read_parquet(folder/f'fold-{index:02d}-{name}-{kind}.parquet'), check_exact=True)
            if _json_safe(_financial_record(replay, picks, sized, days)) != record['modes'][name]:
                raise ValueError('The sized-DIP financial ledger or coverage differs')
            reviewed += 1
    if (result['summary'] != _summary(result['records']) or result['outcome_rows'] != len(sized)
            or result['sized_outcome_coverage'] != sized.state.value_counts().to_dict()
            or result['unit_outcome_coverage'] != unit.state.value_counts().to_dict()):
        raise ValueError('The sized-DIP summary or outcome denominator differs')
    if protocol['code_sha256'] != research_code_fingerprint():
        raise ValueError('Source changed during the offline review')
    return dict(verified_at=now_tehran_iso(), outcome_rows=len(sized), folds=len(folds), financial_modes=reviewed,
        outcomes_recomputed=True, models_refitted=True, forecasts_and_all_picks_recomputed=True,
        cash_ledgers_reexecuted=True, **dict.fromkeys(FALSE_FLAGS, False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    from database.db import DatabaseManager

    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    with DatabaseManager(args.registry) as registry, DatabaseManager(read_only=True) as source_db:
        result = run_sized_study(registry, source_db, plan=plan, root=args.output)
        review = review_sized_study(registry, args.output)
        _dump(args.output/'review.json', review)
    print(json.dumps(dict(folds=len(result['records']), summary=result['summary'], review=review), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
