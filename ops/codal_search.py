"""ثبت دامنه پیش از جست‌وجو و بازحساب آفلاین کشف سند؛ بدون DB مالی یا اجرای کد آرشیوی."""

import argparse
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.codal_search import (
    FALSE_FLAGS,
    MAX_BYTES,
    OBSERVATION_KEYS,
    VERSION,
    assess_search,
    merge_letters,
    search_request,
    stamp,
)
from core.share_changes import share_change_request
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report

SOURCE_FILES=('core/codal_search.py','core/codal_documents.py','core/share_changes.py','core/text.py','core/timeutils.py',
              'data_collector/codal.py','ops/codal_search.py','ops/report_files.py','config/settings.py',
              'config/default.yaml','requirements.lock.txt','pyproject.toml')
EXTRA_FALSE=('database_read','database_writes','account_data_read','independent_strategy_test','external_authenticity_verified')
MAX_RESULT_BYTES=12*1024*1024


def _canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _code(name):return (settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n')


def _requests(instrument,start,end_exclusive,cutoff,max_pages,letter_type):
    if not isinstance(instrument,dict) or set(instrument)!={'symbol_fa','ins_code','instrument_type_code'}:
        raise ValueError('فراداده صریح یک سهم عادی لازم است')
    if type(instrument['instrument_type_code']) is not int or instrument['instrument_type_code'] not in (300,303):
        raise ValueError('جست‌وجو محدود به سهام عادی بورس و فرابورس است')
    code,_url=share_change_request(instrument['ins_code'])
    if code!=instrument['ins_code']:raise ValueError('شناسه بومی باید متن متعارف باشد')
    if type(max_pages) is not int or not 1<=max_pages<=10:
        raise ValueError('ظرفیت صفحه باید یک تا ده باشد')
    requests=[search_request(instrument['symbol_fa'],start=start,end_exclusive=end_exclusive,
        cutoff=cutoff,page=page,letter_type=letter_type) for page in range(1,max_pages+1)]
    if requests[0]['symbol_fa']!=instrument['symbol_fa']:raise ValueError('نماد باید از پیش یکسان‌سازی شده باشد')
    if cutoff>settings.RESEARCH_CONFIG['holdout_start'] or end_exclusive>now_tehran_iso()[:10]:
        raise ValueError('جست‌وجو از مرز مهروموم یا امروز نمی‌گذرد')
    return requests


def _files(folder):
    files={};size=0
    for index,path in enumerate(folder.rglob('*')):
        if index>=256:raise ValueError('شمار مسیرهای پرونده از حد مجاز گذشت')
        if not path.resolve().is_relative_to(folder):raise ValueError('فایل از ریشه پرونده خارج است')
        if path.is_file() and path!=folder/'manifest.json':
            length=path.stat().st_size;size+=length
            if length>16*1024*1024 or size>64*1024*1024:raise ValueError('حجم پرونده از حد مجاز گذشت')
            files[path.relative_to(folder).as_posix()]=_sha(path)
    return files


def _assessment(saved,body,request):
    if (not isinstance(saved,dict) or set(saved)!={'requested','observation','error_type'}
            or type(saved['requested']) is not bool or saved['error_type'] not in (None,'NotImplementedError','ValueError','OSError')):
        raise ValueError('قرارداد تلاش برابر نیست')
    observation=saved['observation']
    if not saved['requested']:
        if observation is not None or saved['error_type'] is not None or body is not None:
            raise ValueError('تلاش درخواست‌نشده رسید ندارد')
        return dict(version=VERSION,requested_page=request['page'],status='not_requested_after_stop',**dict.fromkeys(FALSE_FLAGS,False))
    if observation is not None and saved['error_type'] is not None:raise ValueError('خطا و رسید با هم مجاز نیستند')
    try:return assess_search(observation,body,request)
    except ValueError:return dict(version=VERSION,requested_page=request['page'],status='invalid_response',**dict.fromkeys(FALSE_FLAGS,False))


def _stop(assessment):
    status=assessment['status']
    if status=='coherent_observed_metadata':return None
    if status=='no_observed_letter_in_window' and assessment['row_count']==0:return 'observed_empty_page'
    return status


def _summary(plan,cases):
    stop=None;pages=[];attempted=0
    for case in cases:
        if case['requested'] is not (stop is None):raise ValueError('درخواست پس از توقف یا حذف تلاش پیش از توقف مجاز نیست')
        if case['requested']:
            attempted+=1;pages.append(case['assessment']);stop=_stop(case['assessment'])
    merged=merge_letters(pages)
    totals=sorted({p['total_claim'] for p in pages if p.get('total_claim') is not None})
    return dict(**merged,planned_page_capacity=len(plan['requests']),attempted_pages=attempted,
        not_requested_pages=len(cases)-attempted,status_counts=dict(Counter(c['assessment']['status'] for c in cases)),
        source_total_claims=totals,source_total_claim_changed=len(totals)>1,
        observed_row_count=sum(p.get('row_count') or 0 for p in pages),
        stop_reason=stop or 'page_capacity_reached',page_capacity_reached=stop is None,
        query_filter_semantics_independently_verified=False,**dict.fromkeys((*FALSE_FLAGS,*EXTRA_FALSE),False))


def capture_search(provider,instrument,*,start,end_exclusive,max_pages=3,letter_type=None):
    cutoff=settings.RESEARCH_CONFIG['holdout_start']
    requests=_requests(instrument,start,end_exclusive,cutoff,max_pages,letter_type)
    folder=(settings.DATA_DIR/'private/codal-search'/('SEARCH-'+uuid.uuid4().hex)).resolve()
    private_report_path(folder/'plan.json',forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True,exist_ok=False)
    code={}
    for name in SOURCE_FILES:
        target=folder/'code'/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(_code(name));code[name]=_sha(target)
    plan=dict(version=VERSION,study_id=folder.name,registered_at=now_tehran_iso(),instrument=instrument,
        start=start,end_exclusive=end_exclusive,cutoff=cutoff,max_pages=max_pages,letter_type=letter_type,
        requests=requests,code_sha256=code,metadata_source='caller_supplied_current_ordinary_metadata',
        **dict.fromkeys((*FALSE_FLAGS,*EXTRA_FALSE),False))
    write_private_report(folder/'plan.json',plan)
    cases=[];stop=None
    try:
        for index,request in enumerate(requests):
            requested=stop is None;observation=None;body=None;error_type=None
            if requested:
                try:
                    observed=provider.get_historical_search_observation(request['symbol_fa'],start=start,
                        end_exclusive=end_exclusive,page=request['page'],letter_type=letter_type)
                    if observed is not None:
                        if not isinstance(observed,dict) or set(observed)!=OBSERVATION_KEYS|{'content'}:
                            raise ValueError('رسید بایتی معتبر لازم است')
                        body=observed['content'];observation={k:v for k,v in observed.items() if k!='content'}
                        if not isinstance(body,bytes) or not 0<len(body)<=MAX_BYTES:raise ValueError('بدنه محدود لازم است')
                except (NotImplementedError,OSError,ValueError) as error:
                    error_type='NotImplementedError' if isinstance(error,NotImplementedError) else 'OSError' if isinstance(error,OSError) else 'ValueError'
                    observation=None;body=None
            saved=dict(requested=requested,observation=observation,error_type=error_type)
            prefix=f'page-{index+1:03d}';body_name=None
            if body is not None:
                (folder/'raw').mkdir(exist_ok=True);body_name='raw/'+prefix+'.json';(folder/body_name).write_bytes(body)
            write_private_report(folder/(prefix+'.json'),saved)
            if observation is not None and not stamp(plan['registered_at'])<=stamp(observation['requested_at'])<=stamp(observation['received_at'])<=stamp(now_tehran_iso()):
                raise ValueError('زمان رسید از پنجره ثبت خارج است')
            assessed=_assessment(saved,body,request)
            assessment_name=prefix+'-assessment.json';write_private_report(folder/assessment_name,assessed)
            cases.append(dict(request=request,requested=requested,observation_file=prefix+'.json',
                body_file=body_name,assessment_file=assessment_name,assessment=assessed))
            if requested:stop=_stop(assessed)
            if stop is None and index+1<len(requests):time.sleep(settings.RATE_LIMIT_DELAY)
        summary=_summary(plan,cases)
        # ارزیابی هر صفحه جداست؛ نتیجه فقط پیوند فایل‌ها و ادغام فراداده را نگه می‌دارد.
        compact=[{k:v for k,v in c.items() if k!='assessment'} for c in cases]
        result=dict(version=VERSION,study_id=folder.name,completed_at=now_tehran_iso(),cases=compact,summary=summary)
        if len(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8'))>MAX_RESULT_BYTES:
            raise ValueError('نتیجه جست‌وجو از سقف حجم گذشت')
        if any(hashlib.sha256(_code(n)).hexdigest()!=h for n,h in code.items()):
            raise ValueError('کد هنگام دریافت تغییر کرد؛ تلاش ناتمام محفوظ است')
        write_private_report(folder/'result.json',result)
        write_private_report(folder/'manifest.json',dict(version=VERSION,files=_files(folder)))
        review_search(folder)
        return dict(folder=str(folder),**result)
    except BaseException:
        write_private_report(folder/'failed.json',dict(complete=False,completed_pages=len(cases),planned_page_capacity=max_pages))
        raise


def review_search(folder):
    folder=Path(folder).resolve()
    if not re.fullmatch(r'SEARCH-[a-f0-9]{32}',folder.name):raise ValueError('شناسه جست‌وجو معتبر نیست')
    manifest=read_private_report(folder/'manifest.json')
    if set(manifest)!={'version','files'} or manifest['version']!=VERSION or manifest['files']!=_files(folder):
        raise ValueError('فهرست یا هش پرونده برابر نیست')
    plan=read_private_report(folder/'plan.json');result=read_private_report(folder/'result.json')
    keys={'version','study_id','registered_at','instrument','start','end_exclusive','cutoff','max_pages','letter_type',
        'requests','code_sha256','metadata_source',*FALSE_FLAGS,*EXTRA_FALSE}
    if (set(plan)!=keys or plan['version']!=VERSION or plan['study_id']!=folder.name
            or plan['metadata_source']!='caller_supplied_current_ordinary_metadata'
            or any(plan[k] is not False for k in (*FALSE_FLAGS,*EXTRA_FALSE))):raise ValueError('قرارداد برنامه تغییر کرده است')
    requests=_requests(plan['instrument'],plan['start'],plan['end_exclusive'],plan['cutoff'],plan['max_pages'],plan['letter_type'])
    if _canonical(requests)!=_canonical(plan['requests']) or set(plan['code_sha256'])!=set(SOURCE_FILES):
        raise ValueError('دامنه یا کد کامل برنامه برابر نیست')
    current=True
    for name,digest in plan['code_sha256'].items():
        if _sha(folder/'code'/name)!=digest:raise ValueError('کد محفوظ ناسازگار است')
        matches=hashlib.sha256(_code(name)).hexdigest()==digest;current &= matches
        if name not in ('data_collector/codal.py','config/default.yaml','pyproject.toml') and not matches:
            raise ValueError('کد تفسیر تغییر کرده؛ نسخه اصلی یا بازتفسیر صریح لازم است')
    if (set(result)!={'version','study_id','completed_at','cases','summary'} or result['version']!=VERSION
            or result['study_id']!=folder.name or len(result['cases'])!=len(requests)
            or not stamp(plan['registered_at'])<=stamp(result['completed_at'])<=stamp(now_tehran_iso())):
        raise ValueError('قرارداد یا زمان نتیجه برابر نیست')
    expected={'plan.json','result.json',*('code/'+n for n in SOURCE_FILES)};cases=[]
    for index,(request,case) in enumerate(zip(requests,result['cases'])):
        prefix=f'page-{index+1:03d}';name=prefix+'.json';assessment_name=prefix+'-assessment.json'
        expected.update((name,assessment_name));saved=read_private_report(folder/name)
        body_name=None;body=None;observation=saved.get('observation')
        if observation is not None:
            body_name='raw/'+prefix+'.json';expected.add(body_name);body=(folder/body_name).read_bytes()
            if not stamp(plan['registered_at'])<=stamp(observation['requested_at'])<=stamp(observation['received_at'])<=stamp(result['completed_at']):
                raise ValueError('زمان تلاش بیرون پنجره ثبت است')
        assessed=_assessment(saved,body,request)
        actual=dict(request=request,requested=saved['requested'],observation_file=name,body_file=body_name,assessment_file=assessment_name)
        if _canonical(actual)!=_canonical(case) or _canonical(assessed)!=_canonical(read_private_report(folder/assessment_name)):
            raise ValueError('بازحساب صفحه با نسخه محفوظ برابر نیست')
        cases.append(dict(actual,assessment=assessed))
    if (set(manifest['files'])!=expected or _canonical(_summary(plan,cases))!=_canonical(result['summary'])
            or manifest['files']!=_files(folder)):raise ValueError('مخرج پرونده یا ادغام بازحساب نشد')
    return dict(artifacts_verified=True,assessments_recomputed=True,semantic_code_matches_current=True,
        capture_code_matches_current=bool(current),database_read=False,network_requested=False,
        external_authenticity_verified=False,**result)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    capture=commands.add_parser('capture');capture.add_argument('--symbol',required=True)
    capture.add_argument('--start',required=True);capture.add_argument('--end-exclusive',required=True)
    capture.add_argument('--max-pages',type=int,default=3);capture.add_argument('--letter-type',type=int)
    review=commands.add_parser('review');review.add_argument('--study',required=True)
    args=parser.parse_args(argv)
    if args.command=='review':result=review_search(args.study)
    else:
        search_request(args.symbol,start=args.start,end_exclusive=args.end_exclusive,
            cutoff=settings.RESEARCH_CONFIG['holdout_start'],letter_type=args.letter_type)
        if not 1<=args.max_pages<=10:parser.error('ظرفیت صفحه باید یک تا ده باشد')
        from data_collector.codal import CodalProvider
        from database.db import DatabaseManager
        if not settings.DB_PATH.is_file():parser.error('دیتابیس موجود لازم است')
        with DatabaseManager(read_only=True) as db:
            item=db.conn.execute('SELECT symbol_fa,ins_code,instrument_type_code FROM symbols WHERE symbol_fa=?',
                (normalize_symbol(args.symbol),)).fetchone()
        instrument=dict(item) if item is not None else None
        _requests(instrument,args.start,args.end_exclusive,settings.RESEARCH_CONFIG['holdout_start'],args.max_pages,args.letter_type)
        provider=CodalProvider()
        try:result=capture_search(provider,instrument,start=args.start,end_exclusive=args.end_exclusive,
            max_pages=args.max_pages,letter_type=args.letter_type)
        finally:provider.session.close()
    print(json.dumps(result,ensure_ascii=True,indent=2,allow_nan=False))


if __name__=='__main__':main()
