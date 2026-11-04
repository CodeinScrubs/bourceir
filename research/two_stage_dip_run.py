"""Registered retrospective entry/return comparison with shared-cash replays.

This experiment reads a previously reviewed, already-seen development snapshot.
It never opens the sealed holdout or accepts a forecast as a trading signal.
"""

import argparse
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.portfolio import PortfolioConfig
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.db import DatabaseManager
from ops.research_review import read_development_parquet, sha256
from research.dip_expectancy_run import _dump, _empty_column_axis
from research.dip_outcomes import RULE
from research.expectancy import PREDICTORS, _keys
from research.expectancy_run import _random
from research.labels import market_calendar
from research.probability import _numeric
from research.run import _json_safe, archive_research_code, research_code_fingerprint
from research.sized_dip_expectancy import CAPACITY_FORMULA, CAPACITY_NAME
from research.sized_dip_outcomes import budget_value, execution_contract, sized_dip_attempt_outcomes
from research.sized_dip_run import _conditional, _financial_record, _folds, _panel, _replay
from research.surge_portfolio_run import _registry
from research.two_stage_dip import fit_two_stage, forecast_two_stage

VERSION='two-stage-sized-dip-study-v1'
PLAN_VERSION='two-stage-sized-dip-pre-code-v1'
SEEDS=(41,97,173)
FALSE_FLAGS=('accepted_for_signals','economic_profit_verified','holdout_opened',
             'statistical_superiority_claim','unseen_test')


def modes():
    return ['two-stage','two-stage-stress',
        *[f'random{seed}{suffix}' for seed in SEEDS for suffix in ('','-stress')]]


def validate_plan(plan):
    if not isinstance(plan,dict):
        raise ValueError('Registered study plan must be explicit')
    expected=dict(version=PLAN_VERSION,predictors=[*PREDICTORS,CAPACITY_NAME],
        holdout_start=settings.RESEARCH_CONFIG['holdout_start'],development_start=settings.RESEARCH_CONFIG['dev_start'],
        fill_target='normal_entry_filled; proxy outcome observed at next entry session, including an entry whose later exit is unknown',
        fill_label_rule='only boolean entry outcomes with canonical entry_date after decision and strictly before train cutoff; unknown entry outcomes remain unknown',
        conditional_targets='normal_net and min(normal_net,actual_stress_net) conditional on known normal entry',
        conditional_label_rule='normal entry filled and both modeled paths complete with resolved_at strictly before train cutoff; censored/unknown outcomes counted and excluded from fitting',
        expected_formula='proxy_p_normal_entry * conditional_mean_return; require zero in both scenarios for known normal no-entry',
        probability_semantics='normal proxy fill in OHLC model, not real queue fill, not probability of profit, not calibrated',
        gate='normal and scenario-floor expected values strictly greater than zero, at most five highest floor forecasts; last two test decision sessions excluded',
        fill_model=dict(kind='weighted_l2_logistic_newton',penalty=.01,max_iterations=50,gradient_tolerance=1e-8,
            max_backtracking_steps=20,training_weight='equal weight per decision date',one_class='unavailable'),
        conditional_ridge_penalty=.01,fill_probability_threshold=None,threshold_search=False,parameter_search=False,
        outcome_censoring_unbiased_assumption=False,independent_test=False,
        outcomes_and_previous_parent_results_previously_observed=True,
        statistical_superiority_claim_permitted=False,economic_profit_verified=False,accepted_for_signals=False,
        holdout_opened=False,new_modes=modes(),
        financial_comparisons=['existing_sized11','existing_sized10','idle_cash',
                               'same_count_random41','same_count_random97','same_count_random173'])
    if any(plan.get(k)!=v or isinstance(v,bool) and type(plan.get(k)) is not bool for k,v in expected.items()):
        raise ValueError('The registered two-stage plan differs from its fixed contract')
    capacity=plan.get('capacity_feature')
    if not isinstance(capacity,dict) or capacity.get('name')!=CAPACITY_NAME or capacity.get('formula')!=CAPACITY_FORMULA:
        raise ValueError('The registered decision-day capacity formula differs')
    for field in ('parent_result_sha256','parent_review_sha256'):
        if not isinstance(plan.get(field),str) or not re.fullmatch('[0-9a-f]{64}',plan[field]):
            raise ValueError('Pinned parent hashes are required')
    hashes=plan.get('input_hashes')
    if (not isinstance(hashes,dict) or set(hashes)!={'prices.parquet','features.parquet','sized-outcomes.parquet'}
            or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in hashes.values())):
        raise ValueError('Three pinned input hashes are required')
    if not isinstance(plan.get('experiment_id'),str) or not plan['experiment_id']:
        raise ValueError('Explicit registered experiment identity is required')
    budget=budget_value(plan.get('budget_rial'))
    contract=plan.get('financial_contract')
    if not isinstance(contract,dict) or contract!=dict(rule=asdict(RULE),fees=dict(settings.FEES),slippage_bps=10,
            participation=.05,initial_capital_rial=budget*5,max_positions=5,loss_floor_exit=True,max_loss_pct=.1,
            entry_day_exit=False,independent_accounts_per_fold=True):
        raise ValueError('Shared-cash execution contract differs')
    training=plan.get('training')
    if (not isinstance(training,dict) or any(type(training.get(k)) is not int or training[k]<3
            for k in ('initial_sessions','test_sessions','embargo_sessions'))
            or any(type(plan.get(k)) is not int or plan[k]<3 for k in ('min_model_sessions','min_model_rows'))):
        raise ValueError('Chronological fold settings or computational minima differ')
    return PortfolioConfig(initial_capital_rial=contract['initial_capital_rial'],horizon=RULE.hold,max_positions=5,
        participation=.05,slippage_bps=10,max_loss_pct=.1)


