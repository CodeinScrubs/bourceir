"""Preregister bounded publication requests, then reconstruct an official-mark coverage study."""
import argparse
import hashlib
import json
import math
import os
import shutil
import uuid
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.daily_publications import _clock, _unique, publication_request
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from ops.research_review import _relative_file, _verify_inventory, read_development_parquet
from research.rare_event_marks import VERSION as LABEL_VERSION
from research.rare_event_marks import compute_marked_rare_events, prepare_mark_ledger, summarize_marked_events
from research.rare_event_run import _inputs
from research.rare_events import HORIZONS, compute_rare_events, summarize_rare_events
from research.rare_events import VERSION as PARENT_VERSION
from research.run import _json_safe, _sha256, archive_research_code, research_code_fingerprint

VERSION = 'no-trade-mark-coverage-study-v1'
MAX_JSON_BYTES = 32*1024*1024
MAX_REQUESTS = 100
FALSE_FLAGS = ('holdout_opened', 'accepted_for_signals', 'profitability_proven',
               'calibration_performed', 'model_selection_performed', 'historical_membership_verified',
               'prospective_independence', 'economic_entitlements_verified', 'executable_return_proven')
PLAN_FILES = ('prices.parquet','cohort.parquet','parent-outcomes.parquet','parent-result.json',
              'parent-protocol.json','registry.json','selection.json','protocol.json')
FINAL_FILES = (*PLAN_FILES,'receipts.json','ledger.json','outcomes.parquet')


def _load(path,expected_sha=None):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>MAX_JSON_BYTES:
        raise ValueError('سند مطالعه ارزش موجود نیست یا از حد حجم گذشت')
    with path.open('rb') as stream:
        body=stream.read(MAX_JSON_BYTES+1)
    if len(body)>MAX_JSON_BYTES:
        raise ValueError('سند مطالعه ارزش هنگام خواندن از حد حجم گذشت')
    if expected_sha is not None and hashlib.sha256(body).hexdigest()!=expected_sha:
        raise ValueError('اثر انگشت تصویر بایتی سند مطالعه ارزش برابر نیست')
    def finite(value):
        number=float(value)
        if not math.isfinite(number):
            raise ValueError('عدد غیرمتناهی در سند مطالعه ارزش')
        return number
    def invalid(_):
        raise ValueError('عدد غیرمتناهی در سند مطالعه ارزش')
    try:
        return json.loads(body.decode('utf-8'),object_pairs_hook=_unique,
                          parse_float=finite,parse_constant=invalid)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as error:
        raise ValueError('JSON مطالعه ارزش معتبر نیست') from error


def _write(path,value):
    body=json.dumps(_json_safe(value),ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
    if len(body)>MAX_JSON_BYTES:
        raise ValueError('خروجی مطالعه ارزش از حد حجم گذشت')
    temporary=path.with_suffix('.tmp')
    temporary.write_bytes(body)
    os.replace(temporary,path)


def _register(db,identifier,protocol,metrics):
    if Path(db.db_path).resolve()==settings.DB_PATH.resolve():
        raise ValueError('دیتابیس مطالعه ارزش باید مستقل از حساب باشد')
    row=db.log_experiment(experiment_id=identifier,strategy_name=VERSION,
        symbols_tested='fixed parent observed-symbol cohort; historical membership unverified',
        parameters_json=json.dumps(protocol,ensure_ascii=False,allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics),ensure_ascii=False,allow_nan=False),
        start_date=protocol['development_start'],end_date=protocol['observed_through'],
        dataset_version=protocol['prices_sha256'],features_version='none',model_version=LABEL_VERSION)
    if row is None:
        raise RuntimeError('ثبت مطالعه ارزش ناموفق بود')


