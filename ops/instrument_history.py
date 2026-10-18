"""آرشیو محدود و فقط‌خواندنی مشخصات تاریخی؛ داده بازار یا فیچر موجود را تغییر نمی‌دهد."""

import hashlib
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.instrument_history import VERSION, assess_instrument_history, history_request, receipt_time
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report

CODE_FILES = ('core/instrument_history.py', 'data_collector/base.py', 'data_collector/tsetmc.py',
              'ops/instrument_history.py', 'ops/report_files.py', 'core/timeutils.py', 'core/text.py',
              'config/settings.py', 'config/default.yaml', 'config/production.yaml', 'requirements.lock.txt', 'brs.py')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _requests(instruments, dates, cutoff):
    if (not isinstance(instruments, list) or not instruments or len(instruments) > 20
            or not isinstance(dates, list) or not dates or len(dates) > 20
            or len(instruments) * len(dates) > 100 or len(set(dates)) != len(dates)
            or not is_canonical_iso_date(cutoff)):
        raise ValueError('حداکثر ۲۰ نماد، ۲۰ تاریخ یکتا و ۱۰۰ درخواست مجاز است')
    for day in dates:
        if not is_canonical_iso_date(day) or day >= cutoff or day > now_tehran_iso()[:10]:
            raise ValueError('این ابزار پژوهشی فقط تاریخ استاندارد پیش از مرز مهروموم را می‌پذیرد')
    requests = []
    codes = set()
    for instrument in instruments:
        if type(instrument.get('instrument_type_code')) is not int or instrument['instrument_type_code'] not in (300, 303):
            raise ValueError('فقط ابزار ثبت‌شده سهام عادی بورس/فرابورس پذیرفته است')
        code, _ = history_request(instrument.get('ins_code'), dates[0])
        symbol = normalize_symbol(instrument.get('symbol_fa', ''))
        if not symbol or symbol[-1:].isdigit() or code in codes:
            raise ValueError('نام نماد، تابلو یا شناسه تکراری/نامعتبر است')
        codes.add(code)
        for day in sorted(dates):
            requests.append(dict(symbol=symbol, ins_code=code, requested_date=day,
                                 current_instrument_type_code=instrument['instrument_type_code']))
    return requests


def capture_instrument_history(provider, instruments, dates, *, root=None):
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    requests = _requests(instruments, dates, cutoff)
    folder = Path(root or settings.DATA_DIR / 'private' / 'instrument-history') / ('HISTORY-' + uuid.uuid4().hex)
    private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    code = {}
    for name in CODE_FILES:
        content = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        code[name] = _sha(target)
    plan = dict(version=VERSION, registered_at=now_tehran_iso(), cutoff=cutoff, requests=requests, code=code,
        database_writes=False, feature_eligible=False, historical_membership_verified=False,
        accepted_for_signals=False, original_publication_time=None)
    write_private_report(folder / 'plan.json', plan)
    cases, files = [], {}
    write_private_report(folder / 'status.json', dict(status='running', completed=0, planned=len(requests)))
    try:
        for index, request in enumerate(requests):
            error_type = None
            try:
                observation = provider.get_instrument_history_observation(request['ins_code'], request['requested_date'])
            except (NotImplementedError, ValueError, OSError) as error:
                observation, error_type = None, type(error).__name__
            filename = f'observation-{index:03d}.json'
            write_private_report(folder / filename, dict(observation=observation, error_type=error_type))
            files[filename] = _sha(folder / filename)
            assessed = assess_instrument_history(observation, request['ins_code'], request['requested_date'])
            if observation and receipt_time(observation['received_at']) < receipt_time(plan['registered_at']):
                raise ValueError('زمان دریافت پیش از ثبت برنامه است؛ مشاهده منتشر نشد')
            cases.append(dict(request=request, assessment=assessed, observation_file=filename))
            write_private_report(folder / 'status.json', dict(status='running', completed=len(cases), planned=len(requests)))
        for name, digest in code.items():
            if hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != digest:
                raise ValueError('کد در میانه دریافت تغییر کرده است؛ تلاش ناقص حفظ شد')
        result = dict(version=VERSION, completed_at=now_tehran_iso(), cases=cases,
            status_counts=dict(Counter(c['assessment']['status'] for c in cases)),
            candidate_fields_complete=sum(c['assessment']['candidate_fields_complete'] for c in cases),
            feature_eligible=False, accepted_for_signals=False, historical_membership_verified=False)
        write_private_report(folder / 'result.json', result)
        write_private_report(folder / 'manifest.json', dict(version=VERSION, code=code, observations=files,
            plan_sha256=_sha(folder / 'plan.json'), result_sha256=_sha(folder / 'result.json')))
        load_instrument_history(folder)
        write_private_report(folder / 'status.json', dict(status='complete', completed=len(cases), planned=len(requests)))
        return dict(folder=str(folder), **result)
    except BaseException:
        write_private_report(folder / 'status.json', dict(status='failed_or_interrupted', completed=len(cases), planned=len(requests)))
        raise