def _validate_outcomes(outcomes, cutoff):
    required={'date','symbol','state','resolved_at','normal_net','floor_net','normal_entry_filled',
              'stress_entry_filled','normal_entry_date','normal_observed_net','stress_observed_net'}
    if not required<=set(outcomes) or outcomes.duplicated(['date','symbol']).any():
        raise ValueError('Unique, complete outcome contract is required')
    if not outcomes.state.isin(['complete','entry_blocked','unknown']).all():
        raise ValueError('Unknown outcome state')
    known=outcomes.state.ne('unknown')
    if (not outcomes.resolved_at.notna().equals(known) or not outcomes.normal_net.notna().equals(known)
            or not outcomes.floor_net.notna().equals(known)):
        raise ValueError('Unknown joint return must not become a number')
    for column in ('normal_entry_filled','stress_entry_filled'):
        observed=outcomes[column].notna()
        if not outcomes.loc[observed,column].map(lambda v:isinstance(v,(bool,np.bool_))).all():
            raise ValueError('Entry status must be an explicit boolean or unknown')
    resolved=outcomes.loc[known,'resolved_at']
    if (any(not is_canonical_iso_date(d) or d>=cutoff for d in resolved)
            or resolved.le(outcomes.loc[known,'date']).any()):
        raise ValueError('Joint return availability is invalid or sealed')
    for column in ('normal_net','floor_net','normal_observed_net','stress_observed_net'):
        values=_numeric(outcomes.loc[known,column])
        if not np.isfinite(values).all() or values.lt(-1).any():
            raise ValueError('A known modeled return is invalid')
    if (outcomes.loc[known,'floor_net'].gt(outcomes.loc[known,'normal_net']+1e-10).any()
            or not np.allclose(outcomes.loc[known,'floor_net'],np.minimum(
                outcomes.loc[known,'normal_observed_net'],outcomes.loc[known,'stress_observed_net']),rtol=0,atol=1e-12)):
        raise ValueError('Scenario floor must equal the minimum of both outcomes')
    no_entry=outcomes.normal_entry_filled.eq(False).fillna(False)  # noqa: E712
    if (not outcomes.loc[no_entry,'stress_entry_filled'].eq(False).all()  # noqa: E712
            or not outcomes.loc[no_entry,'state'].eq('entry_blocked').all()
            or outcomes.loc[no_entry,['normal_net','floor_net']].ne(0).any().any()):
        raise ValueError('Known normal no-entry must have zero return in both scenarios')
    if (outcomes.loc[known,'normal_entry_filled'].isna().any()
            or outcomes.loc[outcomes.state.eq('complete'),'normal_entry_filled'].eq(False).any()  # noqa: E712
            or not outcomes.loc[outcomes.state.eq('entry_blocked'),'normal_entry_filled'].eq(False).all()):  # noqa: E712
        raise ValueError('Known joint outcomes require coherent entry evidence')
    entry_known=outcomes.normal_entry_filled.notna()
    entry_dates=outcomes.loc[entry_known,'normal_entry_date']
    if (any(not is_canonical_iso_date(d) or d>=cutoff for d in entry_dates)
            or entry_dates.le(outcomes.loc[entry_known,'date']).any()):
        raise ValueError('Known entry date is invalid or sealed')
    filled=known&outcomes.normal_entry_filled.eq(True).fillna(False)  # noqa: E712
    blocked=outcomes.state.eq('entry_blocked')
    if (outcomes.loc[filled,'resolved_at'].le(outcomes.loc[filled,'normal_entry_date']).any()
            or outcomes.loc[blocked,'resolved_at'].ne(outcomes.loc[blocked,'normal_entry_date']).any()):
        raise ValueError('Entry and resolution dates contradict the execution rule')