def select_gap_keys(prices,cohort,calendar,*,max_requests=MAX_REQUESTS):
    """Prioritize single missing-date windows, without reading future return outcomes."""
    if type(max_requests) is not int or not 1<=max_requests<=MAX_REQUESTS:
        raise ValueError('بودجه دریافت باید یک تا صد درخواست باشد')
    positions={d:i for i,d in enumerate(calendar)}
    present={symbol:set(group.date) for symbol,group in prices.groupby('symbol')}
    candidates=Counter()
    for symbol,anchors in cohort.groupby('symbol'):
        known=present[symbol]
        missing=np.array([d not in known for d in calendar])
        cumulative=np.r_[0,missing.cumsum()]
        missing_positions=np.flatnonzero(missing)
        for day in anchors.date:
            start=positions[day]
            for horizon in HORIZONS:
                end=start+horizon
                if end>=len(calendar) or cumulative[end+1]-cumulative[start+1]!=1:
                    continue
                index=missing_positions[np.searchsorted(missing_positions,start+1)]
                candidates[symbol,calendar[index]]+=1
    ranking=[dict(symbol=symbol,date=day,potential_window_count=count)
             for (symbol,day),count in candidates.items()]
    ranking.sort(key=lambda row:(-row['potential_window_count'],row['symbol'],row['date']))
    selected,seen=[],set()
    for row in ranking:
        if row['symbol'] not in seen:
            seen.add(row['symbol']);selected.append(row)
            if len(selected)==max_requests:
                break
    return dict(rule='one absent raw date in a fully observable nominal window; potential windows desc, symbol/date asc; first key per symbol; no future-return selection',
        missing_keys=len(ranking),candidate_symbols=len({r['symbol'] for r in ranking}),
        selected=selected,max_requests=max_requests,
        potential_windows_selected=sum(r['potential_window_count'] for r in selected),
        limitation='Potential windows may already have an established hit or other invalid data; these counts are not resolved outcomes or a prevalence estimate.')


def _parent_binding(parent_db,result,protocol):
    binding=[]
    for identifier,metrics in ((result['experiment_id']+'-PROTOCOL',{}),(result['experiment_id'],result['summary'])):
        row=parent_db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?',
                                  (identifier,'completed')).fetchone()
        if row is None or json.loads(row['parameters_json'])!=protocol or json.loads(row['metrics_json'])!=metrics:
            raise ValueError('ثبت مستقل آرشیو والد با فایل‌ها برابر نیست')
        binding.append(dict(experiment_id=identifier,parameters=protocol,metrics=metrics))
    return binding


def _code_inventory(folder,name,inventory):
    root=folder/name
    if not root.resolve().is_relative_to(folder.resolve()) or root.is_symlink():
        raise ValueError('مسیر کد محفوظ بیرون از مطالعه است')
    _verify_inventory(root,inventory)


def _parent_inputs(folder,result,protocol):
    if (not isinstance(result,dict) or not isinstance(protocol,dict) or result.get('protocol')!=protocol
            or protocol.get('version')!=PARENT_VERSION or protocol.get('horizons')!=list(HORIZONS)
            or protocol.get('threshold')!=2. or type(protocol.get('strategy_trials_added')) is not int
            or protocol['strategy_trials_added']!=0
            or any(result.get(k) is not False for k in ('accepted_for_signals','profitability_proven'))
            or any(protocol.get(k) is not False for k in ('holdout_opened','accepted_for_signals','profitability_proven',
                'calibration_performed','prospective_independence'))):
        raise ValueError('قرارداد والد پژوهشی ثابت و بدون پذیرش لازم است')
    _code_inventory(folder,'code',protocol['source_code'])
    _verify_inventory(folder,result['artifacts_sha256'],parquet=True)
    prices,calendar,cohort=_inputs(folder/'prices.parquet',protocol['prices_sha256'],
        protocol['holdout_start'],protocol['development_start'],protocol['calendar_min_symbols'],protocol['anchor_stride'])
    if (calendar!=protocol['calendar'] or str(prices.date.max())!=protocol['observed_through']
            or len(prices)!=protocol['source_rows'] or len(cohort)!=protocol['opportunities']
            or _sha256(folder/'cohort.parquet')!=protocol['cohort_sha256']):
        raise ValueError('جامعه، تاریخ یا تقویم والد ناسازگار است')
    saved_cohort=read_development_parquet(folder/'cohort.parquet',protocol['cohort_sha256'],protocol['holdout_start'])
    pd.testing.assert_frame_equal(cohort.reset_index(drop=True),saved_cohort.reset_index(drop=True))
    return prices,calendar,cohort


