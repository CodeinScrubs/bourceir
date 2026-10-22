"""Byte receipts for routine dated publications; no DB or account writes here."""
import base64
import hashlib
import json
import logging
import math
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import settings
from core.daily_publications import MAX_BYTES, _clock, parse_publication, publication_request
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso

VERSION = 'daily-publication-archive-v1'
CODE_FILES = ('core/daily_publications.py', 'core/price_quality.py', 'core/price_revision.py',
              'core/client_quality.py', 'core/text.py', 'core/timeutils.py', 'data_collector/base.py',
              'data_collector/tsetmc.py', 'data_collector/daily_publications.py', 'data_collector/scheduler.py',
              'database/db.py', 'database/price_recovery.py', 'config/settings.py', 'config/default.yaml')
META = ('source', 'ins_code', 'url', 'requested_at', 'received_at', 'response_bytes',
        'body_sha256', 'price_unit', 'requested_date', 'kind')
logger = logging.getLogger(__name__)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _code():
    return {name: hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            for name in CODE_FILES}


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def _encode(observation):
    if observation is None:
        return None
    if not isinstance(observation, dict):
        return dict(invalid_wrapper_type=type(observation).__name__)
    result = {}
    for key in META:
        value = observation.get(key)
        if type(value) in (str, int, bool, type(None)) or type(value) is float and math.isfinite(value):
            result[key] = value
        else:
            result[key] = dict(invalid_type=type(value).__name__)
    body = observation.get('content')
    result['body_base64'] = (base64.b64encode(body).decode('ascii')
                             if isinstance(body, bytes) and len(body) <= MAX_BYTES else None)
    return result