def _checked_parent(parent,plan):
    parent=Path(parent).resolve()
    if (sha256(parent/'result.json')!=plan['parent_result_sha256']
            or sha256(parent/'review.json')!=plan['parent_review_sha256']):
        raise ValueError('Pinned parent result or review differs')
    result=json.loads((parent/'result.json').read_text(encoding='utf-8'))
    review=json.loads((parent/'review.json').read_text(encoding='utf-8'))
    prior=result['protocol']['pre_code_plan']
    if (prior['experiment_id']!=plan['parent_id'] or prior['training']!=plan['training']
            or prior['budget_rial']!=plan['budget_rial'] or prior['holdout_start']!=plan['holdout_start']
            or result['protocol']['execution_contract']!=execution_contract(plan['budget_rial'])
            or review.get('financial_modes')!=len(result['records'])*25
            or not all(review.get(k) is True for k in ('outcomes_recomputed','models_refitted',
                'forecasts_and_all_picks_recomputed','cash_ledgers_reexecuted'))
            or any(result.get(k) is not False or review.get(k) is not False for k in FALSE_FLAGS)):
        raise ValueError('Prior study was not reviewed under the matching execution contract')
    hashes=result['artifacts_sha256']
    needed=[*plan['input_hashes'],*[f'fold-{i:02d}-sized11-forecast.parquet' for i in range(len(result['records']))]]
    for name in needed:
        path=(parent/name).resolve()
        if not path.is_relative_to(parent) or sha256(path)!=hashes.get(name):
            raise ValueError('Prior study input or baseline forecast hash differs: '+name)
        if name in plan['input_hashes'] and hashes[name]!=plan['input_hashes'][name]:
            raise ValueError('Prior input differs from the preregistered plan')
    return result,review


def _mean_by_date(frame,column):
    if frame.empty:
        return None
    return float(frame.groupby('date')[column].mean().mean())