def plan_study(db,*,parent_folder,parent_db,registry_path,registry_sha256,root,max_requests=MAX_REQUESTS):
    parent=Path(parent_folder).resolve()
    parent_result,parent_protocol=_load(parent/'result.json'),_load(parent/'protocol.json')
    prices,calendar,cohort=_parent_inputs(parent,parent_result,parent_protocol)
    binding=_parent_binding(parent_db,parent_result,parent_protocol)
    registry_path=Path(registry_path)
    if _sha256(registry_path)!=registry_sha256:
        raise ValueError('اثر انگشت مشخصات عمومی نمادها ناسازگار است')
    registry=_load(registry_path,registry_sha256)
    if not isinstance(registry,dict) or set(registry)!={'symbols'} or not isinstance(registry['symbols'],list):
        raise ValueError('فهرست صریح مشخصات عمومی لازم است')
    metadata={}
    for row in registry['symbols']:
        if (not isinstance(row,dict) or set(row)!={'symbol_id','symbol','ins_code','instrument_type_code'}
                or type(row['symbol_id']) is not int or row['symbol_id']<=0
                or not isinstance(row['symbol'],str) or not row['symbol'] or normalize_symbol(row['symbol'])!=row['symbol']
                or type(row['instrument_type_code']) is not int or row['instrument_type_code'] not in (300,303)
                or row['symbol'] in metadata or not isinstance(row['ins_code'],str)):
            raise ValueError('مشخصات عمومی نماد عادی یا یکتایی آن معتبر نیست')
        code,_=publication_request(row['ins_code'],parent_protocol['observed_through'],'price')
        if code!=row['ins_code']:
            raise ValueError('شناسه متنی صریح لازم است')
        metadata[row['symbol']]=row
    selection=select_gap_keys(prices,cohort,calendar,max_requests=max_requests)
    if not selection['selected']:
        raise ValueError('کلید شکاف با قرارداد این پایلوت موجود نیست')
    tasks,unavailable=[],[]
    for selected in selection['selected']:
        row=metadata.get(selected['symbol'])
        if row is None:
            unavailable.append(selected)
        else:
            tasks.append(dict(row,date=selected['date'],kind='price'))
    identifier=f'EXP-{now_tehran_iso()[:10]}-RAREMARK-{uuid.uuid4().hex}'
    folder=Path(root).resolve()/identifier
    folder.mkdir(parents=True,exist_ok=False)
    for source,target in (('prices.parquet','prices.parquet'),('cohort.parquet','cohort.parquet'),('outcomes.parquet','parent-outcomes.parquet')):
        shutil.copyfile(parent/source,folder/target)
        if _sha256(parent/source)!=_sha256(folder/target):
            raise ValueError('ورودی والد هنگام کپی تغییر کرد')
    shutil.copytree(parent/'code',folder/'parent-code')
    _write(folder/'parent-result.json',parent_result);_write(folder/'parent-protocol.json',parent_protocol)
    shutil.copyfile(registry_path,folder/'registry.json')
    if _sha256(folder/'registry.json')!=registry_sha256 or _sha256(registry_path)!=registry_sha256:
        raise ValueError('مشخصات هنگام کپی تغییر کرد')
    _write(folder/'selection.json',selection)
    protocol=dict(version=VERSION,label_contract=LABEL_VERSION,experiment_id=identifier,registered_at=now_tehran_iso(),
        development_start=parent_protocol['development_start'],holdout_start=parent_protocol['holdout_start'],
        observed_through=parent_protocol['observed_through'],prices_sha256=parent_protocol['prices_sha256'],
        cohort_sha256=parent_protocol['cohort_sha256'],parent_experiment_id=parent_result['experiment_id'],
        parent_registration=binding,registry_sha256=registry_sha256,calendar=calendar,
        horizons=list(HORIZONS),threshold=2.,source_code=archive_research_code(folder/'code'),code_sha256=research_code_fingerprint(),
        max_requests=max_requests,tasks=tasks,metadata_unavailable=unavailable,
        fixed_input_sha256={name:_sha256(folder/name) for name in PLAN_FILES if name!='protocol.json'},
        strategy_trials_added=0,descriptive_target_definitions=len(HORIZONS),predictions_fitted=0,
        event='same parent synthetic official-close index, with byte-bound flat marks only in absent raw sessions and exact prior-close linkage',
        limits='no imputed OHLC, execution, economic rights, calibrated probability or historical availability proof',
        **dict.fromkeys(FALSE_FLAGS,False))
    _write(folder/'protocol.json',protocol)
    _register(db,identifier+'-PROTOCOL',protocol,{})
    return dict(experiment_id=identifier,folder=str(folder),tasks=tasks,selection=selection)


