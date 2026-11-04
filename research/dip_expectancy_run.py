"""Registered retrospective DIP research with matched targets and shared cash; never live signals."""

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
from core.timeutils import now_tehran_iso
from ops.research_review import read_development_parquet, sha256
from research.baselines import STRATEGIES, decision_score_inputs, eligible_strategy_score, pick_top
from research.dip_expectancy import fit_dip_model, forecast_dip
from research.dip_outcomes import RULE, dip_attempt_outcomes, execution_contract
from research.expectancy import PREDICTORS
from research.expectancy_run import _random, _rank
from research.labels import market_calendar
from research.portfolio_run import BASELINE_SELECTION_VERSION, select_from_past, verified_baseline_inputs
from research.run import _compact_metrics, _json_safe, archive_research_code, research_code_fingerprint
from research.surge_portfolio_run import _registry
from research.walkforward import time_folds

VERSION='execution-aligned-dip-study-v1'
DIP_BASELINE_RESOLUTION_VERSION='paired-dip-floor-resolution-v1'
SEEDS=(41,97,173)


def modes():
    return ['conditional','conditional-stress',
        *[f'conditional-random{seed}{suffix}' for seed in SEEDS for suffix in ('','-stress')],
        'past-dip-baseline','past-dip-baseline-stress','buyer-power','buyer-power-stress',
        'liquidity','liquidity-stress','idle-cash']