def prediction_metrics(forecast,baseline,outcomes,*,end_exclusive,training_rate):
    """Compare the same rows; later/unresolved outcomes stay in the denominator."""
    if not is_canonical_iso_date(end_exclusive):
        raise ValueError('A canonical fold end is required')
    for frame in (forecast,baseline,outcomes):
        _keys(frame)
    if any(frame.duplicated(['date','symbol']).any() for frame in (forecast,baseline,outcomes)):
        raise ValueError('Forecast and outcome identities must be unique')
    for frame,columns in ((forecast,('normal_expected_net','scenario_floor_expected_net','proxy_entry_probability')),
                          (baseline,('normal_expected_net','scenario_floor_expected_net'))):
        if not {'status',*columns}<=set(frame):
            raise ValueError('Forecast columns are incomplete')
        predicted=frame.status.eq('forecasted')
        for name in columns:
            numbers=_numeric(frame[name])
            if not numbers.notna().equals(predicted) or not np.isfinite(numbers.loc[predicted]).all():
                raise ValueError('Forecast status and numeric values differ')
        if (frame.loc[predicted,'scenario_floor_expected_net'].gt(frame.loc[predicted,'normal_expected_net']+1e-10).any()
                or frame.loc[predicted,['normal_expected_net','scenario_floor_expected_net']].lt(-1).any().any()
                or ('proxy_entry_probability' in columns and not frame.loc[predicted,'proxy_entry_probability'].between(0,1).all())):
            raise ValueError('Forecast values violate the target or probability contract')
    old=baseline[['date','symbol','status','normal_expected_net','scenario_floor_expected_net']].rename(columns={
        'status':'baseline_status','normal_expected_net':'baseline_normal','scenario_floor_expected_net':'baseline_floor'})
    rows=forecast.merge(old,on=['date','symbol'],how='left',validate='one_to_one',indicator=True)
    if rows._merge.ne('both').any() or len(rows)!=len(baseline):
        raise ValueError('A baseline forecast is missing')
    rows=rows.drop(columns='_merge').merge(outcomes,on=['date','symbol'],how='left',validate='one_to_one',indicator=True)
    if rows._merge.ne('both').any() or len(rows)!=len(outcomes):
        raise ValueError('A forecast outcome is missing')
    predicted=rows.status.eq('forecasted')
    baseline_predicted=rows.baseline_status.eq('forecasted')
    known_entry=rows.normal_entry_filled.notna()&rows.normal_entry_date.fillna('').lt(end_exclusive)
    known_result=rows.state.ne('unknown')&rows.resolved_at.fillna('').lt(end_exclusive)
    result=dict(rows=len(rows),forecasted=int(predicted.sum()),known_entry=int(known_entry.sum()),
        unknown_or_late_entry=int((~known_entry).sum()),known_joint_return_by_fold_end=int(known_result.sum()),
        unknown_or_late_joint_return=int((~known_result).sum()),entry_probability_is_proxy=True,
        probabilities_calibrated=False,statistical_significance_tested=False)
    entry=rows.loc[predicted&known_entry].copy()
    if not entry.empty:
        if training_rate is None:
            raise ValueError('A forecasted entry model needs a training-rate baseline')
        y=entry.normal_entry_filled.astype(float)
        entry['error']=(entry.proxy_entry_probability-y)**2
        entry['baseline_error']=(training_rate-y)**2
    result['entry_brier']=dict(paired_rows=len(entry),paired_dates=int(entry.date.nunique()),
        mean_squared_error=float(entry.error.mean()) if len(entry) else None,
        equal_date_mse=_mean_by_date(entry,'error'),
        baseline_mean_squared_error=float(entry.baseline_error.mean()) if len(entry) else None,
        baseline_equal_date_mse=_mean_by_date(entry,'baseline_error'))
    comparison=rows.loc[predicted&baseline_predicted&known_result].copy()
    result['returns']={}
    for label,pred,old_pred,actual in (('normal','normal_expected_net','baseline_normal','normal_net'),
            ('scenario_floor','scenario_floor_expected_net','baseline_floor','floor_net')):
        comparison['new_error']=(comparison[pred]-comparison[actual])**2
        comparison['old_error']=(comparison[old_pred]-comparison[actual])**2
        result['returns'][label]=dict(paired_rows=len(comparison),paired_dates=int(comparison.date.nunique()),
            new_mean_squared_error=float(comparison.new_error.mean()) if len(comparison) else None,
            old_mean_squared_error=float(comparison.old_error.mean()) if len(comparison) else None,
            new_equal_date_mse=_mean_by_date(comparison,'new_error'),old_equal_date_mse=_mean_by_date(comparison,'old_error'))
    return result


def _frame(folder,name,frame,*,save):
    frame=_empty_column_axis(frame.reset_index(drop=True))
    path=folder/name
    if save:
        frame.to_parquet(path,index=False)
    else:
        pd.testing.assert_frame_equal(frame,pd.read_parquet(path),check_exact=True)


def _model(folder,name,model,*,save):
    path=folder/name
    if save:
        _dump(path,model)
    elif _json_safe(model)!=json.loads(path.read_text(encoding='utf-8')):
        raise ValueError('Recomputed model differs from archive')


