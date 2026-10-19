"""چهار سناریوی حسابداری ثابت از قیمت واقعی؛ نسبت سرمایه، حق واقعی سهم فرض نمی‌شود."""

import argparse
import hashlib
import json
import re
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from backtest.bonus_shares import BonusCapitalization, replay_bonus_share_portfolio
from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.capital_actions import _clock, review_capital_study
from ops.report_files import private_report_path, read_private_report, write_private_report
from research.cash_dividend_case import _load_prices, _sha, _summary
from research.cash_dividend_case import _source_names as _base_source_names
from research.run import _json_safe, archive_research_code

VERSION = 'bonus-share-accounting-case-v1'
MODES = ('legacy_normal', 'uncredited_bonus_normal', 'legacy_stress', 'uncredited_bonus_stress')
FALSE_FLAGS = ('actual_share_ratio_verified', 'historical_entitlement_mapping_verified', 'holder_credit_verified',
               'historical_identity_verified', 'independent_strategy_test', 'accepted_for_signals',
               'created_market_data', 'account_data_read', 'historical_fees_verified')
PRICE_COLUMNS = ('symbol', 'date', 'open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
                 'ref_price_raw', 'volume', 'value', 'trade_count')
RATIO_SOURCE = 'registered-capital ratio scenario; actual ordinary-share entitlement not proven'
SELECTION = 'one fixed decision before the prior-selected accounting event; no rank or performance search'
CALENDAR_SOURCE = 'observed ordinary-equity price dates; not verified historical membership or halt evidence'
PROTOCOL_KEYS = {'version','experiment_id','registered_at','cutoff','modes','config','fees','event','releases',
    'settlements','dates','source_folder','source_manifest_sha256','input_sha256','code_sha256',
    'share_ratio_source','selection','calendar_source',*FALSE_FLAGS}