def _dump(path,value):
    path.write_text(json.dumps(_json_safe(value),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def _unknown_as_nan(value):
    """Restore JSON null to numerical unknown for the original in-memory metrics reducer."""
    if isinstance(value,dict):
        return {k:_unknown_as_nan(v) for k,v in value.items()}
    if isinstance(value,list):
        return [_unknown_as_nan(v) for v in value]
    return np.nan if value is None else value


def _empty_column_axis(frame):
    """Parquet cannot retain an empty RangeIndex column axis; no financial cells change."""
    if not len(frame.columns):
        frame=frame.copy()
        frame.columns=pd.Index([],dtype=object)
    return frame


def validate_plan(plan):
    if (not isinstance(plan,dict) or plan.get('version')!='execution-aligned-dip-pre-code-v1'
            or plan.get('rule')!=asdict(RULE) or plan.get('predictors')!=list(PREDICTORS)
            or plan.get('modes')!=modes() or plan.get('fees')!=dict(settings.FEES)
            or plan.get('entry_day_exit') is not False or plan.get('holdout_opened') is not False
            or plan.get('accepted_for_signals') is not False or plan.get('parameter_search') is not False
            or plan.get('feature_search') is not False or plan.get('ridge_penalty')!=.01
            or plan.get('holdout_start')!=settings.RESEARCH_CONFIG['holdout_start']
            or plan.get('development_start')!=settings.RESEARCH_CONFIG['dev_start']
            or plan.get('max_positions')!=5 or plan.get('participation')!=.05
            or plan.get('loss_floor_exit') is not True or plan.get('entry_allocation_fraction')!=1.0
            or plan.get('random_seeds')!=list(SEEDS)):
        raise ValueError('The pre-registered fixed DIP plan is incompatible')
    for field in ('initial_sessions','test_sessions','embargo_sessions'):
        if type(plan['training'].get(field)) is not int or plan['training'][field]<3:
            raise ValueError('Explicit supported chronological fold settings are required')
    for field in ('min_model_sessions','min_model_rows'):
        if type(plan.get(field)) is not int or plan[field]<3:
            raise ValueError('Explicit computational sample minima are required')
    # PortfolioConfig supplies the shared finite/capital/risk/slippage checks.
    return PortfolioConfig(initial_capital_rial=plan['initial_capital_rial'],horizon=RULE.hold,
        max_positions=plan['max_positions'],participation=plan['participation'],
        slippage_bps=plan['slippage_bps'],max_loss_pct=plan['max_loss_pct'])


def _training(outcomes,boundary):
    result=outcomes.copy()
    known=result.resolved_at.notna() & result.resolved_at.fillna('').lt(boundary)
    result.loc[~known,['normal_net','floor_net']]=np.nan
    result.loc[~known,'resolved_at']=None
    result.loc[~known,'state']='unknown'
    return result


def _past_choice(train,targets,fold,calendar):
    # Reuse the audited rank-before-purge selector; the declared target is the
    # per-attempt floor, not a VWAP label or the actual stress return.
    target=targets[['date','symbol','state','resolved_at','floor_net']].copy()
    blocked = target.state.eq('entry_blocked')
    if (blocked & ~target.floor_net.eq(0)).any():
        raise ValueError('A known blocked paired attempt must have zero scenario-floor return')
    target['label_status_1d']=target.state
    target['net_ret_1d']=target.floor_net.where(target.state.eq('complete'))
    panel=train.merge(target.drop(columns=['state','floor_net']),on=['date','symbol'],validate='one_to_one')
    return select_from_past(panel,fold,(1,),5,calendar=calendar,outcome_mode='paired_dip_floor')


def fold_candidates(train,test,outcomes,calendar,fold,config,index,plan):
    targets=_training(outcomes.loc[outcomes.date.ge(fold.train_start)&outcomes.date.lt(fold.train_end_exclusive)],
        fold.train_end_exclusive)
    xcols=['date','symbol',*PREDICTORS]
    model=fit_dip_model(train[xcols],targets,train_end_exclusive=fold.train_end_exclusive,
        min_sessions=plan['min_model_sessions'],min_rows=plan['min_model_rows'])
    forecast=forecast_dip(test[xcols],model)
    days=calendar[(calendar>=fold.test_start)&(calendar<fold.test_end_exclusive)]
    # Decision -> entry -> next-session exit needs two remaining sessions.
    end=days[-2]
    gate=(forecast.status.eq('forecasted')&forecast.normal_expected_net.gt(0)
        &forecast.scenario_floor_expected_net.gt(0)&forecast.date.lt(end))
    conditional=_rank(forecast,forecast.scenario_floor_expected_net,gate,5)
    pool=test.loc[test.date.lt(end)]
    best,selection_diagnostics=_past_choice(train,targets,fold,calendar)
    picks={'conditional':conditional,'past-dip-baseline':
        (pick_top(pool,eligible_strategy_score(decision_score_inputs(pool),
                  next(s for s in STRATEGIES if s.name==best['strategy'])),5)[['date','symbol']]
            if best else pool.iloc[:0][['date','symbol']]),
        'buyer-power':pick_top(pool,pool.buyer_power,5)[['date','symbol']],
        'liquidity':pick_top(pool,pool.value,5)[['date','symbol']],
        'idle-cash':pool.iloc[:0][['date','symbol']]}
    for seed in SEEDS:
        picks[f'conditional-random{seed}']=_random(pool,conditional,5,seed+index)
    jobs={name:picks[name.removesuffix('-stress')].reset_index(drop=True) for name in modes()}
    return model,forecast,jobs,days,best,selection_diagnostics


def _summary(records):
    result={}
    for name in modes():
        cases=[r['modes'][name] for r in records]
        known=[r['metrics']['total_return'] for r in cases if r['metrics']['total_return'] is not None]
        result[name]=dict(folds=len(cases),known_proxy_return_folds=len(known),
            positive_proxy_folds=sum(v>0 for v in known),
            mean_known_proxy_fold_return=float(np.mean(known)) if known else None,
            mean_conditions_on_known_folds_only=True,
            unknown_selected_outcomes=sum(r['unknown_selected_outcomes'] for r in cases),
            corporate_action_events=sum(r['metrics']['corporate_action_events'] for r in cases),
            stale_mark_days=sum(r['metrics']['stale_mark_days'] for r in cases),
            open_position_folds=sum(r['metrics']['open_positions']>0 for r in cases),
            economic_contract_complete_folds=sum(r['economic_contract_complete'] for r in cases),
            buy_executions=sum(r['buy_executions'] for r in cases),
            max_known_proxy_drawdown=max((r['metrics']['max_drawdown'] for r in cases
                if r['metrics']['max_drawdown'] is not None),default=None))
    return result


def run_dip_study(registry,source_db,*,plan,root):
    config=validate_plan(plan)
    _registry(registry,plan['experiment_id']+'-PLAN',plan,{},'registered')
    source_root=settings.DATA_DIR/'research'
    source_file=source_root/(plan['source_id']+'.json')
    if sha256(source_file)!=plan['source_report_sha256']:
        raise ValueError('Pinned source report differs')
    source=json.loads(source_file.read_text(encoding='utf-8'))
    _registry(source_db,plan['source_id'],source['params'],_compact_metrics(_unknown_as_nan(source['results'])),'completed')
    _,inputs,digests=verified_baseline_inputs(source_root,plan['source_id'])
    if digests!=plan['source_inputs_sha256']:
        raise ValueError('Pinned source inputs differ')
    root=Path(root).resolve()
    from ops.report_files import private_report_path

    private_report_path(root/'result.json',forbidden_paths=(settings.DB_PATH,))
    root.mkdir(parents=True,exist_ok=False)
    source_hash=research_code_fingerprint()
    code=archive_research_code(root/'code')
    protocol=dict(version=VERSION,registered_at=now_tehran_iso(),pre_code_plan=plan,
        baseline_selection_version=BASELINE_SELECTION_VERSION,
        baseline_resolution_version=DIP_BASELINE_RESOLUTION_VERSION,
        code_sha256=source_hash,source_files=code,execution_contract=execution_contract(),
        min_daily_value_rial=source['params']['min_daily_value_rial'],
        empty_column_serialization='object-index-v1',
        config=asdict(config),last_test_decision_sessions_skipped=2,
        independent_accounts_per_fold=True,accepted_for_signals=False,holdout_opened=False)
    _dump(root/'protocol.json',protocol)
    def register(suffix,metrics,status):
        registered=registry.log_experiment(experiment_id=plan['experiment_id']+suffix,strategy_name=VERSION,
            symbols_tested='all frozen liquid development rows',parameters_json=json.dumps(protocol,ensure_ascii=False),
            metrics_json=json.dumps(_json_safe(metrics),ensure_ascii=False,allow_nan=False),status=status,
            dataset_version=plan['source_report_sha256'])
        if registered is None:
            raise RuntimeError('Independent DIP registration failed')
        _registry(registry,plan['experiment_id']+suffix,protocol,metrics,status)
    register('-PROTOCOL',{},'registered')
    for name in ('prices','features'):
        shutil.copyfile(source_root/plan['source_id']/(name+'.parquet'),root/(name+'.parquet'))
    prices=inputs['prices']
    panel=inputs['features'].merge(prices[['date','symbol','value']],on=['date','symbol'],validate='one_to_one')
    panel=panel.loc[panel.date.ge(plan['development_start'])&panel.date.lt(plan['holdout_start'])
        &panel.value.ge(source['params']['min_daily_value_rial'])].reset_index(drop=True)
    calendar=market_calendar(prices)
    prepared=prepare_portfolio_prices(prices)
    outcomes=dip_attempt_outcomes(prepared,panel[['date','symbol']],calendar,fees=plan['fees'],
        participation=plan['participation'],slippage_bps=plan['slippage_bps'],end_exclusive=plan['holdout_start'])
    outcomes.to_parquet(root/'outcomes.parquet',index=False)
    _dump(root/'price-preparation.json',dict(version=prepared.version,rows=prepared.row_count,
        frame_fingerprint=prepared.frame_fingerprint))
    training=plan['training']
    folds=time_folds(calendar[calendar>=plan['development_start']],min_train_sessions=training['initial_sessions'],
        test_sessions=training['test_sessions'],embargo_sessions=training['embargo_sessions'],
        end_exclusive=plan['holdout_start'])
    if not folds:
        raise ValueError('No chronological research folds')
    records=[]
    for index,fold in enumerate(folds):
        train=panel.loc[panel.date.ge(fold.train_start)&panel.date.lt(fold.train_end_exclusive)].reset_index(drop=True)
        test=panel.loc[panel.date.ge(fold.test_start)&panel.date.lt(fold.test_end_exclusive)].reset_index(drop=True)
        model,forecast,jobs,days,best,diagnostics=fold_candidates(train,test,outcomes,calendar,fold,config,index,plan)
        _dump(root/f'fold-{index:02d}-model.json',model)
        forecast.to_parquet(root/f'fold-{index:02d}-forecast.parquet',index=False)
        # Freeze every mode's selections before evaluating any of this fold's returns.
        for name,picks in jobs.items():
            picks.to_parquet(root/f'fold-{index:02d}-{name}-picks.parquet',index=False)
        record=dict(fold=asdict(fold),index=index,best_past=best,past_selection=diagnostics,modes={})
        for name,picks in jobs.items():
            replay=replay_portfolio(prepared,picks,calendar,replace(config,stress=name.endswith('-stress')),
                start=fold.test_start,end=fold.test_end_exclusive,dip_rule=RULE,
                entry_allocation_fraction=1.0,loss_floor_exit=True)
            for kind in ('equity','executions','decisions'):
                _empty_column_axis(getattr(replay,kind)).to_parquet(root/f'fold-{index:02d}-{name}-{kind}.parquet',index=False)
            selected=picks.merge(outcomes[['date','symbol','state']],on=['date','symbol'],how='left',validate='one_to_one')
            if selected.state.isna().any():
                raise ValueError('Selected outcome row missing from the fixed denominator')
            unknown=int(selected.state.eq('unknown').sum())
            m=replay.metrics
            economic=bool(m['status']=='complete' and not unknown and not m['corporate_action_events']
                and not m['stale_mark_days'] and not m['open_positions']
                and replay.equity.date.tolist()==list(days))
            record['modes'][name]=dict(metrics=m,assumptions=replay.assumptions,selections=len(picks),
                unknown_selected_outcomes=unknown,economic_contract_complete=economic,
                buy_executions=int(replay.executions.side.eq('buy').sum()) if len(replay.executions) else 0)
        records.append(record)
        print(json.dumps(dict(fold=index+1,total_folds=len(folds),conditional_picks=len(jobs['conditional']),
            model_status=model['status']),ensure_ascii=False),flush=True)
    if source_hash!=research_code_fingerprint():
        raise ValueError('Research source changed during the run')
    summary=_summary(records)
    result=dict(protocol=protocol,completed_at=now_tehran_iso(),records=records,summary=summary,
        outcome_coverage=outcomes.state.value_counts().to_dict(),outcome_rows=len(outcomes),
        actual_stress_above_normal_pairs=int((outcomes.stress_observed_net>outcomes.normal_observed_net+1e-10).sum()),
        accepted_for_signals=False,economic_profit_verified=False,holdout_opened=False,
        statistical_superiority_claim=False,unseen_test=False)
    result['artifacts_sha256']={p.name:sha256(p) for p in root.iterdir() if p.is_file()}
    _dump(root/'result.json',result)
    register('',summary,'completed')
    return result


def review_dip_study(registry,folder):
    """Offline refit and financial re-execution of every mode, including losing/unknown ones."""
    folder=Path(folder).resolve()
    from ops.report_files import private_report_path

    private_report_path(folder/'result.json',forbidden_paths=(settings.DB_PATH,))
    result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
    protocol=result['protocol']
    plan=protocol['pre_code_plan']
    config=validate_plan(plan)
    if (protocol['version']!=VERSION or protocol['execution_contract']!=execution_contract()
            or protocol['code_sha256']!=research_code_fingerprint()
            or protocol.get('baseline_selection_version')!=BASELINE_SELECTION_VERSION
            or protocol.get('baseline_resolution_version')!=DIP_BASELINE_RESOLUTION_VERSION
            or protocol['config']!=asdict(config) or protocol['last_test_decision_sessions_skipped']!=2
            or protocol.get('empty_column_serialization')!='object-index-v1'
            or any(result.get(k) is not False for k in
                ('accepted_for_signals','economic_profit_verified','holdout_opened','statistical_superiority_claim','unseen_test'))):
        raise ValueError('Archived DIP execution/model contract differs')
    _registry(registry,plan['experiment_id']+'-PLAN',plan,{},'registered')
    _registry(registry,plan['experiment_id']+'-PROTOCOL',protocol,{},'registered')
    _registry(registry,plan['experiment_id'],protocol,result['summary'],'completed')
    if protocol!=json.loads((folder/'protocol.json').read_text(encoding='utf-8')):
        raise ValueError('DIP protocol copy differs')
    for name,digest in protocol['source_files'].items():
        path=(folder/'code'/name).resolve()
        if not path.is_relative_to(folder/'code') or sha256(path)!=digest:
            raise ValueError('Archived research source differs')
    for name,digest in result['artifacts_sha256'].items():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder) or sha256(path)!=digest:
            raise ValueError('DIP source artifact differs')
    cutoff=plan['holdout_start']
    prices=read_development_parquet(folder/'prices.parquet',plan['source_inputs_sha256']['prices'],cutoff)
    features=read_development_parquet(folder/'features.parquet',plan['source_inputs_sha256']['features'],cutoff)
    panel=features.merge(prices[['date','symbol','value']],on=['date','symbol'],validate='one_to_one')
    panel=panel.loc[panel.date.ge(plan['development_start'])&panel.date.lt(cutoff)
        &panel.value.ge(protocol['min_daily_value_rial'])].reset_index(drop=True)
    calendar=market_calendar(prices)
    prepared=prepare_portfolio_prices(prices)
    prepared_meta=json.loads((folder/'price-preparation.json').read_text(encoding='utf-8'))
    if prepared_meta!=dict(version=prepared.version,rows=prepared.row_count,frame_fingerprint=prepared.frame_fingerprint):
        raise ValueError('Prepared raw-price view differs')
    outcomes=dip_attempt_outcomes(prepared,panel[['date','symbol']],calendar,fees=plan['fees'],
        participation=plan['participation'],slippage_bps=plan['slippage_bps'],end_exclusive=cutoff)
    pd.testing.assert_frame_equal(outcomes,pd.read_parquet(folder/'outcomes.parquet'),check_exact=True)
    training=plan['training']
    folds=time_folds(calendar[calendar>=plan['development_start']],min_train_sessions=training['initial_sessions'],
        test_sessions=training['test_sessions'],embargo_sessions=training['embargo_sessions'],end_exclusive=cutoff)
    if len(folds)!=len(result['records']):
        raise ValueError('DIP chronological fold denominator differs')
    reviewed_modes=0
    for index,(fold,record) in enumerate(zip(folds,result['records'],strict=True)):
        if record['index']!=index or record['fold']!=asdict(fold) or set(record['modes'])!=set(modes()):
            raise ValueError('DIP fold identity/mode denominator differs')
        train=panel.loc[panel.date.ge(fold.train_start)&panel.date.lt(fold.train_end_exclusive)].reset_index(drop=True)
        test=panel.loc[panel.date.ge(fold.test_start)&panel.date.lt(fold.test_end_exclusive)].reset_index(drop=True)
        model,forecast,jobs,days,best,diagnostics=fold_candidates(train,test,outcomes,calendar,fold,config,index,plan)
        if (_json_safe(model)!=json.loads((folder/f'fold-{index:02d}-model.json').read_text(encoding='utf-8'))
                or _json_safe(best)!=record['best_past'] or _json_safe(diagnostics)!=record['past_selection']):
            raise ValueError('DIP training or past-only baseline choice differs')
        pd.testing.assert_frame_equal(forecast,pd.read_parquet(folder/f'fold-{index:02d}-forecast.parquet'),check_exact=True)
        for name,picks in jobs.items():
            pd.testing.assert_frame_equal(picks,pd.read_parquet(folder/f'fold-{index:02d}-{name}-picks.parquet'),check_exact=True)
            replay=replay_portfolio(prepared,picks,calendar,replace(config,stress=name.endswith('-stress')),
                start=fold.test_start,end=fold.test_end_exclusive,dip_rule=RULE,
                entry_allocation_fraction=1.0,loss_floor_exit=True)
            for kind in ('equity','executions','decisions'):
                pd.testing.assert_frame_equal(_empty_column_axis(getattr(replay,kind)),
                    pd.read_parquet(folder/f'fold-{index:02d}-{name}-{kind}.parquet'),check_exact=True)
            selected=picks.merge(outcomes[['date','symbol','state']],on=['date','symbol'],how='left',validate='one_to_one')
            if selected.state.isna().any():
                raise ValueError('An archived selection outcome disappeared')
            unknown=int(selected.state.eq('unknown').sum())
            m=replay.metrics
            economic=bool(m['status']=='complete' and not unknown and not m['corporate_action_events']
                and not m['stale_mark_days'] and not m['open_positions'] and replay.equity.date.tolist()==list(days))
            expected=dict(metrics=m,assumptions=replay.assumptions,selections=len(picks),
                unknown_selected_outcomes=unknown,economic_contract_complete=economic,
                buy_executions=int(replay.executions.side.eq('buy').sum()) if len(replay.executions) else 0)
            if _json_safe(expected)!=record['modes'][name]:
                raise ValueError('DIP financial metrics or evidence coverage differs')
            reviewed_modes+=1
    if (_summary(result['records'])!=result['summary'] or result['outcome_rows']!=len(outcomes)
            or result['outcome_coverage']!=outcomes.state.value_counts().to_dict()
            or result['actual_stress_above_normal_pairs']!=int(
                (outcomes.stress_observed_net>outcomes.normal_observed_net+1e-10).sum())):
        raise ValueError('DIP summary or target denominator differs')
    return dict(verified_at=now_tehran_iso(),outcome_rows=len(outcomes),folds=len(folds),financial_modes=reviewed_modes,
        outcomes_recomputed=True,models_refitted=True,forecasts_and_all_picks_recomputed=True,
        cash_ledgers_reexecuted=True,accepted_for_signals=False,economic_profit_verified=False,holdout_opened=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    from database.db import DatabaseManager

    plan=json.loads(args.plan.read_text(encoding='utf-8'))
    with DatabaseManager(args.plan.parent/'experiments.db') as registry,DatabaseManager(read_only=True) as source_db:
        result=run_dip_study(registry,source_db,plan=plan,root=args.output)
    print(json.dumps(dict(folds=len(result['records']),summary=result['summary'],accepted_for_signals=False),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
