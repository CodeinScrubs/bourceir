"""Temporary-data study: preregistration, size targets, controls and complete cash replay."""

import json
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from config import settings
from ops.research_review import sha256
from research.dip_outcomes import RULE
from research.expectancy import PREDICTORS
from research.sized_dip_expectancy import CAPACITY_FORMULA, CAPACITY_NAME, VARIANTS
from research.sized_dip_run import VERSION, modes, review_sized_study, run_sized_study, validate_plan

pytest_plugins = ['tests.test_flow_research']


def plan_for(source, base):
    sid = source['experiment_id']
    return dict(version='sized-dip-pre-code-v1', experiment_id='EXP-test-sized-dip', source_id=sid,
        source_report_sha256=sha256(base/'research'/(sid+'.json')),
        source_inputs_sha256={n: sha256(base/'research'/sid/(n+'.parquet')) for n in ('features','labels','prices')},
        rule=asdict(RULE), predictors=list(PREDICTORS), variants=list(VARIANTS), modes=modes(), fees=dict(settings.FEES),
        entry_day_exit=False, holdout_opened=False, accepted_for_signals=False, parameter_search=False,
        additional_feature_or_threshold_search=False, ridge_penalty=.01,
        holdout_start=settings.RESEARCH_CONFIG['holdout_start'], development_start=settings.RESEARCH_CONFIG['dev_start'],
        max_positions=5, participation=.05, loss_floor_exit=True, entry_allocation_fraction=1., random_seeds=[41,97,173],
        training=dict(initial_sessions=104, test_sessions=30, embargo_sessions=3), min_model_sessions=3, min_model_rows=3,
        initial_capital_rial=100_000_000, budget_rial=20_000_000, slippage_bps=10, max_loss_pct=.1,
        prespecified_feature_variants=2, new_financial_modes=25, horizon_variants=1, capital_is_owner_account=False,
        target_global_account_interactions_modeled=False, target_account_loss_floor_modeled=False,
        outcome_labels_are_portfolio_returns=False, probability_of_profit=None, statistical_superiority_claim_permitted=False,
        capacity_feature=dict(name=CAPACITY_NAME, formula=CAPACITY_FORMULA))


def test_registered_pipeline_refits_every_variant_and_reexecutes_all_modes(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research import sized_dip_run as module
    from research.dip_expectancy_run import fold_candidates as original_candidates
    from research.run import run_research

    _, base = flow_baseline
    for symbol in ('الف', 'ب', 'ج'):
        price = db.get_daily_prices_df(symbol)
        mask = np.arange(len(price)) >= 95
        ref = price.loc[mask, 'ref_price_raw']
        price.loc[mask, 'open_raw'] = ref*.97
        price.loc[mask, 'low_raw'] = ref*.96
        price.loc[mask, 'high_raw'] = ref*1.02
        price.loc[mask, 'close_raw'] = ref*1.01
        price.loc[mask, 'value'] = price.loc[mask, 'volume']*ref*.99
        sid = next(s['id'] for s in db.get_universe_symbols(include_inactive=True) if s['symbol_fa'] == symbol)
        db.upsert_daily_prices_df(sid, price)
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    plan = plan_for(source, base)
    assert db.log_experiment(experiment_id=plan['experiment_id']+'-PLAN', strategy_name=plan['version'],
        symbols_tested='temporary fixture', parameters_json=json.dumps(plan), metrics_json='{}', status='registered') is not None
    fit_original, calls = module.fit_sized_model, []

    def guarded(*args, **kwargs):
        row = db.conn.execute('SELECT parameters_json FROM experiments WHERE experiment_id=?',
            (plan['experiment_id']+'-PROTOCOL',)).fetchone()
        assert row and json.loads(row[0])['execution_contract']['budget_rial'] == plan['budget_rial']
        calls.append(kwargs['variant'])
        return fit_original(*args, **kwargs)

    monkeypatch.setattr(module, 'fit_sized_model', guarded)
    folder = base/'private'/'sized-integration'
    result = run_sized_study(db, db, plan=plan, root=folder)
    review = review_sized_study(db, folder)
    assert set(calls) == set(VARIANTS)
    assert review['financial_modes'] == len(result['records'])*25
    assert review['cash_ledgers_reexecuted'] and review['models_refitted']
    assert set(result['summary']) == set(modes())
    assert result['summary']['buyer-power']['buy_executions'] > 0
    assert not result['accepted_for_signals'] and not result['economic_profit_verified']
    assert result['outcome_rows'] == sum(result['sized_outcome_coverage'].values())
    assert not (base/'research'/'holdout-unsealed.json').exists()
    # The original unit selector must reproduce exactly; it is an unchanged
    # control, not a newly optimized method that merely shares its name.
    prices, features = pd.read_parquet(folder/'prices.parquet'), pd.read_parquet(folder/'features.parquet')
    panel = module._panel(prices, features, plan, result['protocol']['min_daily_value_rial'])
    calendar = module.market_calendar(prices)
    unit = pd.read_parquet(folder/'unit-outcomes.parquet')
    for index, fold in enumerate(module._folds(calendar, plan)):
        train = panel.loc[panel.date.ge(fold.train_start)&panel.date.lt(fold.train_end_exclusive)].reset_index(drop=True)
        test = panel.loc[panel.date.ge(fold.test_start)&panel.date.lt(fold.test_end_exclusive)].reset_index(drop=True)
        model, forecast, jobs, *_ = original_candidates(train, test, unit, calendar, fold, validate_plan(plan), index, plan)
        assert model == json.loads((folder/f'fold-{index:02d}-unit-reference-model.json').read_text(encoding='utf-8'))
        pd.testing.assert_frame_equal(forecast, pd.read_parquet(folder/f'fold-{index:02d}-unit-reference-forecast.parquet'))
        pd.testing.assert_frame_equal(jobs['conditional'], pd.read_parquet(folder/f'fold-{index:02d}-unit-reference-picks.parquet'))
    artifact = folder/'fold-00-sized11-model.json'
    original = artifact.read_bytes()
    changed = json.loads(original)
    changed['budget_rial'] += 1
    artifact.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError, match='artifact'):
        review_sized_study(db, folder)
    artifact.write_bytes(original)


