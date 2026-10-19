"""آرشیو بایتی تغییر تعداد سهام و مقایسه محدود نسبت سرمایه؛ بدون تغییر بازار یا حساب."""

import argparse
import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.share_changes import (
    FALSE_FLAGS,
    MAX_BYTES,
    OBSERVATION_KEYS,
    VERSION,
    assess_share_changes,
    compare_capital_ratio,
    share_change_request,
    stamp,
    window,
)
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from ops.capital_actions import review_capital_study
from ops.report_files import private_report_path, read_private_report, write_private_report

EXTRA_FALSE = ('account_data_read', 'database_writes', 'independent_strategy_test')
PLAN_KEYS = {'version','study_id','registered_at','start','end_exclusive','cutoff','requests','comparison',
             'code_sha256','metadata_source',*FALSE_FLAGS,*EXTRA_FALSE}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _current_hash(name):
    return hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _source_names():
    names={'config/default.yaml','config/production.yaml','requirements.lock.txt','pyproject.toml',
           'ops/share_changes.py','ops/capital_actions.py','ops/codal_documents.py','ops/report_files.py'}
    for directory in ('core','data_collector','config'):
        names.update(p.relative_to(settings.BASE_DIR).as_posix() for p in (settings.BASE_DIR/directory).rglob('*.py'))
    return names


def _window(start,end,cutoff):
    window(start,end,cutoff)
    if cutoff>settings.RESEARCH_CONFIG['holdout_start'] or end>now_tehran_iso()[:10]:
        raise ValueError('پنجره مطالعه از مرز مهروموم/امروز نمی‌گذرد')


def _requests(instruments):
    if not isinstance(instruments,list) or not 1<=len(instruments)<=10:
        raise ValueError('یک تا ده سهم عادی صریح لازم است')
    requests=[]
    for item in instruments:
        symbol=item.get('symbol_fa') if isinstance(item,dict) else None
        kind=item.get('instrument_type_code') if isinstance(item,dict) else None
        if (not isinstance(symbol,str) or not normalize_symbol(symbol) or len(symbol)>64
                or any(c in symbol for c in ('\x00','\r','\n')) or type(kind) is not int or kind not in (300,303)):
            raise ValueError('نماد و نوع فعلی سهام عادی لازم‌اند؛ عضویت تاریخی هنوز تأیید نیست')
        code,url=share_change_request(item.get('ins_code'))
        requests.append(dict(symbol_fa=normalize_symbol(symbol),ins_code=code,instrument_type_code=kind,url=url))
    if len({r['symbol_fa'] for r in requests})!=len(requests) or len({r['ins_code'] for r in requests})!=len(requests):
        raise ValueError('نماد یا شناسه تکراری مجاز نیست')
    return requests


def _all_files(folder):
    files={}
    for path in folder.rglob('*'):
        if not path.resolve().is_relative_to(folder):
            raise ValueError('مسیر فایل یا پوشه از پرونده خارج است')
        if path.is_file() and path!=folder/'manifest.json':
            files[path.relative_to(folder).as_posix()]=_sha(path)
    return files


def _capital(folder):
    folder=Path(folder).resolve()
    if not re.fullmatch(r'CAPITAL-[a-f0-9]{32}',folder.name):
        raise ValueError('شناسه مطالعه سرمایه معتبر نیست')
    timing=read_private_report(folder/'result.json')
    if stamp(timing['available_at'])>stamp(now_tehran_iso()):
        raise ValueError('شاهد سرمایه هنوز در دسترس نبوده است')
    result=review_capital_study(folder)
    plan=read_private_report(folder/'plan.json')
    for name in ('core/capital_actions.py','core/dividend_evidence.py','core/text.py','core/timeutils.py'):
        current=hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        if current!=plan['code'][name]:
            raise ValueError('منطق تفسیر سند با نسخه محفوظ برابر نیست')
    return result,plan


def _capital_files(plan):
    return {'manifest.json','plan.json','result.json',*plan['inputs'],*('code/'+n for n in plan['code'])}


def _assess(observation,body,request,plan):
    try:
        return assess_share_changes(observation,body,request['ins_code'],start=plan['start'],
            end_exclusive=plan['end_exclusive'],cutoff=plan['cutoff'])
    except ValueError:
        return dict(version=VERSION,status='invalid_response',**dict.fromkeys(FALSE_FLAGS,False))


