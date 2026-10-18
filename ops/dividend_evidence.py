"""برنامه و پاسخ خام سود نقدی؛ بررسی آفلاین بدون تغییر حساب، فیچر یا بکتست."""

import hashlib
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import settings
from core.dividend_evidence import MAX_BYTES, VERSION, dividend_request, parse_dividend_source
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report

CODE_FILES = ('core/dividend_evidence.py', 'ops/dividend_evidence.py', 'data_collector/base.py',
    'data_collector/tsetmc.py', 'core/text.py', 'core/timeutils.py', 'ops/report_files.py',
    'config/settings.py', 'config/default.yaml', 'config/production.yaml', 'requirements.lock.txt', 'brs.py')
BOUNDARIES = dict(feature_eligible=False, accepted_for_signals=False, economic_entitlements_verified=False,
                  settlement_verified=False, database_writes=False)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('زمان دریافت معتبر نیست')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('زمان باید منطقه زمانی داشته باشد')
    return result


def _scope(instruments):
    if not isinstance(instruments, list) or not 1 <= len(instruments) <= 10:
        raise ValueError('یک تا ده نماد ثبت‌شده لازم است')
    requests, codes, symbols = [], set(), set()
    for item in instruments:
        if not isinstance(item, dict) or type(item.get('instrument_type_code')) is not int or item['instrument_type_code'] not in (300, 303):
            raise ValueError('دامنه فقط سهام عادی بورس و فرابورس است')
        code, symbol, url = dividend_request(item.get('ins_code'), item.get('symbol_fa'))
        if code in codes or symbol in symbols:
            raise ValueError('شناسه یا نام نماد تکراری است')
        codes.add(code)
        symbols.add(symbol)
        requests.append(dict(ins_code=code, symbol=symbol, url=url, current_instrument_type_code=item['instrument_type_code']))
    return requests


def _bytes(path, content):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _assess(folder, request, wrapper, body_name, cutoff, registered, completed):
    observation = wrapper['observation']
    body_path = folder / body_name
    if observation is None:
        if body_path.exists():
            raise ValueError('پاسخ ناموجود نباید فایل محتوای معتبر داشته باشد')
        return dict(status='unavailable', assessment=None)
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc' or any(observation.get(key) != request[key] for key in ('ins_code', 'symbol', 'url'))
            or type(observation.get('response_bytes')) is not int or not 0 <= observation['response_bytes'] <= MAX_BYTES):
        raise ValueError('هویت یا حجم پاسخ با درخواست برابر نیست')
    if not registered <= _stamp(observation['requested_at']) <= _stamp(observation['received_at']) <= completed:
        raise ValueError('ساعت درخواست/دریافت خارج از پنجره برنامه است')
    with body_path.open('rb') as handle:
        content = handle.read(MAX_BYTES + 1)
    if len(content) != observation['response_bytes'] or hashlib.sha256(content).hexdigest() != observation['body_sha256']:
        raise ValueError('هش یا حجم محتوای خام یکسان نیست')
    try:
        assessment = parse_dividend_source(content, end_exclusive=cutoff)
        return dict(status=assessment['status'], assessment=assessment)
    except ValueError:
        return dict(status='invalid_schema', assessment=None)


def capture_dividend_evidence(provider, instruments, *, root=None):
    requests = _scope(instruments)
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if not is_canonical_iso_date(cutoff):
        raise ValueError('مرز مهروموم استاندارد نیست')
    folder = Path(root or settings.DATA_DIR / 'private/dividend-evidence') / ('DIVIDENDS-' + uuid.uuid4().hex)
    private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    code = {}
    for name in CODE_FILES:
        content = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _bytes(target, content)
        code[name] = _sha(target)
    plan = dict(version=VERSION, registered_at=now_tehran_iso(), cutoff=cutoff, requests=requests,
                code=code, **BOUNDARIES)
    write_private_report(folder / 'plan.json', plan)
    files, cases = {}, []
    write_private_report(folder / 'status.json', dict(status='running', planned=len(requests), completed=0))
    try:
        for index, request in enumerate(requests):
            error_type = None
            try:
                observation = provider.get_dividend_observation(request['ins_code'], request['symbol'])
            except (NotImplementedError, ValueError, OSError) as error:
                observation, error_type = None, type(error).__name__
            body_name, metadata_name = f'body-{index:03d}.bin', f'observation-{index:03d}.json'
            if observation is not None:
                observation = dict(observation)
                content = observation.pop('content')
                if not isinstance(content, bytes) or len(content) > MAX_BYTES:
                    raise ValueError('پاسخ باید بایت محدود باشد')
                _bytes(folder / body_name, content)
                files[body_name] = _sha(folder / body_name)
            wrapper = dict(observation=observation, error_type=error_type)
            write_private_report(folder / metadata_name, wrapper)
            files[metadata_name] = _sha(folder / metadata_name)
            assessed = _assess(folder, request, wrapper, body_name, cutoff, _stamp(plan['registered_at']), _stamp(now_tehran_iso()))
            cases.append(dict(request=request, observation_file=metadata_name,
                              body_file=body_name if observation is not None else None, **assessed))
            write_private_report(folder / 'status.json', dict(status='running', planned=len(requests), completed=len(cases)))
        if code != {name:hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() for name in CODE_FILES}:
            raise ValueError('کد در حین دریافت تغییر کرد؛ تلاش ناقص محفوظ ماند')
        result = dict(version=VERSION, completed_at=now_tehran_iso(), cases=cases,
            status_counts=dict(Counter(case['status'] for case in cases)), **BOUNDARIES)
        write_private_report(folder / 'result.json', result)
        write_private_report(folder / 'manifest.json', dict(version=VERSION, plan_sha256=_sha(folder / 'plan.json'),
            result_sha256=_sha(folder / 'result.json'), code=code, observations=files))
        load_dividend_evidence(folder)
        write_private_report(folder / 'status.json', dict(status='complete', planned=len(requests), completed=len(cases)))
        return dict(folder=str(folder), **result)
    except BaseException:
        try:
            write_private_report(folder / 'status.json', dict(status='failed_or_interrupted', planned=len(requests), completed=len(cases)))
        except OSError:
            pass
        raise