@pytest.mark.parametrize('change', ['version','fees','rule','budget','capacity','modes','cutoff','same_day','slippage'])
def test_plan_mismatch_stops_before_any_database_or_market_access(change, tmp_path):
    # Complete shape, but no market file or registry exists. Validation must be
    # the first operation, before either unavailable dependency is touched.
    fake = dict(experiment_id='unused')
    base = tmp_path/'research'
    base.mkdir()
    (base/'unused.json').write_text('{}')
    (base/'unused').mkdir()
    for name in ('features','labels','prices'):
        (base/'unused'/(name+'.parquet')).write_bytes(b'')
    plan = plan_for(fake, tmp_path)
    if change == 'version':
        plan['version'] = 'wrong'
    elif change == 'fees':
        plan['fees']['buy'] = 0.
    elif change == 'rule':
        plan['rule']['dip'] = .03
    elif change == 'budget':
        plan['budget_rial'] += 1
    elif change == 'capacity':
        plan['capacity_feature']['formula'] = 'forward_volume'
    elif change == 'modes':
        plan['modes'].pop()
    elif change == 'cutoff':
        plan['holdout_start'] = '2099-01-01'
    elif change == 'slippage':
        plan['slippage_bps'] = 0
    else:
        plan['entry_day_exit'] = True
    with pytest.raises(ValueError):
        run_sized_study(None, None, plan=plan, root=None)


def test_study_has_25_distinct_preregistered_modes():
    assert VERSION == 'sized-dip-study-v1'
    assert len(modes()) == len(set(modes())) == 25


@pytest.mark.parametrize(('state','resolved','unknown','late','complete'),[
    ('complete','2024-01-03',0,0,True),
    ('complete','2024-01-04',1,1,False),
    ('unknown',None,1,0,False),
])
def test_financial_coverage_censors_results_unavailable_at_fold_end(state,resolved,unknown,late,complete):
    from types import SimpleNamespace

    from research.sized_dip_run import _financial_record

    days=['2024-01-01','2024-01-02','2024-01-03']
    picks=pd.DataFrame([dict(date=days[0],symbol='الف')])
    outcomes=picks.assign(state=state,resolved_at=resolved)
    replay=SimpleNamespace(metrics=dict(status='complete',corporate_action_events=0,stale_mark_days=0,open_positions=0),
        assumptions={},equity=pd.DataFrame(dict(date=days)),executions=pd.DataFrame())
    result=_financial_record(replay,picks,outcomes,days)
    assert result['unknown_selected_outcomes']==unknown
    assert result['selected_outcomes_resolved_after_fold_end']==late
    assert result['economic_contract_complete'] is complete


def test_financial_coverage_rejects_missing_or_contradictory_outcomes():
    from types import SimpleNamespace

    from research.sized_dip_run import _financial_record

    picks=pd.DataFrame([dict(date='2024-01-01',symbol='الف')])
    replay=SimpleNamespace(metrics={},assumptions={},equity=pd.DataFrame(),executions=pd.DataFrame())
    with pytest.raises(ValueError,match='missing'):
        _financial_record(replay,picks,picks.iloc[:0].assign(state=None,resolved_at=None),['2024-01-01'])
    with pytest.raises(ValueError,match='resolution'):
        _financial_record(replay,picks,picks.assign(state='complete',resolved_at=None),['2024-01-01'])