def _evaluate(folder,protocol,*,save):
    plan=protocol['pre_code_plan']
    config=validate_plan(plan)
    parent=json.loads((folder/'parent-result.json').read_text(encoding='utf-8'))
    prior=parent['protocol']['pre_code_plan']
    prices,features,outcomes=[read_development_parquet(folder/name,plan['input_hashes'][name],plan['holdout_start'])
        for name in ('prices.parquet','features.parquet','sized-outcomes.parquet')]
    _validate_outcomes(outcomes,plan['holdout_start'])
    outcomes.attrs.update(execution_contract(plan['budget_rial']))
    panel=_panel(prices,features,prior,protocol['min_daily_value_rial'])
    calendar=market_calendar(prices)
    prepared=prepare_portfolio_prices(prices)
    folds=_folds(calendar,prior)
    if len(folds)!=len(parent['records']):
        raise ValueError('Parent chronological folds differ')
    records=[]
    columns=['date','symbol',*PREDICTORS,'value']
    for index,fold in enumerate(folds):
        if parent['records'][index]['index']!=index or parent['records'][index]['fold']!=asdict(fold):
            raise ValueError('Fold identity differs from the frozen parent')
        train=panel.loc[panel.date.ge(fold.train_start)&panel.date.lt(fold.train_end_exclusive)].reset_index(drop=True)
        test=panel.loc[panel.date.ge(fold.test_start)&panel.date.lt(fold.test_end_exclusive)].reset_index(drop=True)
        targets=outcomes.loc[outcomes.date.ge(fold.train_start)&outcomes.date.lt(fold.train_end_exclusive)].copy()
        targets.attrs.update(execution_contract(plan['budget_rial']))
        model=fit_two_stage(train[columns],targets,train_end_exclusive=fold.train_end_exclusive,
            budget_rial=plan['budget_rial'],min_rows=plan['min_model_rows'],min_sessions=plan['min_model_sessions'])
        forecast=forecast_two_stage(test[columns],model)
        days=calendar[(calendar>=fold.test_start)&(calendar<fold.test_end_exclusive)]
        if len(days)<3:
            raise ValueError('A decision and two following sessions are required')
        end=days[-2]
        picks=_conditional(forecast,end)
        jobs={'two-stage':picks}
        for seed in SEEDS:
            jobs[f'random{seed}']=_random(test.loc[test.date.lt(end)],picks,5,seed+index)
        # Freeze all forecasts and picks before reading test returns or producing ledgers.
        _model(folder,f'fold-{index:02d}-model.json',model,save=save)
        _frame(folder,f'fold-{index:02d}-forecast.parquet',forecast,save=save)
        for name,selected in jobs.items():
            _frame(folder,f'fold-{index:02d}-{name}-picks.parquet',selected,save=save)
        baseline=read_development_parquet(folder/f'baseline-{index:02d}-sized11-forecast.parquet',
            parent['artifacts_sha256'][f'fold-{index:02d}-sized11-forecast.parquet'],plan['holdout_start'])
        test_decisions=forecast.loc[forecast.date.lt(end)].reset_index(drop=True)
        baseline=baseline.loc[baseline.date.lt(end)].reset_index(drop=True)
        test_outcomes=outcomes.loc[outcomes.date.ge(fold.test_start)&outcomes.date.lt(end)].reset_index(drop=True)
        prediction=prediction_metrics(test_decisions,baseline,test_outcomes,end_exclusive=fold.test_end_exclusive,
                                      training_rate=model['fill_model']['training_rate'])
        record=dict(index=index,fold=asdict(fold),training_coverage={
            'entry':{k:model['fill_model'][k] for k in ('status','training_rows','known_entries','unknown_entries',
                'fitted_rows','fitted_sessions','max_entry_date','training_rate')},
            'known_filled_entries':model['known_filled_entries'],
            'conditional_outcomes_known':model['conditional_outcomes_known'],
            'filled_with_unresolved_return':model['filled_with_unresolved_return'],
            'conditional_status':model['conditional_return_model']['status']},prediction=prediction,modes={},
            prior_comparisons={n:parent['records'][index]['modes'][n] for n in ('sized10','sized11','idle-cash')})
        for name in modes():
            selected=jobs[name.removesuffix('-stress')]
            replay=_replay(prepared,selected,calendar,config,fold,name)
            for kind in ('equity','executions','decisions'):
                _frame(folder,f'fold-{index:02d}-{name}-{kind}.parquet',getattr(replay,kind),save=save)
            record['modes'][name]=_financial_record(replay,selected,outcomes,days)
        records.append(_json_safe(record))
        if save:
            print(json.dumps(dict(fold=index+1,total_folds=len(folds),picks=len(picks),
                entry_model=model['fill_model']['status'],conditional_model=model['conditional_return_model']['status'])),flush=True)
    return records


