"""Temporary-data integration: registration, matched targets, full refit and cash replay."""

import json
from dataclasses import asdict

import numpy as np
import pytest

from config import settings
from ops.research_review import sha256
from research.dip_expectancy_run import VERSION, modes, review_dip_study, run_dip_study
from research.dip_outcomes import RULE
from research.expectancy import PREDICTORS

pytest_plugins=['tests.test_flow_research']


def registered_plan(db,source,base):
    sid=source['experiment_id']
    plan=dict(version='execution-aligned-dip-pre-code-v1',experiment_id='EXP-test-dip-aligned',
        source_id=sid,source_report_sha256=sha256(base/'research'/(sid+'.json')),
        source_inputs_sha256={n:sha256(base/'research'/sid/(n+'.parquet')) for n in ('features','labels','prices')},
        rule=asdict(RULE),predictors=list(PREDICTORS),modes=modes(),fees=dict(settings.FEES),
        entry_day_exit=False,holdout_opened=False,accepted_for_signals=False,parameter_search=False,
        feature_search=False,ridge_penalty=.01,holdout_start=settings.RESEARCH_CONFIG['holdout_start'],
        development_start=settings.RESEARCH_CONFIG['dev_start'],max_positions=5,participation=.05,
        loss_floor_exit=True,entry_allocation_fraction=1.0,random_seeds=[41,97,173],
        training=dict(initial_sessions=104,test_sessions=30,embargo_sessions=3),
        min_model_sessions=3,min_model_rows=3,initial_capital_rial=100000000,
        slippage_bps=10,max_loss_pct=.10)
    assert db.log_experiment(experiment_id=plan['experiment_id']+'-PLAN',strategy_name=plan['version'],
        symbols_tested='temporary fixture',parameters_json=json.dumps(plan),metrics_json='{}',status='registered') is not None
    return plan


def test_registered_pipeline_recomputes_models_all_picks_and_every_financial_mode(db,flow_baseline,monkeypatch):
    from features.store import build_feature_store
    from research import dip_expectancy_run as module
    from research.run import run_research

    source,base=flow_baseline
    # A real temporary feature rebuild after changing the test-only raw source;
    # the frozen baseline is never mixed with later revised raw prices.
    for symbol in ('الف','ب','ج'):
        p=db.get_daily_prices_df(symbol)
        mask=np.arange(len(p))>=95
        ref=p.loc[mask,'ref_price_raw']
        p.loc[mask,'open_raw']=ref*.97
        p.loc[mask,'low_raw']=ref*.96
        p.loc[mask,'high_raw']=ref*1.02
        p.loc[mask,'close_raw']=ref*1.01
        p.loc[mask,'value']=p.loc[mask,'volume']*ref*.99
        sid=next(s['id'] for s in db.get_universe_symbols(include_inactive=True) if s['symbol_fa']==symbol)
        db.upsert_daily_prices_df(sid,p)
    build_feature_store(db)
    source=run_research(db,random_trials=1)
    plan=registered_plan(db,source,base)
    original=module.fit_dip_model
    registered_calls=[]
    def guarded(*args,**kwargs):
        row=db.conn.execute('SELECT parameters_json FROM experiments WHERE experiment_id=?',
            (plan['experiment_id']+'-PROTOCOL',)).fetchone()
        assert row and json.loads(row[0])['execution_contract']['entry_day_exit'] is False
        registered_calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'fit_dip_model',guarded)
    folder=base/'private'/'dip-integration'
    result=run_dip_study(db,db,plan=plan,root=folder)
    review=review_dip_study(db,folder)
    assert registered_calls and review['cash_ledgers_reexecuted'] and review['models_refitted']
    assert review['financial_modes']==len(result['records'])*15
    assert set(result['summary'])==set(modes())
    assert result['summary']['buyer-power']['buy_executions']>0
    assert result['outcome_rows']==sum(result['outcome_coverage'].values())
    assert not result['accepted_for_signals'] and not result['economic_profit_verified']
    assert not (base/'research'/'holdout-unsealed.json').exists()
    model_path=next(folder.glob('fold-*-model.json'))
    original_bytes=model_path.read_bytes()
    changed=json.loads(original_bytes)
    changed['execution_contract']['entry_day_exit']=True
    model_path.write_text(json.dumps(changed),encoding='utf-8')
    with pytest.raises(ValueError,match='artifact'):
        review_dip_study(db,folder)
    model_path.write_bytes(original_bytes)


@pytest.mark.parametrize('change',['version','fees','rule','same_day','modes','cutoff'])
def test_plan_mismatch_stops_before_database_or_market_inputs(change):
    plan=dict(version='execution-aligned-dip-pre-code-v1',rule=asdict(RULE),predictors=list(PREDICTORS),
        modes=modes(),fees=dict(settings.FEES),entry_day_exit=False,holdout_opened=False,accepted_for_signals=False,
        parameter_search=False,feature_search=False,ridge_penalty=.01,holdout_start=settings.RESEARCH_CONFIG['holdout_start'],
        development_start=settings.RESEARCH_CONFIG['dev_start'],max_positions=5,participation=.05,
        loss_floor_exit=True,entry_allocation_fraction=1.0,random_seeds=[41,97,173],
        training=dict(initial_sessions=504,test_sessions=252,embargo_sessions=3),
        min_model_sessions=60,min_model_rows=200,initial_capital_rial=10000000000,slippage_bps=10,max_loss_pct=.10)
    if change=='version':
        plan['version']='wrong'
    elif change=='fees':
        plan['fees']['buy']=0.
    elif change=='rule':
        plan['rule']['dip']=.03
    elif change=='same_day':
        plan['entry_day_exit']=True
    elif change=='modes':
        plan['modes']=plan['modes'][:-1]
    else:
        plan['holdout_start']='2099-01-01'
    with pytest.raises(ValueError):
        run_dip_study(None,None,plan=plan,root=None)


def test_pipeline_version_is_independent_of_the_vwap_expectancy_family():
    assert VERSION=='execution-aligned-dip-study-v1'
    assert len(modes())==15 and len(set(modes()))==15