def _canonical(value):
    return json.dumps(_json_safe(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _source_names():
    names = _base_source_names()
    for directory in ('ops','data_collector'):
        names.update(p.relative_to(settings.BASE_DIR).as_posix() for p in (settings.BASE_DIR/directory).rglob('*.py'))
    return names


def _archive_code(folder):
    code = archive_research_code(folder)
    for name in _source_names()-set(code):
        target=folder/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n'))
        code[name]=_sha(target)
    return code


def _dates(decision, previous, record, ex, end, cutoff):
    dates = (decision, previous, record, ex, end)
    if (not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']
            or any(not is_canonical_iso_date(d) for d in dates) or any(d >= cutoff for d in dates[:-1])
            or end > cutoff or not decision < previous <= record < ex < end):
        raise ValueError('تاریخ‌های سناریو باید مرتب و تماماً پیش از مرز مهروموم باشند')


def _source(folder):
    folder = Path(folder).resolve()
    if not re.fullmatch(r'CAPITAL-[a-f0-9]{32}', folder.name):
        raise ValueError('شناسه مطالعه ثبت سرمایه معتبر نیست')
    timing=read_private_report(folder/'result.json')
    if _clock(timing['available_at'])>_clock(now_tehran_iso()):
        raise ValueError('مطالعه سرمایه در زمان فعلی در دسترس نبوده است')
    result = review_capital_study(folder)
    facts = result['facts']
    if facts['status'] != 'supported_primary_fact' or facts['retained_equals_increase'] is not True:
        raise ValueError('سناریوی محدود فقط آگهی ثبت با سود انباشته برابر افزایش را می‌پذیرد؛ این، اثبات حق سهم نیست')
    plan = read_private_report(folder/'plan.json')
    # نسخه تنظیمات ممکن است متفاوت باشد؛ خود منطق مالی/تاریخ نباید بی‌صدا عوض شده باشد.
    for name in ('core/capital_actions.py', 'core/dividend_evidence.py', 'core/text.py', 'core/timeutils.py'):
        if hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != plan['code'][name]:
            raise ValueError('منطق تفسیر سند با نسخه محفوظ برابر نیست')
    return result, plan


def _event(source, source_sha, prices, *, previous, record, ex):
    facts = source['facts']
    if facts['meeting_date'] != record:
        raise ValueError('روز مشمول سناریو باید همان تاریخ مجمع اظهارشده باشد؛ انطباق حقوقی همچنان تأیید نیست')
    before, after = prices.loc[prices.date.eq(previous)], prices.loc[prices.date.eq(ex)]
    if len(before) != 1 or len(after) != 1 or prices.loc[prices.date.lt(ex)].date.max() != previous:
        raise ValueError('دو جلسه قیمت مجاورِ مشاهده‌شده با ورودی صریح برابر نیستند')
    ratio = facts['increase_ratio']
    return BonusCapitalization('CAPITAL-'+record, facts['symbol'], record, ex,
        ratio['numerator'], ratio['denominator'], previous, str(before.iloc[0].close_final_raw),
        str(after.iloc[0].ref_price_raw), source_sha, source['available_at'])


def _compare(prices, calendar, selections, event, config):
    outputs = {}
    for stress, suffix in ((False, 'normal'), (True, 'stress')):
        cfg = replace(config, stress=stress)
        outputs['legacy_'+suffix] = replay_portfolio(prices, selections, calendar, cfg)
        outputs['uncredited_bonus_'+suffix] = replay_bonus_share_portfolio(prices, selections, calendar, cfg,
            events=[event], releases=[], settlements=[])
    return outputs


def _case_config():
    return PortfolioConfig(100_000_000, horizon=2, max_positions=1, participation=.05,
        slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'], max_loss_pct=.1)


def _source_file_names(plan):
    return {'manifest.json','plan.json','result.json',*plan['inputs'],*('code/'+n for n in plan['code'])}


def _expected_files(protocol, source_plan):
    names={'protocol.json','result.json','prices.parquet','selections.parquet','calendar.json'}
    names.update(f'{mode}-{label}.parquet' for mode in MODES for label in ('equity','executions','decisions'))
    names.update('code/'+n for n in protocol['code_sha256'])
    names.update(protocol['source_folder']+'/'+n for n in _source_file_names(source_plan))
    return names


def _all_files(folder):
    files = {}
    for path in folder.rglob('*'):
        if not path.is_file() or path == folder/'manifest.json':
            continue
        if not path.resolve().is_relative_to(folder):
            raise ValueError('مسیر پرونده از ریشه خارج است')
        files[path.relative_to(folder).as_posix()] = _sha(path)
    return files


def run_bonus_share_case(db, *, capital_study, decision_date, previous_price_session, record_date, ex_date, end_exclusive):
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    _dates(decision_date, previous_price_session, record_date, ex_date, end_exclusive, cutoff)
    source, source_plan = _source(capital_study)
    facts = source['facts']
    if facts['registration_date'] >= end_exclusive:
        raise ValueError('پنجره ثابت سناریو باید ثبت اظهارشده را در بر بگیرد')
    meta = db.get_symbol(facts['symbol'])
    if meta is None or meta['instrument_type_code'] not in (300,303):
        raise ValueError('نماد باید در فراداده فعلی سهم عادی باشد؛ هویت تاریخی هنوز تأیید نیست')
    own = not db.conn.in_transaction
    if own:
        db.conn.execute('BEGIN')
    try:
        prices = pd.read_sql_query('SELECT '+','.join(PRICE_COLUMNS[1:])+' FROM daily_prices WHERE symbol_id=? '
            'AND source=? AND date>=? AND date<? ORDER BY date', db.conn,
            params=(meta['id'], 'tsetmc', decision_date, end_exclusive))
        calendar = [r[0] for r in db.conn.execute('SELECT DISTINCT p.date FROM daily_prices p JOIN symbols s ON s.id=p.symbol_id '
            'WHERE p.source=? AND s.instrument_type_code IN(300,303) AND p.date>=? AND p.date<? ORDER BY p.date',
            ('tsetmc', decision_date, end_exclusive))]
    finally:
        if own:
            db.conn.rollback()
    if (prices.empty or decision_date not in set(prices.date) or ex_date not in calendar
            or not calendar or calendar[0] != decision_date):
        raise ValueError('قیمت و تقویم مشاهده‌شده کافی نیست؛ روز غایب به روز توقف تبدیل نمی‌شود')
    prices.insert(0, 'symbol', facts['symbol'])
    selections = pd.DataFrame([dict(date=decision_date, symbol=facts['symbol'], rank=0)])
    cfg = _case_config()
    original = Path(capital_study).resolve()
    source_sha = _sha(original/'manifest.json')
    event = _event(source, source_sha, prices, previous=previous_price_session, record=record_date, ex=ex_date)
    identifier = f'EXP-{now_tehran_iso()[:10]}-BONUSRIGHTS-{uuid.uuid4().hex}'
    folder = settings.DATA_DIR/'private'/'bonus-share-cases'/identifier
    private_report_path(folder/'protocol.json', forbidden_paths=[settings.DB_PATH])
    folder.mkdir(parents=True, exist_ok=False)
    source_relative = 'capital/'+original.name
    copied = folder/source_relative
    names = _source_file_names(source_plan)
    for name in names:
        path = (original/name).resolve()
        if not path.is_relative_to(original) or not path.is_file():
            raise ValueError('مسیر ورودی سرمایه خارج از مطالعه است')
        target = copied/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    copied_source, _ = _source(copied)
    if copied_source != source or _sha(copied/'manifest.json') != source_sha:
        raise ValueError('مطالعه سرمایه در حین کپی تغییر کرد')
    prices.to_parquet(folder/'prices.parquet', index=False)
    selections.to_parquet(folder/'selections.parquet', index=False)
    write_private_report(folder/'calendar.json', dict(dates=calendar))
    inputs = {name:_sha(folder/name) for name in ('prices.parquet','selections.parquet','calendar.json')}
    code = _archive_code(folder/'code')
    registered_at=now_tehran_iso()
    if _clock(source['available_at'])>_clock(registered_at):
        raise ValueError('شاهد در زمان ثبت سناریو در دسترس نبوده است')
    protocol = dict(version=VERSION, experiment_id=identifier, registered_at=registered_at, cutoff=cutoff,
        modes=list(MODES), config=asdict(cfg), fees=dict(settings.FEES),event=asdict(event), releases=[], settlements=[],
        dates=dict(decision=decision_date, previous=previous_price_session, record=record_date, ex=ex_date, end=end_exclusive),
        source_folder=source_relative, source_manifest_sha256=source_sha, input_sha256=inputs, code_sha256=code,
        share_ratio_source=RATIO_SOURCE,selection=SELECTION,calendar_source=CALENDAR_SOURCE,
        **dict.fromkeys(FALSE_FLAGS,False))
    write_private_report(folder/'protocol.json', protocol)
    encoded = json.dumps(protocol,ensure_ascii=False,allow_nan=False)
    common = dict(symbols_tested=event.symbol, start_date=decision_date, end_date=end_exclusive,
        dataset_version=source_sha, features_version='raw_prices_only', model_version=VERSION)
    if db.log_experiment(experiment_id=identifier+'-PROTOCOL',strategy_name='bonus-share-accounting-protocol',
        parameters_json=encoded,metrics_json='{}',**common) is None:
        raise RuntimeError('ثبت رسمی سناریو پیش از محاسبه انجام نشد')
    outputs = _compare(prices, calendar, selections, event, cfg)
    for mode, replay in outputs.items():
        for label in ('equity','executions','decisions'):
            getattr(replay,label).to_parquet(folder/f'{mode}-{label}.parquet',index=False)
    result = dict(version=VERSION,experiment_id=identifier,completed_at=now_tehran_iso(),
        modes=_summary(outputs),accepted_for_signals=False,profitability_proven=False,holdout_opened=False)
    if any(hashlib.sha256((settings.BASE_DIR/n).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=h for n,h in code.items()):
        raise RuntimeError('کد در حین حسابداری تغییر کرد؛ تلاش ناقص محفوظ است')
    if any(_sha(folder/n)!=h for n,h in inputs.items()) or _source(copied)[0]!=source:
        raise RuntimeError('ورودی در حین حسابداری تغییر کرد')
    write_private_report(folder/'result.json',result)
    if db.log_experiment(experiment_id=identifier,strategy_name='bonus-share-accounting-case',parameters_json=encoded,
        metrics_json=json.dumps(result,ensure_ascii=False,allow_nan=False),**common) is None:
        raise RuntimeError('ثبت نتیجه سناریو انجام نشد')
    write_private_report(folder/'manifest.json',dict(version=VERSION,files=_all_files(folder)))
    return dict(folder=str(folder),**result)


def review_bonus_share_case(folder):
    folder = Path(folder).resolve()
    manifest = read_private_report(folder/'manifest.json')
    if manifest.get('version') != VERSION or manifest['files'] != _all_files(folder):
        raise ValueError('فهرست یا هش همه ورودی‌ها و خروجی‌ها برابر نیست')
    protocol = read_private_report(folder/'protocol.json')
    if (set(protocol)!=PROTOCOL_KEYS or protocol.get('version')!=VERSION or protocol.get('experiment_id')!=folder.name
            or protocol.get('modes')!=list(MODES) or protocol.get('releases')!=[] or protocol.get('settlements')!=[]
            or protocol.get('share_ratio_source')!=RATIO_SOURCE or protocol.get('selection')!=SELECTION
            or protocol.get('calendar_source')!=CALENDAR_SOURCE
            or any(protocol.get(key) is not False for key in FALSE_FLAGS)):
        raise ValueError('قرارداد، هویت یا حدود شاهد سناریو تغییر کرده است')
    dates=protocol['dates']
    if set(dates)!={'decision','previous','record','ex','end'}:
        raise ValueError('فهرست تاریخ‌های سناریو باید محدود و صریح باشد')
    _dates(dates['decision'],dates['previous'],dates['record'],dates['ex'],dates['end'],protocol['cutoff'])
    if not re.fullmatch(r'capital/CAPITAL-[a-f0-9]{32}',protocol['source_folder']):
        raise ValueError('مسیر منبع سرمایه معتبر نیست')
    if set(protocol['code_sha256'])!=_source_names():
        raise ValueError('مخرج کد پژوهش ناقص است')
    for name,sha in protocol['code_sha256'].items():
        if (hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=sha
                or _sha(folder/'code'/name)!=sha):
            raise ValueError('بازاجرای سناریو به همان کد محفوظ نیاز دارد')
    source, source_plan = _source(folder/protocol['source_folder'])
    if _sha(folder/protocol['source_folder']/'manifest.json')!=protocol['source_manifest_sha256']:
        raise ValueError('مطالعه سرمایه به پروتکل پیوند ندارد')
    if (set(manifest['files'])!=_expected_files(protocol,source_plan)
            or source['facts']['registration_date']>=dates['end']
            or _clock(source['available_at'])>_clock(protocol['registered_at'])):
        raise ValueError('مخرج فایل‌ها، پنجره ثبت یا تقدم دسترسی برابر نیست')
    prices = _load_prices(folder/'prices.parquet',protocol['cutoff'])
    selections = _load_prices(folder/'selections.parquet',protocol['cutoff'])
    calendar = read_private_report(folder/'calendar.json')['dates']
    if (set(prices.columns)!=set(PRICE_COLUMNS) or set(prices.symbol)!={source['facts']['symbol']}
            or selections.to_dict('records')!=[dict(date=dates['decision'],symbol=source['facts']['symbol'],rank=0)]
            or not calendar or calendar[0]!=dates['decision'] or calendar[-1]>=dates['end']
            or any(not is_canonical_iso_date(d) or d>=protocol['cutoff'] for d in calendar)
            or not prices.date.ge(dates['decision']).all() or not prices.date.lt(dates['end']).all()):
        raise ValueError('دامنه قیمت، انتخاب یا تقویم با سناریوی صریح برابر نیست')
    if set(protocol['input_sha256'])!={'prices.parquet','selections.parquet','calendar.json'} or any(
            _sha(folder/n)!=h for n,h in protocol['input_sha256'].items()):
        raise ValueError('منشأ ورودی با پروتکل برابر نیست')
    event = _event(source,protocol['source_manifest_sha256'],prices,
        previous=dates['previous'],record=dates['record'],ex=dates['ex'])
    if _canonical(asdict(event))!=_canonical(protocol['event']):
        raise ValueError('نسبت یا تاریخ/قیمت سناریو با مطالعه اولیه و خام برابر نیست')
    cfg=_case_config()
    if _canonical(protocol['config'])!=_canonical(asdict(cfg)) or _canonical(protocol.get('fees'))!=_canonical(dict(settings.FEES)):
        raise ValueError('تنظیمات پایه سناریوی ثابت تغییر کرده است')
    result = read_private_report(folder/'result.json')
    if (set(result)!={'version','experiment_id','completed_at','modes','accepted_for_signals','profitability_proven','holdout_opened'}
            or result.get('version')!=VERSION or result.get('experiment_id')!=folder.name
            or any(result.get(key) is not False for key in ('accepted_for_signals','profitability_proven','holdout_opened'))
            or _clock(result['completed_at'])<_clock(protocol['registered_at'])):
        raise ValueError('هویت یا حدود نتیجه سناریو تغییر کرده است')
    outputs = _compare(prices,calendar,selections,event,cfg)
    if _canonical(_summary(outputs))!=_canonical(result['modes']):
        raise ValueError('بازحساب سناریو با نتیجه برابر نیست')
    for mode,replay in outputs.items():
        for label in ('equity','executions','decisions'):
            expected=_load_prices(folder/f'{mode}-{label}.parquet',protocol['cutoff'],allow_empty=True)
            actual=getattr(replay,label)
            if (list(actual.columns)!=list(expected.columns) or
                    _canonical(actual.to_dict('records'))!=_canonical(expected.to_dict('records'))):
                raise ValueError('جدول اجرا یا منحنی با بازپخش برابر نیست')
    if manifest['files']!=_all_files(folder):
        raise ValueError('فایل‌ها در حین بازبینی تغییر کردند')
    return dict(experiment_id=folder.name,replay_verified=True,source_study_verified=True,
        database_read=False,network_requested=False,accepted_for_signals=False,profitability_proven=False,
        independent_registration_verified=False,modes=result['modes'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run')
    for name in ('capital-study','decision-date','previous-price-session','record-date','ex-date','end-exclusive'):
        run.add_argument('--'+name,required=True)
    review=commands.add_parser('review')
    review.add_argument('--study',required=True)
    args=parser.parse_args(argv)
    if args.command=='review':
        result=review_bonus_share_case(args.study)
    else:
        _dates(args.decision_date,args.previous_price_session,args.record_date,args.ex_date,args.end_exclusive,
               settings.RESEARCH_CONFIG['holdout_start'])
        from database.db import DatabaseManager

        if not settings.DB_PATH.is_file():
            parser.error('دیتابیس موجود لازم است؛ پایگاه خالی ساخته نمی‌شود')
        with DatabaseManager() as db:
            result=run_bonus_share_case(db,**{k:v for k,v in vars(args).items() if k!='command'})
    print(json.dumps(result,ensure_ascii=True,indent=2,allow_nan=False))


if __name__=='__main__':main()
