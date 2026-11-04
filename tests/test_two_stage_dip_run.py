"""Fixed model comparison: no-entry accounting, censoring and replay provenance."""

import json
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from config import settings
from ops.research_review import sha256
from research.dip_outcomes import RULE
from research.expectancy import PREDICTORS
from research.sized_dip_expectancy import CAPACITY_FORMULA, CAPACITY_NAME
from research.sized_dip_outcomes import execution_contract
from research.two_stage_dip_run import _validate_outcomes, modes, prediction_metrics, validate_plan
from tests.test_two_stage_dip import BUDGET, inputs

pytest_plugins=['tests.test_flow_research']


def plan_for(parent,folder):
    prior=parent['protocol']['pre_code_plan']
    budget=prior['budget_rial']
    return dict(version='two-stage-sized-dip-pre-code-v1',experiment_id='EXP-test-two-stage',
        parent_id=prior['experiment_id'],parent_result_sha256=sha256(folder/'result.json'),
        parent_review_sha256=sha256(folder/'review.json'),
        input_hashes={n:parent['artifacts_sha256'][n] for n in ('prices.parquet','features.parquet','sized-outcomes.parquet')},
        holdout_start=settings.RESEARCH_CONFIG['holdout_start'],development_start=settings.RESEARCH_CONFIG['dev_start'],
        predictors=[*PREDICTORS,CAPACITY_NAME],capacity_feature=dict(name=CAPACITY_NAME,formula=CAPACITY_FORMULA),
        fill_target='normal_entry_filled; proxy outcome observed at next entry session, including an entry whose later exit is unknown',
        fill_label_rule='only boolean entry outcomes with canonical entry_date after decision and strictly before train cutoff; unknown entry outcomes remain unknown',
        conditional_targets='normal_net and min(normal_net,actual_stress_net) conditional on known normal entry',
        conditional_label_rule='normal entry filled and both modeled paths complete with resolved_at strictly before train cutoff; censored/unknown outcomes counted and excluded from fitting',
        expected_formula='proxy_p_normal_entry * conditional_mean_return; require zero in both scenarios for known normal no-entry',
        probability_semantics='normal proxy fill in OHLC model, not real queue fill, not probability of profit, not calibrated',
        gate='normal and scenario-floor expected values strictly greater than zero, at most five highest floor forecasts; last two test decision sessions excluded',
        budget_rial=budget,fill_model=dict(kind='weighted_l2_logistic_newton',penalty=.01,max_iterations=50,
            gradient_tolerance=1e-8,max_backtracking_steps=20,training_weight='equal weight per decision date',
            one_class='unavailable'),conditional_ridge_penalty=.01,fill_probability_threshold=None,
        threshold_search=False,parameter_search=False,outcome_censoring_unbiased_assumption=False,
        independent_test=False,outcomes_and_previous_parent_results_previously_observed=True,
        statistical_superiority_claim_permitted=False,economic_profit_verified=False,accepted_for_signals=False,
        holdout_opened=False,new_modes=modes(),financial_comparisons=['existing_sized11','existing_sized10','idle_cash',
            'same_count_random41','same_count_random97','same_count_random173'],training=prior['training'],
        min_model_sessions=prior['min_model_sessions'],min_model_rows=prior['min_model_rows'],
        financial_contract=dict(rule=asdict(RULE),fees=dict(settings.FEES),slippage_bps=10,participation=.05,
            initial_capital_rial=budget*5,max_positions=5,loss_floor_exit=True,max_loss_pct=.1,
            entry_day_exit=False,independent_accounts_per_fold=True))


def outcome_contract():
    _,outcomes=inputs()
    outcomes['stress_entry_filled']=outcomes.normal_entry_filled.copy()
    outcomes['normal_observed_net']=outcomes.normal_net.copy()
    outcomes['stress_observed_net']=outcomes.floor_net.copy()
    return outcomes