def _checked_plan(db,folder):
    protocol=_load(folder/'protocol.json')
    if (protocol.get('version')!=VERSION or protocol.get('label_contract')!=LABEL_VERSION
            or protocol.get('experiment_id')!=folder.name or protocol.get('horizons')!=list(HORIZONS)
            or protocol.get('threshold')!=2. or type(protocol.get('strategy_trials_added')) is not int
            or protocol['strategy_trials_added']!=0 or type(protocol.get('predictions_fitted')) is not int
            or protocol['predictions_fitted']!=0
            or protocol.get('descriptive_target_definitions')!=len(HORIZONS)
            or any(protocol.get(k) is not False for k in FALSE_FLAGS)):
        raise ValueError('نسخه یا ادعای مطالعه ارزش ناسازگار است')
    row=db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?',
        (folder.name+'-PROTOCOL','completed')).fetchone()
    if row is None or json.loads(row['parameters_json'])!=protocol or json.loads(row['metrics_json'])!={}:
        raise ValueError('پیش‌ثبت مستقل مطالعه ارزش ناسازگار است')
    _code_inventory(folder,'code',protocol['source_code'])
    if set(protocol['fixed_input_sha256'])!=set(PLAN_FILES)-{'protocol.json'}:
        raise ValueError('فهرست ثابت ورودی مطالعه ارزش ناقص است')
    for name,digest in protocol['fixed_input_sha256'].items():
        if _sha256(_relative_file(folder,name))!=digest:
            raise ValueError('ورودی ثابت مطالعه ارزش تغییر کرد')
    parent=_load(folder/'parent-result.json',protocol['fixed_input_sha256']['parent-result.json'])
    pp=_load(folder/'parent-protocol.json',protocol['fixed_input_sha256']['parent-protocol.json'])
    _code_inventory(folder,'parent-code',pp['source_code'])
    if (parent['protocol']!=pp or parent['experiment_id']!=protocol['parent_experiment_id']
            or protocol['calendar']!=pp['calendar'] or protocol['prices_sha256']!=pp['prices_sha256']
            or protocol['cohort_sha256']!=pp['cohort_sha256']
            or protocol['holdout_start']!=pp['holdout_start'] or protocol['development_start']!=pp['development_start']
            or protocol['observed_through']!=pp['observed_through']):
        raise ValueError('والد محفوظ با قرارداد مطالعه برابر نیست')
    for binding,identifier,metrics in zip(protocol['parent_registration'],
            (parent['experiment_id']+'-PROTOCOL',parent['experiment_id']),({},parent['summary']),strict=True):
        if binding!=dict(experiment_id=identifier,parameters=pp,metrics=metrics):
            raise ValueError('ثبت والد محفوظ ناسازگار است')
    prices,calendar,cohort=_inputs(folder/'prices.parquet',protocol['prices_sha256'],protocol['holdout_start'],
        protocol['development_start'],pp['calendar_min_symbols'],pp['anchor_stride'])
    saved_cohort=read_development_parquet(folder/'cohort.parquet',protocol['cohort_sha256'],protocol['holdout_start'])
    pd.testing.assert_frame_equal(cohort.reset_index(drop=True),saved_cohort.reset_index(drop=True))
    selection=select_gap_keys(prices,cohort,calendar,max_requests=protocol['max_requests'])
    if selection!=_load(folder/'selection.json',protocol['fixed_input_sha256']['selection.json']):
        raise ValueError('انتخاب کلیدهای شکاف بازتولید نشد')
    metadata={row['symbol']:row for row in _load(folder/'registry.json',protocol['fixed_input_sha256']['registry.json'])['symbols']}
    tasks=[dict(metadata[r['symbol']],date=r['date'],kind='price') for r in selection['selected'] if r['symbol'] in metadata]
    missing=[r for r in selection['selected'] if r['symbol'] not in metadata]
    if tasks!=protocol['tasks'] or missing!=protocol['metadata_unavailable']:
        raise ValueError('برنامه درخواست یا داده ناشناخته عوض شد')
    return protocol,prices,calendar,cohort,parent