def _comparison(plan,cases,source):
    contract=plan['comparison']
    if contract is None:
        return None
    if not source or source['facts']['symbol']!=contract['symbol_fa']:
        raise ValueError('هویت منبع سرمایه با مقایسه برابر نیست')
    matching=[c for c in cases if c['request']['ins_code']==contract['ins_code']
              and c['request']['symbol_fa']==contract['symbol_fa']]
    if len(matching)!=1:
        raise ValueError('منبع سرمایه باید با دقیقاً یک ابزار برنامه متناظر باشد')
    return compare_capital_ratio(matching[0]['assessment'],source['facts'],contract['native_event_date'])


def capture_share_changes(provider,instruments,*,start,end_exclusive,capital_study=None,native_event_date=None):
    cutoff=settings.RESEARCH_CONFIG['holdout_start']
    _window(start,end_exclusive,cutoff)
    requests=_requests(instruments)
    if (capital_study is None)!=(native_event_date is None):
        raise ValueError('مطالعه سرمایه و تاریخ بومی مقایسه باید با هم داده شوند')
    source,source_plan=None,None
    if capital_study is not None:
        if not isinstance(native_event_date,str) or not start<=native_event_date<end_exclusive:
            raise ValueError('تاریخ بومی مقایسه باید در پنجره باشد')
        window(native_event_date,end_exclusive,cutoff)
        source,source_plan=_capital(capital_study)
        if len([r for r in requests if r['symbol_fa']==source['facts']['symbol']])!=1:
            raise ValueError('نماد سند سرمایه در برنامه وجود ندارد')
    folder=(settings.DATA_DIR/'private'/'share-changes'/('SHARES-'+uuid.uuid4().hex)).resolve()
    private_report_path(folder/'plan.json',forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True,exist_ok=False)
    comparison=None
    if source is not None:
        original=Path(capital_study).resolve();relative='capital/'+original.name
        for name in _capital_files(source_plan):
            path=(original/name).resolve()
            if not path.is_relative_to(original) or not path.is_file():
                raise ValueError('ورودی سرمایه از ریشه منبع خارج است')
            target=folder/relative/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(path.read_bytes())
        copied,copied_plan=_capital(folder/relative)
        if copied!=source or copied_plan!=source_plan:
            raise ValueError('منبع سرمایه هنگام کپی عوض شد')
        request=next(r for r in requests if r['symbol_fa']==source['facts']['symbol'])
        comparison=dict(symbol_fa=request['symbol_fa'],ins_code=request['ins_code'],native_event_date=native_event_date,
            capital_folder=relative,capital_manifest_sha256=_sha(folder/relative/'manifest.json'))
    code={}
    for name in sorted(_source_names()):
        target=folder/'code'/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n'));code[name]=_sha(target)
    plan=dict(version=VERSION,study_id=folder.name,registered_at=now_tehran_iso(),start=start,end_exclusive=end_exclusive,
        cutoff=cutoff,requests=requests,comparison=comparison,code_sha256=code,
        metadata_source='caller_supplied_current_ordinary_metadata',**dict.fromkeys((*FALSE_FLAGS,*EXTRA_FALSE),False))
    if source is not None and stamp(source['available_at'])>stamp(plan['registered_at']):
        raise ValueError('سرمایه پیش از ثبت در دسترس نبوده است')
    write_private_report(folder/'plan.json',plan)
    cases=[]
    try:
        for index,request in enumerate(requests):
            observation,body,error_type=None,None,None
            try:
                observed=provider.get_share_change_observation(request['ins_code'])
                if observed is not None:
                    if not isinstance(observed,dict) or set(observed)!=OBSERVATION_KEYS|{'content'}:
                        raise ValueError('قرارداد رسید بایتی معتبر نیست')
                    observation={k:v for k,v in observed.items() if k!='content'}
                    body=observed['content']
                    if not isinstance(body,bytes) or len(body)>MAX_BYTES:
                        raise ValueError('بایت خام لازم است')
            except (NotImplementedError,OSError,ValueError) as error:
                category='NotImplementedError' if isinstance(error,NotImplementedError) else 'OSError' if isinstance(error,OSError) else 'ValueError'
                observation,body,error_type=None,None,category
            filename=f'observation-{index:03d}.json';body_name=None
            if body is not None:
                body_name=f'raw/observation-{index:03d}.json'
                (folder/'raw').mkdir(exist_ok=True);(folder/body_name).write_bytes(body)
            write_private_report(folder/filename,dict(observation=observation,error_type=error_type))
            if observation and not stamp(plan['registered_at'])<=stamp(observation['requested_at'])<=stamp(observation['received_at'])<=stamp(now_tehran_iso()):
                raise ValueError('ساعت رسید از پنجره ثبت خارج است')
            assessed=_assess(observation,body,request,plan)
            cases.append(dict(request=request,observation_file=filename,body_file=body_name,assessment=assessed))
        result=dict(version=VERSION,study_id=folder.name,completed_at=now_tehran_iso(),cases=cases,
            status_counts=dict(Counter(c['assessment']['status'] for c in cases)),comparison=_comparison(plan,cases,source),
            **dict.fromkeys((*FALSE_FLAGS,*EXTRA_FALSE),False))
        if any(_current_hash(n)!=h for n,h in code.items()):
            raise ValueError('کد در میانه دریافت عوض شد؛ تلاش ناقص محفوظ است')
        write_private_report(folder/'result.json',result)
        write_private_report(folder/'manifest.json',dict(version=VERSION,files=_all_files(folder)))
        review_share_changes(folder)
        return dict(folder=str(folder),**result)
    except BaseException:
        write_private_report(folder/'failed.json',dict(completed=len(cases),planned=len(requests),complete=False))
        raise