@pytest.mark.parametrize('mutation',['blocked_stress_fill','blocked_return','floor','missing_return','late_date',
                                      'filled_blocked','filled_resolution','nonboolean','boolean_return'])
def test_incoherent_entry_or_return_is_rejected(mutation):
    outcomes=outcome_contract()
    if mutation=='blocked_stress_fill':
        outcomes.loc[2,'stress_entry_filled']=True
    elif mutation=='blocked_return':
        outcomes.loc[2,['normal_net','normal_observed_net']]=[.1,.1]
    elif mutation=='floor':
        outcomes.loc[0,'floor_net']=outcomes.loc[0,'normal_net']+.1
    elif mutation=='missing_return':
        outcomes.loc[0,'normal_net']=np.nan
    elif mutation=='late_date':
        outcomes.loc[0,'resolved_at']='2025-03-21'
    elif mutation=='filled_blocked':
        outcomes.loc[0,'state']='entry_blocked'
    elif mutation=='filled_resolution':
        outcomes.loc[0,'resolved_at']=outcomes.loc[0,'normal_entry_date']
    elif mutation=='nonboolean':
        outcomes.loc[0,'normal_entry_filled']=1
    else:
        outcomes['normal_net']=outcomes.normal_net.astype(object)
        outcomes.loc[0,'normal_net']=True
    with pytest.raises(ValueError):
        _validate_outcomes(outcomes,'2025-03-21')


def test_later_return_stays_unknown_at_fold_end_but_known_entry_informs_fill_score():
    keys=pd.DataFrame(dict(date=['2023-05-04']*4,symbol=['الف','ب','ج','د']))
    forecast=keys.assign(status=['forecasted']*3+['unknown_features'],
        normal_expected_net=[.01,.01,.01,np.nan],scenario_floor_expected_net=[-.01,-.01,-.01,np.nan],
        proxy_entry_probability=[.75,.25,.5,np.nan])
    baseline=keys.assign(status='forecasted',normal_expected_net=.01,scenario_floor_expected_net=-.01)
    outcomes=keys.assign(state=['complete','entry_blocked','unknown','unknown'],
        resolved_at=['2023-05-08','2023-05-05',None,None],normal_net=[.02,0.,np.nan,np.nan],
        floor_net=[-.02,0.,np.nan,np.nan],normal_entry_filled=[True,False,True,None],
        normal_entry_date=['2023-05-05']*4)
    result=prediction_metrics(forecast,baseline,outcomes,end_exclusive='2023-05-07',training_rate=.5)
    assert result['rows']==4 and result['known_entry']==3 and result['unknown_or_late_entry']==1
    assert result['known_joint_return_by_fold_end']==1 and result['unknown_or_late_joint_return']==3
    assert result['entry_brier']['paired_rows']==3 and result['entry_brier']['mean_squared_error']==pytest.approx(.125)
    assert result['entry_brier']['baseline_mean_squared_error']==pytest.approx(.25)
    assert result['returns']['normal']['paired_rows']==1
    with pytest.raises(ValueError,match='missing'):
        prediction_metrics(forecast,baseline,outcomes.iloc[:-1],end_exclusive='2023-05-07',training_rate=.5)
    forecast.loc[0,'proxy_entry_probability']=np.nan
    with pytest.raises(ValueError,match='status'):
        prediction_metrics(forecast,baseline,outcomes,end_exclusive='2023-05-07',training_rate=.5)