def _derive(folder,protocol,prices,calendar,cohort,parent,receipts,*,completed_by,receipts_sha256):
    if not isinstance(receipts,list) or len(receipts)!=len(protocol['tasks']):
        raise ValueError('رسید تمام درخواست‌های ثبت‌شده، شامل شکست‌ها لازم است')
    for task,record in zip(protocol['tasks'],receipts,strict=True):
        if (not isinstance(record,dict) or record.get('task')!=task
                or not _clock(protocol['registered_at'])<=_clock(record['attempted_at'])
                <=_clock(record['completed_at'])<=_clock(completed_by)):
            raise ValueError('رسید پیش از ثبت یا بیرون از برنامه است')
    strict=compute_rare_events(prices,cohort,calendar,observed_through=protocol['observed_through'])
    saved_strict=read_development_parquet(folder/'parent-outcomes.parquet',
        parent['artifacts_sha256']['outcomes.parquet'],protocol['holdout_start'])
    pd.testing.assert_frame_equal(strict,saved_strict)
    if _json_safe(summarize_rare_events(strict))!=parent['summary']:
        raise ValueError('خلاصه والد از خام بازتولید نشد')
    ledger=prepare_mark_ledger(prices,calendar,receipts,observed_through=protocol['observed_through'])
    outcomes=compute_marked_rare_events(prices,cohort,calendar,observed_through=protocol['observed_through'],receipts=receipts)
    equal=pd.Series(True,index=strict.index)
    for column in strict:
        equal&=strict[column].eq(outcomes[column]).fillna(False) | (strict[column].isna() & outcomes[column].isna())
    transitions=[]
    for horizon in HORIZONS:
        where=strict.horizon_sessions.eq(horizon)
        old,new=strict.loc[where],outcomes.loc[where]
        known=old.outcome.notna()
        if not old.loc[known,'outcome'].equals(new.loc[known,'outcome']):
            raise ValueError('نتیجه معلوم قبلی نباید با دفتر شکاف عوض شود')
        transitions.append(dict(horizon_sessions=horizon,opportunities=len(old),changed_rows=int((~equal[where]).sum()),
            resolved_outcomes=int((old.outcome.isna() & new.outcome.notna()).sum()),
            resolved_endpoint_values=int((old.endpoint_multiple.isna() & new.endpoint_multiple.notna()).sum()),
            unknown_before=int(old.outcome.isna().sum()),unknown_after=int(new.outcome.isna().sum()),
            established_outcomes_changed=0))
    metrics=dict(summary=summarize_marked_events(outcomes),comparison=transitions,
        ledger_statuses=dict(Counter(r['status'] for r in ledger)),parent_outcomes_recomputed=len(strict),
        receipts_sha256=receipts_sha256,
        parent_replay_verified=True,receipts=len(receipts),source_scope='public_market_only',
        strategy_trials_added=0,predictions_fitted=0,accepted_for_signals=False,profitability_proven=False)
    return outcomes,ledger,metrics