def _summary(records):
    result={}
    for name in modes():
        rows=[r['modes'][name] for r in records]
        known=[r['metrics']['total_return'] for r in rows if r['metrics']['total_return'] is not None]
        result[name]=dict(folds=len(rows),known_proxy_return_folds=len(known),
            mean_known_proxy_fold_return=float(np.mean(known)) if known else None,
            positive_proxy_folds=sum(v>0 for v in known),mean_conditions_on_known_folds_only=True,
            selections=sum(r['selections'] for r in rows),buy_executions=sum(r['buy_executions'] for r in rows),
            unknown_selected_outcomes=sum(r['unknown_selected_outcomes'] for r in rows),
            selected_outcomes_resolved_after_fold_end=sum(r['selected_outcomes_resolved_after_fold_end'] for r in rows),
            local_economic_contract_complete_folds=sum(r['economic_contract_complete'] for r in rows),
            corporate_action_events=sum(r['metrics']['corporate_action_events'] for r in rows),
            stale_mark_days=sum(r['metrics']['stale_mark_days'] for r in rows),
            open_position_folds=sum(r['metrics']['open_positions']>0 for r in rows))
    return result


def _register(db,identifier,protocol,metrics,status):
    result=db.log_experiment(experiment_id=identifier,strategy_name=VERSION,
        symbols_tested='frozen previously observed pre-seal public universe',
        parameters_json=json.dumps(_json_safe(protocol),ensure_ascii=False,allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics),ensure_ascii=False,allow_nan=False),status=status,
        dataset_version=protocol['pre_code_plan']['input_hashes']['prices.parquet'],model_version=VERSION)
    if result is None:
        raise RuntimeError('Two-stage experiment registration failed')
    _registry(db,identifier,protocol,metrics,status)


def run_study(registry,parent,*,plan,root):
    config=validate_plan(plan)
    _registry(registry,plan['experiment_id']+'-PLAN',plan,{},'registered')
    parent=Path(parent).resolve()
    previous,_=_checked_parent(parent,plan)
    root=Path(root).resolve()
    from ops.report_files import private_report_path

    private_report_path(root/'result.json',forbidden_paths=(settings.DB_PATH,))
    root.mkdir(parents=True,exist_ok=False)
    code_hash=research_code_fingerprint()
    source=archive_research_code(root/'code')
    protocol=dict(version=VERSION,registered_at=now_tehran_iso(),pre_code_plan=plan,code_sha256=code_hash,
        source_files=source,config=asdict(config),min_daily_value_rial=previous['protocol']['min_daily_value_rial'],
        independent_accounts_per_fold=True,local_economic_contract_only=True,**dict.fromkeys(FALSE_FLAGS,False))
    _dump(root/'protocol.json',protocol)
    _register(registry,plan['experiment_id']+'-PROTOCOL',protocol,{},'registered')
    for name in plan['input_hashes']:
        shutil.copyfile(parent/name,root/name)
    shutil.copyfile(parent/'result.json',root/'parent-result.json')
    shutil.copyfile(parent/'review.json',root/'parent-review.json')
    if (sha256(root/'parent-result.json')!=plan['parent_result_sha256']
            or sha256(root/'parent-review.json')!=plan['parent_review_sha256']):
        raise ValueError('Parent evidence changed while being copied')
    for index in range(len(previous['records'])):
        shutil.copyfile(parent/f'fold-{index:02d}-sized11-forecast.parquet',root/f'baseline-{index:02d}-sized11-forecast.parquet')
    records=_evaluate(root,protocol,save=True)
    if research_code_fingerprint()!=code_hash:
        raise ValueError('Research source changed during the study')
    result=dict(protocol=protocol,completed_at=now_tehran_iso(),records=records,summary=_summary(records),
        parent_summary={k:previous['summary'][k] for k in ('sized10','sized10-stress','sized11','sized11-stress','idle-cash')},
        **dict.fromkeys(FALSE_FLAGS,False))
    result['artifacts_sha256']={p.name:sha256(p) for p in root.iterdir() if p.is_file()}
    _dump(root/'result.json',result)
    _register(registry,plan['experiment_id'],protocol,result['summary'],'completed')
    return result