def load_dividend_evidence(folder):
    try:
        return _load_dividend_evidence(folder)
    except (KeyError, TypeError, AttributeError, UnicodeError) as error:
        raise ValueError('ساختار آرشیو سود نقدی معتبر نیست') from error


def _load_dividend_evidence(folder):
    folder = Path(folder)
    if not re.fullmatch(r'DIVIDENDS-[a-f0-9]{32}', folder.name):
        raise ValueError('شناسه آرشیو سود نقدی معتبر نیست')
    private_report_path(folder / 'manifest.json', forbidden_paths=(settings.DB_PATH,))
    plan, result, manifest = (read_private_report(folder / name) for name in ('plan.json', 'result.json', 'manifest.json'))
    if any(item['version'] != VERSION for item in (plan, result, manifest)):
        raise ValueError('نسخه آرشیو پشتیبانی نمی‌شود')
    if (not is_canonical_iso_date(plan['cutoff']) or plan['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or _sha(folder / 'plan.json') != manifest['plan_sha256'] or _sha(folder / 'result.json') != manifest['result_sha256']
            or manifest['code'] != plan['code'] or set(plan['code']) != set(CODE_FILES)):
        raise ValueError('مرز، برنامه یا کد محفوظ ناسازگار است')
    if ({p.relative_to(folder / 'code').as_posix() for p in (folder / 'code').rglob('*') if p.is_file()} != set(CODE_FILES)
            or any(_sha(folder / 'code' / name) != value for name, value in plan['code'].items())):
        raise ValueError('کد محفوظ حذف یا عوض شده است')
    if any(item.get(key) is not False for item in (plan, result) for key in BOUNDARIES):
        raise ValueError('حدود شاهد به تأیید مالی ارتقا یافته است')
    if _stamp(result['completed_at']) < _stamp(plan['registered_at']):
        raise ValueError('پایان دریافت پیش از ثبت برنامه است')
    requests, cases = plan['requests'], result['cases']
    if _scope([dict(ins_code=r['ins_code'],symbol_fa=r['symbol'],instrument_type_code=r['current_instrument_type_code']) for r in requests]) != requests or len(cases) != len(requests):
        raise ValueError('مخرج یا دامنه نمادها ناسازگار است')
    expected_files = set()
    for index, (request, case) in enumerate(zip(requests, cases)):
        metadata_name, body_name = f'observation-{index:03d}.json', f'body-{index:03d}.bin'
        wrapper = read_private_report(folder / metadata_name)
        has_body = wrapper['observation'] is not None
        expected_files.add(metadata_name)
        if has_body:
            expected_files.add(body_name)
        if (case['request'] != request or case['observation_file'] != metadata_name
                or case['body_file'] != (body_name if has_body else None)):
            raise ValueError('پیوند تلاش با برنامه برابر نیست')
        assessed = _assess(folder, request, wrapper, body_name, plan['cutoff'], _stamp(plan['registered_at']), _stamp(result['completed_at']))
        if assessed != {key:case[key] for key in ('status', 'assessment')}:
            raise ValueError('بازحساب نتیجه با پاسخ محفوظ برابر نیست')
    actual_files = {p.name for pattern in ('observation-*', 'body-*') for p in folder.glob(pattern)}
    if (set(manifest['observations']) != expected_files or actual_files != expected_files
            or any(_sha(folder / name) != value for name, value in manifest['observations'].items())
            or result['status_counts'] != dict(Counter(case['status'] for case in cases))):
        raise ValueError('هش، شمار تلاش یا خلاصه ناسازگار است')
    return dict(plan=plan, artifacts_verified=True, **result)