def finish_study(db,folder,*,receipts_path,receipts_sha256):
    folder=Path(folder).resolve()
    if (folder/'result.json').exists() or (folder/'receipts.json').exists():
        raise ValueError('مطالعه تکمیل‌شده یا تلاش قبلی بازنویسی نمی‌شود')
    protocol,prices,calendar,cohort,parent=_checked_plan(db,folder)
    if research_code_fingerprint()!=protocol['code_sha256']:
        raise ValueError('کد پس از پیش‌ثبت تغییر کرده است؛ مطالعه تازه لازم است')
    receipts_path=Path(receipts_path)
    if _sha256(receipts_path)!=receipts_sha256:
        raise ValueError('هش رسیدهای ورودی برابر نیست')
    receipts=_load(receipts_path,receipts_sha256)
    shutil.copyfile(receipts_path,folder/'receipts.json')
    if _sha256(folder/'receipts.json')!=receipts_sha256 or _sha256(receipts_path)!=receipts_sha256:
        raise ValueError('رسید هنگام کپی تغییر کرد')
    outcomes,ledger,metrics=_derive(folder,protocol,prices,calendar,cohort,parent,receipts,
        completed_by=now_tehran_iso(),receipts_sha256=receipts_sha256)
    _write(folder/'ledger.json',ledger)
    outcomes.to_parquet(folder/'outcomes.parquet',index=False)
    if research_code_fingerprint()!=protocol['code_sha256']:
        raise ValueError('کد هنگام بازحساب تغییر کرد؛ نتیجه پذیرفته نیست')
    result=dict(version=VERSION,experiment_id=folder.name,protocol=protocol,metrics=metrics,completed_at=now_tehran_iso(),
        artifacts_sha256={name:_sha256(folder/name) for name in FINAL_FILES},
        accepted_for_signals=False,profitability_proven=False)
    _register(db,folder.name,protocol,metrics)
    _write(folder/'result.json',result)
    return result


def fetch_study(db,folder,*,provider=None,provider_factory=None):
    """Coordinate public collection through the provider interface after independent registration."""
    from data_collector.daily_publications import DailyPublicationArchive, review_archive

    folder=Path(folder).resolve()
    protocol,*_=_checked_plan(db,folder)
    if research_code_fingerprint()!=protocol['code_sha256'] or (folder/'result.json').exists():
        raise ValueError('کد تغییر کرده یا مطالعه قبلاً تکمیل شده است')
    collection=folder/'collection'
    collection.mkdir(exist_ok=False)
    archive_path,review=None,None
    if protocol['tasks']:
        archive=DailyPublicationArchive(collection/'publications',protocol['tasks'],workers=2)
        records=archive.capture(provider,provider_factory=provider_factory)
        archive_path=archive.finish(dict(applied=False,reason='registered no-trade valuation coverage study'))
        review=review_archive(archive_path)
    else:
        records=[]
    if research_code_fingerprint()!=protocol['code_sha256']:
        raise ValueError('کد هنگام دریافت مطالعه تغییر کرد')
    receipts_path=collection/'receipts.json'
    _write(receipts_path,records)
    receipt=dict(completed_at=now_tehran_iso(),publication_archive=archive_path,publication_review=review,
        receipts_path=str(receipts_path),receipts_sha256=_sha256(receipts_path),
        planned_requests=len(protocol['tasks']),records_preserved=len(records),applied=False,
        accepted_for_signals=False,profitability_proven=False)
    _write(collection/'result.json',receipt)
    return receipt