def test_registered_study_recomputes_labels_forecasts_and_80_mode_ledgers(db,flow_baseline):
    from features.store import build_feature_store
    from research.dip_expectancy_run import _dump
    from research.run import run_research
    from research.sized_dip_run import review_sized_study, run_sized_study
    from research.two_stage_dip_run import review_study, run_study
    from tests.test_sized_dip_run import plan_for as parent_plan_for

    _,base=flow_baseline
    for symbol in ('الف','ب','ج'):
        price=db.get_daily_prices_df(symbol)
        index=np.arange(len(price))
        mask=index>=95
        ref=price.loc[mask,'ref_price_raw'].to_numpy()
        fill=(index[mask]%2)==0
        price.loc[mask,'open_raw']=ref*np.where(fill,.97,.995)
        price.loc[mask,'low_raw']=ref*np.where(fill,.96,.99)
        price.loc[mask,'high_raw']=ref*1.02
        price.loc[mask,'close_raw']=ref*1.01
        price.loc[mask,'value']=price.loc[mask,'volume']*ref*.99
        sid=next(s['id'] for s in db.get_universe_symbols(include_inactive=True) if s['symbol_fa']==symbol)
        db.upsert_daily_prices_df(sid,price)
    build_feature_store(db)
    source=run_research(db,random_trials=1)
    parent_plan=parent_plan_for(source,base)
    assert db.log_experiment(experiment_id=parent_plan['experiment_id']+'-PLAN',strategy_name=parent_plan['version'],
        symbols_tested='temporary test fixtures',parameters_json=json.dumps(parent_plan),metrics_json='{}',status='registered')
    parent=base/'private'/'parent-sized'
    parent_result=run_sized_study(db,db,plan=parent_plan,root=parent)
    _dump(parent/'review.json',review_sized_study(db,parent))
    plan=plan_for(parent_result,parent)
    assert db.log_experiment(experiment_id=plan['experiment_id']+'-PLAN',strategy_name=plan['version'],
        symbols_tested='temporary test fixtures',parameters_json=json.dumps(plan),metrics_json='{}',status='registered')
    output=base/'private'/'two-stage'
    result=run_study(db,parent,plan=plan,root=output)
    review=review_study(db,output)
    assert review['financial_modes']==len(result['records'])*8
    assert review['models_refitted'] and review['cash_ledgers_reexecuted'] and review['outcomes_recomputed']
    assert not result['accepted_for_signals'] and not result['holdout_opened']
    for i in range(len(result['records'])):
        picks=pd.read_parquet(output/f'fold-{i:02d}-two-stage-picks.parquet')
        for seed in (41,97,173):
            random=pd.read_parquet(output/f'fold-{i:02d}-random{seed}-picks.parquet')
            assert picks.groupby('date').size().to_dict()==random.groupby('date').size().to_dict()
    artifact=output/'fold-00-model.json'
    original=artifact.read_bytes()
    artifact.write_bytes(original+b' ')
    with pytest.raises(ValueError,match='artifact'):
        review_study(db,output)
    artifact.write_bytes(original)
    assert not (base/'research'/'holdout-unsealed.json').exists()


def test_plan_rejects_changed_threshold_or_execution_before_parent_access(tmp_path):
    from tests.test_sized_dip_run import plan_for as parent_plan_for

    base=tmp_path
    source={'experiment_id':'unused'}
    (base/'research'/'unused').mkdir(parents=True)
    (base/'research'/'unused.json').write_text('{}')
    for name in ('features','labels','prices'):
        (base/'research'/'unused'/(name+'.parquet')).write_bytes(b'')
    prior=parent_plan_for(source,base)
    folder=tmp_path/'parent'
    folder.mkdir()
    (folder/'result.json').write_text('{}')
    (folder/'review.json').write_text('{}')
    result={'protocol':{'pre_code_plan':prior},'artifacts_sha256':{n:'a'*64 for n in
        ('prices.parquet','features.parquet','sized-outcomes.parquet')}}
    plan=plan_for(result,folder)
    validate_plan(plan)
    plan['fill_probability_threshold']=.5
    with pytest.raises(ValueError):
        validate_plan(plan)
    plan['fill_probability_threshold']=None
    plan['financial_contract']['fees']['buy']=0.
    with pytest.raises(ValueError):
        validate_plan(plan)


def test_research_budget_is_exactly_representable_and_pinned():
    assert execution_contract(BUDGET)['budget_rial']==BUDGET