def review_share_changes(folder):
    folder=Path(folder).resolve()
    if not re.fullmatch(r'SHARES-[a-f0-9]{32}',folder.name):
        raise ValueError('شناسه پرونده معتبر نیست')
    manifest=read_private_report(folder/'manifest.json')
    if manifest.get('version')!=VERSION or manifest.get('files')!=_all_files(folder):
        raise ValueError('فهرست/هش همه ورودی‌ها و خروجی‌ها برابر نیست')
    plan,result=[read_private_report(folder/n) for n in ('plan.json','result.json')]
    if (set(plan)!=PLAN_KEYS or plan.get('version')!=VERSION or plan.get('study_id')!=folder.name
            or plan.get('metadata_source')!='caller_supplied_current_ordinary_metadata'
            or any(plan.get(k) is not False for k in (*FALSE_FLAGS,*EXTRA_FALSE))):
        raise ValueError('قرارداد یا حدود شاهد برنامه تغییر کرده است')
    _window(plan['start'],plan['end_exclusive'],plan['cutoff'])
    if _canonical(_requests(plan['requests']))!=_canonical(plan['requests']):
        raise ValueError('دامنه برنامه برابر نیست')
    if set(plan['code_sha256'])!=_source_names():
        raise ValueError('مخرج کد محفوظ کامل نیست')
    capture_current=True
    for name,digest in plan['code_sha256'].items():
        if _sha(folder/'code'/name)!=digest:
            raise ValueError('هش نسخه کد دریافت محفوظ برابر نیست')
        matches=_current_hash(name)==digest
        capture_current &= matches
        # دریافت/تنظیمات غیرمالی می‌توانند اصلاح شوند؛ منطق مصرف خام و شاهد اولیه ثابت است.
        semantic=(name.startswith('core/') or name in ('ops/report_files.py','ops/capital_actions.py',
            'ops/codal_documents.py','config/settings.py','requirements.lock.txt'))
        if semantic and not matches:
            raise ValueError('منطق تفسیر محفوظ عوض شده؛ بررسی با نسخه خودش یا بازتفسیر صریح لازم است')
    expected={'plan.json','result.json',*('code/'+n for n in plan['code_sha256'])}
    source=None
    contract=plan['comparison']
    if contract is not None:
        if (set(contract)!={'symbol_fa','ins_code','native_event_date','capital_folder','capital_manifest_sha256'}
                or not re.fullmatch(r'capital/CAPITAL-[a-f0-9]{32}',contract['capital_folder'])):
            raise ValueError('قرارداد مقایسه سرمایه معتبر نیست')
        window(contract['native_event_date'],plan['end_exclusive'],plan['cutoff'])
        if contract['native_event_date']<plan['start']:
            raise ValueError('روز مقایسه پیش از پنجره است')
        source,source_plan=_capital(folder/contract['capital_folder'])
        if (_sha(folder/contract['capital_folder']/'manifest.json')!=contract['capital_manifest_sha256']
                or stamp(source['available_at'])>stamp(plan['registered_at'])):
            raise ValueError('منبع سرمایه به برنامه پیوند ندارد')
        expected.update(contract['capital_folder']+'/'+n for n in _capital_files(source_plan))
    if (set(result)!={'version','study_id','completed_at','cases','status_counts','comparison',*FALSE_FLAGS,*EXTRA_FALSE}
            or result.get('version')!=VERSION or result.get('study_id')!=folder.name
            or any(result.get(k) is not False for k in (*FALSE_FLAGS,*EXTRA_FALSE))
            or not stamp(plan['registered_at'])<=stamp(result['completed_at'])<=stamp(now_tehran_iso())
            or len(result['cases'])!=len(plan['requests'])):
        raise ValueError('قرارداد یا زمان نتیجه برابر نیست')
    cases=[]
    for index,(request,case) in enumerate(zip(plan['requests'],result['cases'])):
        filename=f'observation-{index:03d}.json';expected.add(filename)
        saved=read_private_report(folder/filename)
        if set(saved)!={'observation','error_type'} or saved['error_type'] not in (None,'NotImplementedError','ValueError','OSError'):
            raise ValueError('قرارداد تلاش برابر نیست')
        observation=saved['observation'];body_name=None;body=None
        if observation is not None:
            if saved['error_type'] is not None:
                raise ValueError('خطا و رسید موفق با هم معتبر نیستند')
            body_name=f'raw/observation-{index:03d}.json';expected.add(body_name)
            body=(folder/body_name).read_bytes()
            if not stamp(plan['registered_at'])<=stamp(observation['requested_at'])<=stamp(observation['received_at'])<=stamp(result['completed_at']):
                raise ValueError('زمان تلاش بیرون پنجره ثبت است')
        actual=dict(request=request,observation_file=filename,body_file=body_name,assessment=_assess(observation,body,request,plan))
        if _canonical(actual)!=_canonical(case):
            raise ValueError('بازحساب تلاش با نتیجه برابر نیست')
        cases.append(actual)
    if (set(manifest['files'])!=expected or result['status_counts']!=dict(Counter(c['assessment']['status'] for c in cases))
            or _canonical(result['comparison'])!=_canonical(_comparison(plan,cases,source))
            or manifest['files']!=_all_files(folder)):
        raise ValueError('مخرج پرونده، خلاصه یا مقایسه بازحساب نشد')
    return dict(artifacts_verified=True,assessments_recomputed=True,semantic_code_matches_current=True,
        capture_code_matches_current=bool(capture_current),database_read=False,network_requested=False,
        independent_registration_verified=False,external_authenticity_verified=False,**result)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    capture=commands.add_parser('capture');capture.add_argument('--symbols',nargs='+',required=True)
    capture.add_argument('--start',required=True);capture.add_argument('--end-exclusive',required=True)
    capture.add_argument('--capital-study');capture.add_argument('--native-event-date')
    review=commands.add_parser('review');review.add_argument('--study',required=True)
    args=parser.parse_args(argv)
    if args.command=='review':
        result=review_share_changes(args.study)
    else:
        _window(args.start,args.end_exclusive,settings.RESEARCH_CONFIG['holdout_start'])
        if not 1<=len(args.symbols)<=10:
            parser.error('یک تا ده نماد صریح لازم است')
        from data_collector.tsetmc import TsetmcProvider
        from database.db import DatabaseManager

        if not settings.DB_PATH.is_file():
            parser.error('دیتابیس موجود لازم است؛ پایگاه خالی ساخته نمی‌شود')
        with DatabaseManager(read_only=True) as db:
            instruments=[db.get_symbol(s) for s in args.symbols]
        _requests(instruments)
        provider=TsetmcProvider(realtime=True)
        try:
            result=capture_share_changes(provider,instruments,start=args.start,end_exclusive=args.end_exclusive,
                capital_study=args.capital_study,native_event_date=args.native_event_date)
        finally:
            provider.session.close()
    print(json.dumps(result,ensure_ascii=True,indent=2,allow_nan=False))


if __name__=='__main__':main()