def review_study(db,folder):
    folder=Path(folder).resolve()
    result=_load(folder/'result.json')
    protocol,prices,calendar,cohort,parent=_checked_plan(db,folder)
    if (result.get('version')!=VERSION or result.get('experiment_id')!=folder.name or result.get('protocol')!=protocol
            or any(result.get(k) is not False for k in ('accepted_for_signals','profitability_proven'))
            or set(result.get('artifacts_sha256',{}))!=set(FINAL_FILES)
            or {p.name for p in folder.iterdir() if p.is_file()}!=set(FINAL_FILES)|{'result.json'}):
        raise ValueError('هویت، فهرست یا ادعای نتیجه مطالعه ناسازگار است')
    for name,digest in result['artifacts_sha256'].items():
        if _sha256(_relative_file(folder,name))!=digest:
            raise ValueError('اثر انگشت نتیجه مطالعه تغییر کرد')
    receipts=_load(folder/'receipts.json',result['artifacts_sha256']['receipts.json'])
    if any(_clock(r['completed_at'])>_clock(result['completed_at']) for r in receipts):
        raise ValueError('رسید پس از پایان مطالعه ثبت شده است')
    outcomes,ledger,metrics=_derive(folder,protocol,prices,calendar,cohort,parent,receipts,
        completed_by=result['completed_at'],receipts_sha256=result['artifacts_sha256']['receipts.json'])
    saved_outcomes=read_development_parquet(folder/'outcomes.parquet',
        result['artifacts_sha256']['outcomes.parquet'],protocol['holdout_start'])
    pd.testing.assert_frame_equal(outcomes,saved_outcomes)
    if ledger!=_load(folder/'ledger.json',result['artifacts_sha256']['ledger.json']) or _json_safe(metrics)!=result['metrics']:
        raise ValueError('دفتر یا آمار مطالعه بازتولید نشد')
    row=db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?',
        (folder.name,'completed')).fetchone()
    if row is None or json.loads(row['parameters_json'])!=protocol or json.loads(row['metrics_json'])!=result['metrics']:
        raise ValueError('ثبت نتیجه با آرشیو برابر نیست')
    return dict(version=VERSION,experiment_id=folder.name,parent_outcomes_recomputed=len(outcomes),
        marked_outcomes_recomputed=len(outcomes),current_code_matches_capture=research_code_fingerprint()==protocol['code_sha256'],
        accepted_for_signals=False,profitability_proven=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--fetch',type=Path)
    mode.add_argument('--finish',type=Path);mode.add_argument('--review',type=Path)
    parser.add_argument('--parent',type=Path);parser.add_argument('--parent-db',type=Path)
    parser.add_argument('--registry',type=Path);parser.add_argument('--registry-sha256')
    parser.add_argument('--receipts',type=Path);parser.add_argument('--receipts-sha256')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=settings.DATA_DIR/'private'/'no-trade-studies')
    args=parser.parse_args(argv)
    from database.db import DatabaseManager
    if args.db.resolve()==settings.DB_PATH.resolve():
        parser.error('a separate study DB is required')
    if args.plan:
        if any(v is None for v in (args.parent,args.parent_db,args.registry,args.registry_sha256)):
            parser.error('plan needs parent, parent-db, registry and its explicit hash')
        with DatabaseManager(args.db) as db,DatabaseManager(args.parent_db,read_only=True) as parent:
            output=plan_study(db,parent_folder=args.parent,parent_db=parent,registry_path=args.registry,
                registry_sha256=args.registry_sha256,root=args.root)
    elif args.fetch:
        from data_collector.tsetmc import TsetmcProvider
        with DatabaseManager(args.db) as db:
            output=fetch_study(db,args.fetch,provider_factory=lambda:TsetmcProvider(realtime=True))
    elif args.finish:
        if args.receipts is None or args.receipts_sha256 is None:
            parser.error('finish needs receipts and their explicit hash')
        with DatabaseManager(args.db) as db:
            result=finish_study(db,args.finish,receipts_path=args.receipts,receipts_sha256=args.receipts_sha256)
        output=dict(experiment_id=result['experiment_id'],metrics=result['metrics'],accepted_for_signals=False)
    else:
        with DatabaseManager(args.db,read_only=True) as db:
            output=review_study(db,args.review)
    print(json.dumps(_json_safe(output),ensure_ascii=False,allow_nan=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