def _decode(source):
    if source is None:
        return None
    if not isinstance(source, dict) or set(source) - {*META, 'body_base64', 'invalid_wrapper_type'}:
        raise ValueError('ساختار شاهد روزانه محفوظ معتبر نیست')
    encoded = source.get('body_base64')
    if encoded is None:
        content = None
    elif not isinstance(encoded, str) or len(encoded) > 4*((MAX_BYTES+2)//3):
        raise ValueError('حجم شاهد روزانه محفوظ نامعتبر است')
    else:
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('بایت شاهد روزانه محفوظ نامعتبر است') from exc
    return dict(source, content=content)


def _parsed(source, task, attempted_at, completed_at):
    if source is None:
        return dict(status='source_unavailable', row=None)
    try:
        return parse_publication(_decode(source), task['ins_code'], task['date'], task['kind'],
                                 requested_after=attempted_at, received_by=completed_at)
    except (ValueError, TypeError, OverflowError) as exc:
        return dict(status='rejected', error_type=type(exc).__name__, error=str(exc), row=None)


def _tasks(tasks, at):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12000:
        raise ValueError('دامنه دریافت روزانه باید یک تا دوازده‌هزار مشاهده باشد')
    seen = set()
    day = _clock(at).date().isoformat()
    for task in tasks:
        if (not isinstance(task, dict) or set(task) != {'symbol_id', 'symbol', 'ins_code', 'date', 'kind', 'instrument_type_code'}
                or type(task['symbol_id']) is not int or task['symbol_id'] <= 0
                or not isinstance(task['symbol'], str) or normalize_symbol(task['symbol']) != task['symbol']
                or not task['symbol'] or task['symbol'][-1:].isdigit()
                or type(task['instrument_type_code']) is not int or task['instrument_type_code'] not in (300, 303)
                or not is_canonical_iso_date(task['date']) or task['date'] > day):
            raise ValueError('دامنه ابزار یا تاریخ دریافت روزانه معتبر نیست')
        code, _ = publication_request(task['ins_code'], task['date'], task['kind'])
        if task['ins_code'] != code:
            raise ValueError('شناسه متنی صریح لازم است')
        key = code, task['date'], task['kind']
        if key in seen:
            raise ValueError('مشاهده روزانه تکراری در برنامه دریافت')
        seen.add(key)


class DailyPublicationArchive:
    def __init__(self, root, tasks, *, workers=1):
        if type(workers) is not int or not 1 <= workers <= 3:
            raise ValueError('تعداد دریافت هم‌زمان باید یک تا سه باشد')
        at = now_tehran_iso()
        _tasks(tasks, at)
        self.code, self.tasks, self.workers = _code(), [dict(t) for t in tasks], workers
        self._captured = self._sealed = False
        self.folder = Path(root).resolve()/('DAYPUB-'+uuid.uuid4().hex)
        self.folder.mkdir(parents=True, exist_ok=False)
        for name, digest in self.code.items():
            body = (settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError('کد هنگام ثبت دریافت روزانه تغییر کرد')
            target = self.folder/'code'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        _write(self.folder/'plan.json', dict(version=VERSION, registered_at=at, code=self.code,
            tasks=self.tasks, workers=workers, source_scope='public_market_only',
            price_unit='Rial', model_selection=False, holdout_unsealed=False, accepted_for_signals=False))

    def capture(self, provider, *, provider_factory=None, progress=None):
        """Only owned factory instances run in parallel; an injected provider stays serial."""
        if self._captured:
            raise ValueError('تلاش دریافت قبلی بازنویسی نمی‌شود؛ بایگانی تازه بسازید')
        self._captured = True
        owned, local, records = [], threading.local(), [None]*len(self.tasks)
        def worker(index):
            error = None
            task = self.tasks[index]
            attempted = now_tehran_iso()
            source, profile = None, 'unknown'
            try:
                if provider_factory is None:
                    current = provider
                else:
                    if not hasattr(local, 'provider'):
                        local.provider = provider_factory()
                        owned.append(local.provider)
                    current = local.provider
                profile = getattr(current, 'request_profile', 'unknown')
                source = _encode(current.get_daily_publication_observation(task['ins_code'], task['date'], task['kind']))
            except Exception as exc:
                logger.exception('دریافت بومی روزانه %s ناموفق بود', task['symbol'])
                error = type(exc).__name__
            record = dict(version=VERSION, task=task, attempted_at=attempted, completed_at=now_tehran_iso(),
                request_profile=profile, provider_error_type=error, source=source, parsed=None)
            # Preserve the receipt before parsing; an interrupted parse is not a complete archive.
            _write(self.folder/f'observation-{index:05d}.json', record)
            record['parsed'] = _parsed(source, task, attempted, record['completed_at'])
            _write(self.folder/f'observation-{index:05d}.json', record)
            return index, record
        try:
            if provider_factory is None or self.workers == 1:
                for index in range(len(self.tasks)):
                    _, records[index] = worker(index)
                    if progress:
                        progress(index+1, len(self.tasks), self.tasks[index]['symbol'])
            else:
                with ThreadPoolExecutor(max_workers=self.workers) as executor:
                    futures = [executor.submit(worker, i) for i in range(len(self.tasks))]
                    for count, future in enumerate(as_completed(futures), 1):
                        index, records[index] = future.result()
                        if progress:
                            progress(count, len(self.tasks), self.tasks[index]['symbol'])
        finally:
            for current in owned:
                try:
                    current.close()
                except Exception:
                    logger.exception('بستن منبع دریافت روزانه ناموفق بود')
        if self.code != _code():
            raise ValueError('کد هنگام دریافت روزانه تغییر کرد؛ داده اعمال نمی‌شود')
        self.records = records
        return records

    def finish(self, application):
        if (self._sealed or not self._captured or self.code != _code()
                or len(getattr(self, 'records', [])) != len(self.tasks)):
            raise ValueError('دریافت روزانه کامل نیست یا کد تغییر کرد')
        artifacts = {'plan.json': _sha(self.folder/'plan.json')}
        for index, record in enumerate(self.records):
            saved = json.loads((self.folder/f'observation-{index:05d}.json').read_text(encoding='utf-8'))
            if saved != record:
                raise ValueError('شاهد دریافت‌شده پیش از ثبت نتیجه تغییر کرده است')
        artifacts.update({f'observation-{i:05d}.json': _sha(self.folder/f'observation-{i:05d}.json')
                          for i in range(len(self.tasks))})
        result = dict(version=VERSION, completed_at=now_tehran_iso(), artifacts_sha256=artifacts,
                      application=application, accepted_for_signals=False, profitability_proven=False)
        _write(self.folder/'result.json', result)
        self._sealed = True
        return str(self.folder)


def review_archive(folder):
    """Reparse preserved bytes with matching code; application claims need DB evidence separately."""
    folder = Path(folder).resolve()
    plan, result = (json.loads((folder/name).read_text(encoding='utf-8')) for name in ('plan.json', 'result.json'))
    if plan.get('version') != VERSION or result.get('version') != VERSION or plan.get('code') != _code():
        raise ValueError('قرارداد یا کد بایگانی روزانه با نسخه فعلی برابر نیست')
    _tasks(plan['tasks'], plan['registered_at'])
    expected = {'plan.json', *(f'observation-{i:05d}.json' for i in range(len(plan['tasks'])))}
    if set(result['artifacts_sha256']) != expected:
        raise ValueError('فهرست شاهدهای دریافت روزانه ناقص است')
    for name, digest in result['artifacts_sha256'].items():
        if _sha(folder/name) != digest:
            raise ValueError('هش شاهد روزانه تغییر کرده است')
    for name, digest in plan['code'].items():
        if _sha(folder/'code'/name) != digest:
            raise ValueError('کد محفوظ دریافت روزانه تغییر کرده است')
    statuses = {}
    for index, task in enumerate(plan['tasks']):
        record = json.loads((folder/f'observation-{index:05d}.json').read_text(encoding='utf-8'))
        if (record.get('version') != VERSION or not _clock(plan['registered_at']) <= _clock(record['attempted_at'])
                <= _clock(record['completed_at']) <= _clock(result['completed_at'])):
            raise ValueError('ترتیب زمانی بایگانی روزانه معتبر نیست')
        parsed = _parsed(record['source'], task, record['attempted_at'], record['completed_at'])
        if record['task'] != task or record['parsed'] != parsed:
            raise ValueError('نتیجه از بایت محفوظ دریافت روزانه بازتولید نمی‌شود')
        statuses[parsed['status']] = statuses.get(parsed['status'], 0)+1
    return dict(version=VERSION, byte_replay_verified=True, observations=len(plan['tasks']), statuses=statuses,
                application_independently_verified=False, accepted_for_signals=False, profitability_proven=False)