def load_instrument_history(folder):
    folder = Path(folder)
    if len(folder.name) != 40 or not folder.name.startswith('HISTORY-'):
        raise ValueError('شناسه آرشیو مشخصات تاریخی نامعتبر است')
    try:
        uuid.UUID(hex=folder.name[8:])
    except ValueError as exc:
        raise ValueError('شناسه آرشیو مشخصات تاریخی نامعتبر است') from exc
    manifest = read_private_report(folder / 'manifest.json')
    plan = read_private_report(folder / 'plan.json')
    result = read_private_report(folder / 'result.json')
    if (manifest['version'] != VERSION or plan['version'] != VERSION or result['version'] != VERSION
            or not is_canonical_iso_date(plan['cutoff']) or plan['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or _sha(folder / 'plan.json') != manifest['plan_sha256']
            or _sha(folder / 'result.json') != manifest['result_sha256'] or manifest['code'] != plan['code']):
        raise ValueError('نسخه، مرز یا هش آرشیو ناسازگار است')
    if (set(manifest['code']) != set(CODE_FILES) or any(_sha(folder / 'code' / name) != digest for name, digest in manifest['code'].items())
            or {p.relative_to(folder / 'code').as_posix() for p in (folder / 'code').rglob('*') if p.is_file()} != set(CODE_FILES)):
        raise ValueError('کد محفوظ آرشیو ناسازگار است')
    requests, cases = plan['requests'], result['cases']
    if not requests or len(requests) > 100 or len(cases) != len(requests):
        raise ValueError('مخرج درخواست‌ها یا تلاش‌ها ناسازگار است')
    keys = {(r['ins_code'], r['requested_date']) for r in requests}
    if len(keys) != len(requests) or any(not is_canonical_iso_date(day) or day >= plan['cutoff'] for _, day in keys):
        raise ValueError('تاریخ مهروموم یا درخواست تکراری در آرشیو')
    instruments = {}
    for request in requests:
        item = dict(symbol_fa=request['symbol'], ins_code=request['ins_code'], instrument_type_code=request['current_instrument_type_code'])
        if request['ins_code'] in instruments and instruments[request['ins_code']] != item:
            raise ValueError('هویت ابزار در دامنه درخواست‌ها تغییر کرده است')
        instruments[request['ins_code']] = item
    if _requests(list(instruments.values()), sorted({r['requested_date'] for r in requests}), plan['cutoff']) != requests:
        raise ValueError('دامنه کامل نماد/تاریخ با برنامه محفوظ برابر نیست')
    expected_files = {f'observation-{index:03d}.json' for index in range(len(requests))}
    if set(manifest['observations']) != expected_files or {p.name for p in folder.glob('observation-*')} != expected_files:
        raise ValueError('فایل تلاش از دامنه آرشیو حذف یا اضافه شده است')
    for index, (request, case) in enumerate(zip(requests, cases)):
        filename = f'observation-{index:03d}.json'
        if case['request'] != request or case['observation_file'] != filename or _sha(folder / filename) != manifest['observations'][filename]:
            raise ValueError('هویت یا هش تلاش ناسازگار است')
        observation = read_private_report(folder / filename)['observation']
        if case['assessment'] != assess_instrument_history(observation, request['ins_code'], request['requested_date']):
            raise ValueError('ارزیابی با پاسخ محفوظ سازگار نیست')
        if observation and not receipt_time(plan['registered_at']) <= receipt_time(observation['received_at']) <= receipt_time(result['completed_at']):
            raise ValueError('زمان مشاهده خارج از پنجره ثبت است')
    if (result['status_counts'] != dict(Counter(c['assessment']['status'] for c in cases))
            or result['candidate_fields_complete'] != sum(c['assessment']['candidate_fields_complete'] for c in cases)
            or any(value is not False for value in (result['feature_eligible'], result['accepted_for_signals'], result['historical_membership_verified'],
                plan['database_writes'], plan['feature_eligible'], plan['accepted_for_signals'], plan['historical_membership_verified']))
            or plan['original_publication_time'] is not None):
        raise ValueError('خلاصه یا حدود شاهد ناسازگار است')
    return dict(plan=plan, artifacts_verified=True, **result)