def review_study(registry,folder):
    folder=Path(folder).resolve()
    result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
    protocol=result['protocol']
    plan=protocol['pre_code_plan']
    validate_plan(plan)
    if (protocol['version']!=VERSION or protocol['code_sha256']!=research_code_fingerprint()
            or protocol['config']!=asdict(validate_plan(plan)) or any(result.get(k) is not False for k in FALSE_FLAGS)
            or sha256(folder/'parent-result.json')!=plan['parent_result_sha256']
            or sha256(folder/'parent-review.json')!=plan['parent_review_sha256']):
        raise ValueError('Current review code, protocol or parent evidence differs')
    _registry(registry,plan['experiment_id']+'-PLAN',plan,{},'registered')
    _registry(registry,plan['experiment_id']+'-PROTOCOL',protocol,{},'registered')
    _registry(registry,plan['experiment_id'],protocol,result['summary'],'completed')
    if protocol!=json.loads((folder/'protocol.json').read_text(encoding='utf-8')):
        raise ValueError('Archived protocol differs')
    actual={p.name for p in folder.iterdir() if p.is_file() and p.name not in ('result.json','review.json')}
    if actual!=set(result['artifacts_sha256']):
        raise ValueError('Archived study artifact inventory differs')
    for base,hashes in ((folder/'code',protocol['source_files']),(folder,result['artifacts_sha256'])):
        for name,digest in hashes.items():
            path=(base/name).resolve()
            if not path.is_relative_to(base) or sha256(path)!=digest:
                raise ValueError('A bound source or output artifact differs')
    prices,features,outcomes=[read_development_parquet(folder/name,plan['input_hashes'][name],plan['holdout_start'])
        for name in ('prices.parquet','features.parquet','sized-outcomes.parquet')]
    prior=json.loads((folder/'parent-result.json').read_text(encoding='utf-8'))['protocol']['pre_code_plan']
    panel=_panel(prices,features,prior,protocol['min_daily_value_rial'])
    calendar=market_calendar(prices)
    recalculated=sized_dip_attempt_outcomes(prepare_portfolio_prices(prices),panel[['date','symbol']],calendar,
        budget_rial=plan['budget_rial'],fees=plan['financial_contract']['fees'],participation=.05,
        slippage_bps=10,end_exclusive=plan['holdout_start'])
    pd.testing.assert_frame_equal(recalculated,outcomes,check_exact=True)
    records=_evaluate(folder,protocol,save=False)
    parent_summary=json.loads((folder/'parent-result.json').read_text(encoding='utf-8'))['summary']
    if (records!=result['records'] or _summary(records)!=result['summary']
            or result['parent_summary']!={k:parent_summary[k] for k in result['parent_summary']}):
        raise ValueError('A model, metric or financial replay differs')
    if protocol['code_sha256']!=research_code_fingerprint():
        raise ValueError('Research source changed during review')
    return dict(verified_at=now_tehran_iso(),folds=len(records),financial_modes=len(records)*len(modes()),
        outcomes_recomputed=True,models_refitted=True,forecasts_and_picks_recomputed=True,
        cash_ledgers_reexecuted=True,**dict.fromkeys(FALSE_FLAGS,False))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--registry',type=Path,required=True)
    parser.add_argument('--parent',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    plan=json.loads(args.plan.read_text(encoding='utf-8'))
    with DatabaseManager(args.registry) as registry:
        result=run_study(registry,args.parent,plan=plan,root=args.output)
        review=review_study(registry,args.output)
        _dump(args.output/'review.json',review)
    print(json.dumps(dict(folds=len(result['records']),summary=result['summary'],review=review),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
