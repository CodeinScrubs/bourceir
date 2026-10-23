"""Bounded byte archives of dated state observations; no price, feature or account writes."""
import argparse
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

from config import settings
from core.daily_publications import _clock, _constant, _float, _unique
from core.instrument_states import MAX_BYTES, parse_state_observation, state_request
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso

VERSION = 'instrument-state-archive-v1'
MAX_TASKS = 100
# A bounded native list expands when per-row provenance/clock grades are retained.
MAX_FILE_BYTES = 16_777_216
CODE_FILES = ('core/instrument_states.py', 'core/daily_publications.py', 'core/price_revision.py',
              'core/price_quality.py', 'core/timeutils.py', 'core/text.py',
              'data_collector/base.py', 'data_collector/tsetmc.py',
              'data_collector/instrument_states.py', 'config/settings.py', 'config/default.yaml')
META = ('source', 'ins_code', 'requested_date', 'url', 'requested_at', 'received_at',
        'response_bytes', 'body_sha256', 'observation_kind')


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _code():
    return {name: _digest((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')) for name in CODE_FILES}


def _write(path, value):
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('فایل بایگانی وضعیت از حد حجم گذشت')
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _bytes(folder, name):
    path = folder/name
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('مسیر شاهد خارج از بایگانی است')
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('فایل شاهد موجود نیست یا از حد حجم گذشت')
    with path.open('rb') as source:
        body = source.read(MAX_FILE_BYTES+1)
    if len(body) > MAX_FILE_BYTES:
        raise ValueError('فایل شاهد حین خواندن از حد حجم گذشت')
    return body


def _read(folder, name):
    try:
        value = json.loads(_bytes(folder, name).decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=_constant, parse_float=_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('JSON بایگانی وضعیت معتبر نیست') from exc
    if not isinstance(value, dict):
        raise ValueError('ریشه شاهد وضعیت باید شیء باشد')
    return value


def _tasks(tasks, at):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError('برنامه وضعیت باید یک تا صد مشاهده داشته باشد')
    day, seen = _clock(at).date().isoformat(), set()
    for task in tasks:
        if (not isinstance(task, dict) or set(task) != {'symbol', 'ins_code', 'date', 'instrument_type_code'}
                or not isinstance(task['symbol'], str) or not task['symbol']
                or normalize_symbol(task['symbol']) != task['symbol'] or task['symbol'][-1:].isdigit()
                or type(task['instrument_type_code']) is not int or task['instrument_type_code'] not in (300, 303)):
            raise ValueError('دامنه نماد عادی و نام یکسان‌شده لازم است')
        code, _ = state_request(task['ins_code'], task['date'])
        if code != task['ins_code'] or task['date'] > day or (code, task['date']) in seen:
            raise ValueError('تاریخ آینده، شناسه نامعتبر یا مشاهده تکراری در برنامه وضعیت')
        seen.add((code, task['date']))


def _encode(observation):
    if observation is None:
        return None
    if not isinstance(observation, dict):
        return dict(invalid_wrapper_type=type(observation).__name__)
    result = {}
    for key in META:
        if key not in observation:
            continue
        value = observation[key]
        if type(value) is str and (len(value) > 4096 or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            result[key] = {'invalid_type': 'invalid_text'}
        elif type(value) is int and value.bit_length() > 64:
            result[key] = {'invalid_type': 'oversized_scalar'}
        else:
            result[key] = value if type(value) in (str, int, bool, type(None)) else {'invalid_type': type(value).__name__}
    body = observation.get('content')
    result['body_base64'] = base64.b64encode(body).decode('ascii') if type(body) is bytes and len(body) <= MAX_BYTES else None
    return result


def _decode(source):
    if not isinstance(source, dict) or set(source)-{*META, 'body_base64', 'invalid_wrapper_type'}:
        raise ValueError('ساختار شاهد محفوظ وضعیت معتبر نیست')
    encoded = source.get('body_base64')
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4*((MAX_BYTES+2)//3):
        raise ValueError('بایت محفوظ وضعیت موجود نیست یا از حد حجم گذشت')
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('بایت محفوظ وضعیت نامعتبر است') from exc
    return {key:source[key] for key in META if key in source} | {'content':body}


def _parsed(source, task, attempted, completed):
    if source is None:
        return dict(status='source_unavailable', rows=[], accepted_for_signals=False)
    try:
        return parse_state_observation(_decode(source), task['ins_code'], task['date'],
                                       requested_after=attempted, received_by=completed)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        return dict(status='rejected', error_type=type(exc).__name__, rows=[], accepted_for_signals=False)


class InstrumentStateArchive:
    def __init__(self, root, tasks):
        at = now_tehran_iso()
        _tasks(tasks, at)
        self.tasks, self.code = [dict(t) for t in tasks], _code()
        self._captured = self._sealed = False
        self.folder = Path(root).resolve()/('STATE-'+uuid.uuid4().hex)
        self.folder.mkdir(parents=True, exist_ok=False)
        for name, digest in self.code.items():
            body = (settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')
            if _digest(body) != digest:
                raise ValueError('کد هنگام ثبت برنامه وضعیت تغییر کرد')
            target = self.folder/'code'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        _write(self.folder/'plan.json', dict(version=VERSION, registered_at=at, tasks=self.tasks, code=self.code,
            source_scope='public_market_only', representation='http_body_bytes',
            historical_membership_verified=False, model_selection=False, holdout_unsealed=False,
            accepted_for_signals=False, profitability_proven=False))
        self.plan_sha256 = _digest(_bytes(self.folder, 'plan.json'))

    def _check_plan(self):
        if (_digest(_bytes(self.folder, 'plan.json')) != self.plan_sha256
                or _read(self.folder, 'plan.json')['tasks'] != self.tasks):
            raise ValueError('برنامه ثبت‌شده وضعیت تغییر کرده است')

    def capture(self, provider):
        if self._captured:
            raise ValueError('دریافت قبلی بازنویسی نمی‌شود؛ بایگانی تازه بسازید')
        self._captured = True
        self._check_plan()
        records = []
        for index, task in enumerate(self.tasks):
            attempted, source, error = now_tehran_iso(), None, None
            try:
                if getattr(provider, 'supports_dated_instrument_states', False) is not True:
                    raise NotImplementedError('dated state bytes unsupported')
                source = _encode(provider.get_dated_instrument_state_observation(task['ins_code'], task['date']))
            except Exception as exc:
                error = type(exc).__name__
            record = dict(version=VERSION, task=task, attempted_at=attempted, completed_at=now_tehran_iso(),
                          source=source, provider_error_type=error, parsed=None)
            _write(self.folder/f'observation-{index:03d}.json', record)
            record['parsed'] = _parsed(source, task, attempted, record['completed_at'])
            _write(self.folder/f'observation-{index:03d}.json', record)
            records.append(record)
        if self.code != _code():
            raise ValueError('کد حین دریافت وضعیت تغییر کرد؛ آرشیو کامل نیست')
        self.records = records
        return records

    def finish(self):
        if self._sealed or not self._captured or len(getattr(self, 'records', [])) != len(self.tasks) or self.code != _code():
            raise ValueError('بایگانی وضعیت ناقص است یا کد تغییر کرده است')
        self._check_plan()
        names = ['plan.json', *(f'observation-{i:03d}.json' for i in range(len(self.tasks)))]
        for name, record in zip(names[1:], self.records):
            if _read(self.folder, name) != record:
                raise ValueError('شاهد وضعیت پیش از نهایی‌شدن تغییر کرد')
        _write(self.folder/'result.json', dict(version=VERSION, completed_at=now_tehran_iso(),
            artifacts_sha256={name:_digest(_bytes(self.folder, name)) for name in names},
            applied=False, accepted_for_features=False, accepted_for_signals=False, profitability_proven=False))
        self._sealed = True
        return str(self.folder)


def review_archive(folder):
    folder = Path(folder).resolve()
    plan, result = _read(folder, 'plan.json'), _read(folder, 'result.json')
    if (plan.get('version') != VERSION or result.get('version') != VERSION
            or plan.get('code') != _code() or set(plan['code']) != set(CODE_FILES)
            or plan.get('source_scope') != 'public_market_only' or plan.get('representation') != 'http_body_bytes'
            or any(plan.get(k) is not False for k in ('historical_membership_verified', 'model_selection',
                'holdout_unsealed', 'accepted_for_signals', 'profitability_proven'))
            or any(result.get(k) is not False for k in ('applied', 'accepted_for_features', 'accepted_for_signals', 'profitability_proven'))):
        raise ValueError('نسخه، کد یا ادعای بایگانی وضعیت ناسازگار است')
    _tasks(plan['tasks'], plan['registered_at'])
    if _clock(result['completed_at']) < _clock(plan['registered_at']):
        raise ValueError('ترتیب ثبت و پایان بایگانی وضعیت نامعتبر است')
    expected = {'plan.json', *(f'observation-{i:03d}.json' for i in range(len(plan['tasks'])))}
    if ({p.name for p in folder.glob('observation-*.json')} != expected-{'plan.json'}
            or any(folder.glob('observation-*.tmp'))):
        raise ValueError('فهرست واقعی شاهدهای وضعیت با برنامه برابر نیست')
    hashes = result.get('artifacts_sha256')
    if not isinstance(hashes, dict) or set(hashes) != expected:
        raise ValueError('فهرست شاهدهای وضعیت کامل نیست')
    for name, digest in hashes.items():
        if _digest(_bytes(folder, name)) != digest:
            raise ValueError('هش شاهد وضعیت تغییر کرده است')
    for name, digest in plan['code'].items():
        if _digest(_bytes(folder, 'code/'+name)) != digest:
            raise ValueError('کد محفوظ وضعیت تغییر کرده است')
    statuses = {}
    for index, task in enumerate(plan['tasks']):
        record = _read(folder, f'observation-{index:03d}.json')
        if (record.get('version') != VERSION or record.get('task') != task
                or not _clock(plan['registered_at']) <= _clock(record['attempted_at'])
                <= _clock(record['completed_at']) <= _clock(result['completed_at'])):
            raise ValueError('هویت یا ترتیب زمانی شاهد وضعیت ناسازگار است')
        parsed = _parsed(record['source'], task, record['attempted_at'], record['completed_at'])
        if parsed != record['parsed']:
            raise ValueError('نتیجه وضعیت از بایت محفوظ بازتولید نشد')
        statuses[parsed['status']] = statuses.get(parsed['status'], 0)+1
    return dict(version=VERSION, byte_replay_verified=True, observations=len(plan['tasks']), statuses=statuses,
                full_session_coverage_verified=False, historical_membership_verified=False,
                accepted_for_features=False, accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=Path, help='JSON object with a tasks array; public symbol metadata only')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--review', type=Path)
    parser.add_argument('--root', type=Path, default=settings.DATA_DIR/'private'/'instrument-states')
    args = parser.parse_args(argv)
    if args.review:
        if args.tasks or args.fetch:
            parser.error('review cannot fetch or create another plan')
        output = review_archive(args.review)
    else:
        if not args.tasks or not args.fetch:
            parser.error('explicit tasks and fetch are required')
        from data_collector.tsetmc import TsetmcProvider
        plan = _read(args.tasks.resolve().parent, args.tasks.name)
        if set(plan) != {'tasks'}:
            parser.error('input must contain only tasks')
        archive = InstrumentStateArchive(args.root, plan['tasks'])
        provider = TsetmcProvider(realtime=True)
        try:
            archive.capture(provider)
        finally:
            provider.close()
        folder = archive.finish()
        output = dict(folder=folder, review=review_archive(folder))
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
