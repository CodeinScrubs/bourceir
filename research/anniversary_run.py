"""Two-phase, archived owner-requested anniversary reconstruction of a seen period."""

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.opportunity_radar import digest, scope_rows, stamp
from core.timeutils import is_canonical_iso_date, now_tehran, now_tehran_iso
from database.db import DatabaseManager
from ops.report_files import private_report_path, read_private_report, write_private_report
from research.anniversary import VERSION, aggregate_sleeves, asof_selection, replay_sleeve, scoped_prices
from research.labels import market_calendar
from research.run import _json_safe

FILES=('research/anniversary.py','research/anniversary_run.py','research/baselines.py','research/labels.py',
    'backtest/portfolio.py','backtest/fractions.py','core/market_rules.py','core/price_quality.py',
    'core/opportunity_radar.py','core/risk.py','core/order_book.py','core/text.py','core/timeutils.py','config/settings.py','config/default.yaml',
    'config/production.yaml','research/run.py','ops/report_files.py',
    'database/db.py','database/migrator.py','requirements.lock.txt')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code():
    return {p:hashlib.sha256((settings.BASE_DIR/p).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in FILES}


def _root(path):
    path=Path(path).resolve()
    private_report_path(path/'protocol.json',forbidden_paths=(settings.DB_PATH,))
    return path


def _write(path,value):
    return write_private_report(path,_json_safe(value))


def _register(folder,identifier,protocol,metrics):
    with DatabaseManager(folder/'experiments.db') as db:
        result=db.log_experiment(experiment_id=identifier,strategy_name=VERSION,
            symbols_tested='fixed current-known registry, historical membership unverified',
            parameters_json=json.dumps(_json_safe(protocol),ensure_ascii=False,allow_nan=False),
            metrics_json=json.dumps(_json_safe(metrics),ensure_ascii=False,allow_nan=False),
            start_date=protocol['intent']['requested_start'],end_date=protocol['intent']['requested_end'],
            dataset_version=protocol['source_sha256'],features_version='strict-market-session-momentum20',model_version=VERSION)
        if result is None:
            raise RuntimeError('Experiment registration failed')


def freeze_selection(folder,*,prices_path,registry_path,intent_path,acknowledge_seen_period=False):
    folder=_root(folder)
    intent=read_private_report(intent_path)
    for key in ('requested_start','requested_end'):
        if not is_canonical_iso_date(intent[key]):
            raise ValueError('Canonical start/end dates required')
    if intent['requested_start']>=intent['requested_end'] or intent['requested_end']>now_tehran().date().isoformat():
        raise ValueError('A completed historical interval is required')
    if intent['requested_end']>=settings.RESEARCH_CONFIG['holdout_start'] and not acknowledge_seen_period:
        raise ValueError('Explicit owner-authorized, previously-seen-period acknowledgement required; final holdout selector is not used')
    if (intent['version']!=VERSION or intent['k']!=10 or intent['accepted_for_signals'] is not False
            or intent['period_previously_observed'] is not True or intent['models_fitted']!=0
            or intent['fees']!=dict(settings.FEES) or intent['initial_capital_rial']<=0):
        raise ValueError('Fixed ten-stock reconstruction intent required')
    if folder.exists():
        raise ValueError('A frozen study already exists; replay it instead of reselecting')
    source=Path(prices_path).resolve()
    if source.suffix!='.parquet' or source==settings.DB_PATH.resolve() or source.stat().st_size>512*1024*1024:
        raise ValueError('Bounded public-market Parquet source required')
    identities=scope_rows(json.loads(Path(registry_path).read_text(encoding='utf-8')))
    folder.mkdir(parents=True)
    source_hash=sha(source)
    shutil.copyfile(source,folder/'public-source.parquet')
    if sha(folder/'public-source.parquet')!=source_hash or sha(source)!=source_hash:
        raise ValueError('Source changed while pinning the public snapshot')
    _write(folder/'registry.json',dict(identities=identities))
    protocol=dict(id='ANNIVERSARY-'+uuid.uuid4().hex,version=VERSION,registered_at=now_tehran_iso(),
        intent=intent,source_sha256=source_hash,registry_sha256=sha(folder/'registry.json'),source_code=code(),
        current_known_universe=True,historical_membership_verified=False,original_vintage_verified=False,
        previously_seen_period_acknowledged=acknowledge_seen_period,
        untouched_final_holdout=False,global_holdout_selection_ledger_changed=False,
        live_account_access=False,models_fitted=0,accepted_for_signals=False,profitability_proven=False)
    for name in FILES:
        target=folder/'code'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n'))
    _write(folder/'protocol.json',protocol)
    _register(folder,protocol['id']+'-PROTOCOL',protocol,{})
    # No future numeric columns are requested during the selection phase.
    past=pd.read_parquet(folder/'public-source.parquet',filters=[('date','<=',intent['requested_start'])])
    selection=asof_selection(past,identities,requested_start=intent['requested_start'],
        k=intent['k'],min_value=intent['min_daily_value_rial'])
    if len(selection['selected'])!=intent['k']:
        raise ValueError('Too few eligible as-of stocks; no future-data fallback is permitted')
    selection['panel'].to_parquet(folder/'asof-panel.parquet',index=False)
    selection['selected'].to_parquet(folder/'selected.parquet',index=False)
    frozen=dict(registered_at=now_tehran_iso(),decision_date=selection['decision_date'],
        files_sha256={name:sha(folder/name) for name in ('asof-panel.parquet','selected.parquet')},
        known_registry=len(identities),eligible=int(selection['panel'].status.eq('eligible').sum()),
        status_counts=selection['panel'].status.value_counts().to_dict(),
        selected=selection['selected'].to_dict('records'),outcomes_read=False,ranking_is_not_probability=True)
    _write(folder/'selection.json',frozen)
    _register(folder,protocol['id']+'-SELECTION',protocol,frozen)
    return frozen


def _verified_selection(folder,*,allow_review_code_change=False):
    protocol=read_private_report(folder/'protocol.json')
    frozen=read_private_report(folder/'selection.json')
    if protocol['source_code']!=code() and not allow_review_code_change:
        raise ValueError('Registered replay code differs')
    if sha(folder/'public-source.parquet')!=protocol['source_sha256'] or sha(folder/'registry.json')!=protocol['registry_sha256']:
        raise ValueError('Pinned public inputs differ')
    for name,expected in protocol['source_code'].items():
        if sha(folder/'code'/name)!=expected:
            raise ValueError('Archived replay code differs')
    for name,expected in frozen['files_sha256'].items():
        if sha(folder/name)!=expected:
            raise ValueError('Frozen selection artifact differs')
    identities=read_private_report(folder/'registry.json')['identities']
    past=pd.read_parquet(folder/'public-source.parquet',filters=[('date','<=',protocol['intent']['requested_start'])])
    selection=asof_selection(past,identities,requested_start=protocol['intent']['requested_start'],
        k=10,min_value=protocol['intent']['min_daily_value_rial'])
    pd.testing.assert_frame_equal(selection['panel'],pd.read_parquet(folder/'asof-panel.parquet'))
    pd.testing.assert_frame_equal(selection['selected'],pd.read_parquet(folder/'selected.parquet'))
    if _json_safe(selection['selected'].to_dict('records'))!=frozen['selected'] or selection['decision_date']!=frozen['decision_date']:
        raise ValueError('Selection receipt differs from recomputed as-of inputs')
    return protocol,frozen,selection


def _same_frame(path,frame):
    saved=pd.read_parquet(path)
    if frame.shape!=(0,0) or saved.shape!=(0,0):
        # An invalid path's final row omits risk flags. Parquet restores an
        # object column's NaN as None; both remain unknown. Normalize only null
        # representations, preserving dtypes and every nonmissing value.
        def nulls(value):
            value=value.reset_index(drop=True).copy()
            for name,dtype in value.dtypes.items():
                if dtype==object:
                    value[name]=value[name].where(value[name].notna(),None)
            return value
        pd.testing.assert_frame_equal(nulls(frame),nulls(saved))


def _evaluate(folder,protocol,frozen,selection,*,save,verify_artifacts=False):
    intent=protocol['intent']
    decision=frozen['decision_date']
    prices=pd.read_parquet(folder/'public-source.parquet',filters=[('date','<=',intent['requested_end'])])
    identities=read_private_report(folder/'registry.json')['identities']
    prices=scoped_prices(prices,identities,through=intent['requested_end'])
    calendar=market_calendar(prices).tolist()
    days=[d for d in calendar if decision<=d<=intent['requested_end']]
    if not days or days[-1]<intent['requested_start']:
        raise ValueError('No outcome interval is available')
    pool=selection['panel'].loc[selection['panel'].status.eq('eligible')].sort_values('symbol')
    names=pool.symbol.tolist()
    primary=selection['selected'].symbol.tolist()
    summaries,curves={},{}
    # Evaluate every eligible name for fixed, same-capital random controls. Selection stays locked.
    order=primary+[name for name in names if name not in primary]
    for index,name in enumerate(order):
        own=prices.loc[prices.symbol.eq(name)].copy()
        for scenario in ('normal','stress'):
            case,curve,executions,decisions=replay_sleeve(own,symbol=name,decision=decision,calendar=days,
                initial_capital_rial=intent['initial_capital_rial']/10,fees=intent['fees'],
                slippage_bps=intent['slippage_bps'],participation=intent['participation'],stress=scenario=='stress')
            summaries[(name,scenario)]=case
            curves[(name,scenario)]=curve
            stem=str(pool.loc[pool.symbol.eq(name),'ins_code'].iloc[0])+'-'+scenario
            for kind,frame in [('curve',curve),('executions',executions),('decisions',decisions)]:
                target=folder/(stem+'-'+kind+'.parquet')
                if save:
                    frame.to_parquet(target,index=False)
                if verify_artifacts:
                    _same_frame(target,frame)
        if save and (index+1)%25==0:
            print(json.dumps(dict(completed=index+1,total=len(order))),flush=True)
    results={}
    rng=np.random.default_rng(41)
    control_picks=[rng.choice(names,size=10,replace=False).tolist() for _ in range(100)]
    for scenario in ('normal','stress'):
        cases=[summaries[(name,scenario)] for name in primary]
        result,curve=aggregate_sleeves(cases,[curves[(name,scenario)] for name in primary],k=10,
            initial_capital_rial=intent['initial_capital_rial'])
        if save:
            curve.to_parquet(folder/(scenario+'-basket-curve.parquet'),index=False)
        if verify_artifacts:
            _same_frame(folder/(scenario+'-basket-curve.parquet'),curve)
        random_returns=[]
        for chosen in control_picks:
            values=[summaries[(name,scenario)]['net_model_return'] for name in chosen]
            random_returns.append(float(np.mean(values)) if all(v is not None for v in values) else None)
        known=[value for value in random_returns if value is not None]
        result.update(stocks=cases,random_control_returns=random_returns,
            random_controls=100,known_random_controls=len(known),unknown_random_controls=100-len(known),
            random_control_mean=float(np.mean(known)) if len(known)==100 else None,
            known_random_subset_mean=float(np.mean(known)) if known else None,
            random_control_quantiles=np.quantile(known,[.05,.5,.95]).tolist() if len(known)==100 else None,
            risk_matched=False,significance_test_performed=False)
        results[scenario]=result
    return _json_safe(dict(version=VERSION,decision_date=decision,entry_session=days[1],terminal_session=days[-1],
        requested_start=intent['requested_start'],requested_end=intent['requested_end'],calendar_sessions=len(days),
        current_known_registry=frozen['known_registry'],eligible=frozen['eligible'],rule='momentum_20d',
        selected=primary,selection_sha256=sha(folder/'selection.json'),scenarios=results,
        all_eligible_cases=[dict(scenario=mode,**case) for (name,mode),case in summaries.items()],
        control_picks=control_picks,initial_capital_rial=intent['initial_capital_rial'],
        ranking_is_probability=False,economic_profit_verified=False,unseen_test=False,accepted_for_signals=False,
        corporate_action_model='synthetic whole shares and noncash fractions; actual cash/rights/credit dates unverified'))


def evaluate(folder):
    folder=_root(folder)
    if (folder/'result.json').exists():
        raise ValueError('Result exists; use review to replay it without creating a new trial')
    protocol,frozen,selection=_verified_selection(folder)
    _write(folder/'evaluation-start.json',dict(started_at=now_tehran_iso(),selection_sha256=sha(folder/'selection.json')))
    result=_evaluate(folder,protocol,frozen,selection,save=True)
    files={p.name:sha(p) for p in folder.glob('*.parquet')}
    result['artifacts_sha256']=files
    _write(folder/'result.json',result)
    _register(folder,protocol['id']+'-RESULT',protocol,result)
    return result


def review(folder,*,allow_review_code_change=False):
    folder=_root(folder)
    protocol,frozen,selection=_verified_selection(folder,allow_review_code_change=allow_review_code_change)
    saved=read_private_report(folder/'result.json',max_bytes=64*1024*1024)
    started=read_private_report(folder/'evaluation-start.json')
    if (started['selection_sha256']!=sha(folder/'selection.json') or
            not stamp(protocol['registered_at'])<=stamp(frozen['registered_at'])<=stamp(started['started_at'])):
        raise ValueError('Selection/evaluation chronology differs')
    with DatabaseManager(folder/'experiments.db',read_only=True) as db:
        for suffix,metrics in (('-PROTOCOL',{}),('-SELECTION',frozen),('-RESULT',saved)):
            row=db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=?',
                                (protocol['id']+suffix,)).fetchone()
            if row is None or json.loads(row['parameters_json'])!=protocol or json.loads(row['metrics_json'])!=metrics:
                raise ValueError('Immutable experiment registration differs')
    for name,expected in saved['artifacts_sha256'].items():
        if sha(folder/name)!=expected:
            raise ValueError('Evaluation artifact differs')
    actual=_evaluate(folder,protocol,frozen,selection,save=False,verify_artifacts=True)
    if digest(actual)!=digest({k:v for k,v in saved.items() if k!='artifacts_sha256'}):
        raise ValueError('Numerical reconstruction differs')
    return dict(verified=True,eligible_cases_recomputed=2*saved['eligible'],selected_ledgers_recomputed=20,
        random_controls_recomputed=200,selected=frozen['selected'],accepted_for_signals=False,economic_profit_verified=False,
        current_code_matches_capture=protocol['source_code']==code(),review_code_sha256=code(),
        captured_code_sha256=protocol['source_code'],selection_and_all_results_unchanged=True,
        experiment_registrations_verified=True,all_eligible_ledgers_verified=2*saved['eligible'],
        basket_curves_verified=2)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['select','evaluate','review'])
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--prices',type=Path)
    parser.add_argument('--registry',type=Path)
    parser.add_argument('--intent',type=Path)
    parser.add_argument('--acknowledge-seen-period',action='store_true')
    parser.add_argument('--allow-review-code-change',action='store_true',
        help='Review only: retain capture code and require identical recomputed selection, results and ledgers')
    args=parser.parse_args(argv)
    if args.allow_review_code_change and args.mode!='review':
        raise ValueError('Code changes may only replay existing results; no selection or evaluation bypass')
    if args.mode=='select':
        result=freeze_selection(args.folder,prices_path=args.prices,registry_path=args.registry,intent_path=args.intent,
            acknowledge_seen_period=args.acknowledge_seen_period)
    elif args.mode=='evaluate':
        result=evaluate(args.folder)
        result={k:v for k,v in result.items() if k not in ('scenarios','all_eligible_cases','control_picks')}
    else:
        result=review(args.folder,allow_review_code_change=args.allow_review_code_change)
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
