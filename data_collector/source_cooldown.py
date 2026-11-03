"""Host-local public-source admission shared by TSETMC provider instances/processes."""

import json
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from config import settings
from core.daily_publications import _clock, _constant, _float, _unique
from core.source_cooldown import VERSION, checked_backoff, deferred, make_backoff, public_url, retry_after, stronger
from core.timeutils import now_tehran_iso

STATE_DIR = settings.DATA_DIR/'private'/'source-health'/'tsetmc'
_THREAD_LOCK = threading.RLock()
_VOLATILE_LIMITS = {}
_WINDOWS = os.name == 'nt'
_LOCK_RETRY_DELAYS = (.01, .02, .04, .08)


class SourceDeferred(ValueError):
    """No request was admitted; this is not an empty successful market response."""


def _directory():
    root = Path(STATE_DIR).absolute()
    if '..' in root.parts:
        raise ValueError('Source state path cannot traverse a parent directory')
    for path in (*reversed(root.parents), root, root/'state.json', root/'state.lock'):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Shared-source state cannot follow a symlink or junction')
    root.mkdir(parents=True, exist_ok=True)
    state = root/'state.json'
    if state.exists() and Path(settings.DB_PATH).exists() and state.samefile(settings.DB_PATH):
        raise ValueError('The account database cannot be shared-source state')
    return root


@contextmanager
def _locked(root):
    # Per-process lock also covers threads on platforms with process-owned file locks.
    with _THREAD_LOCK, (root/'state.lock').open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'0')
            stream.flush()
        started = time.monotonic()
        while True:
            stream.seek(0)
            try:
                if sys.platform.startswith('win'):
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic()-started >= 5:
                    raise SourceDeferred('Shared-source admission lock remained busy') from None
                time.sleep(.02)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform.startswith('win'):
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read(root):
    path = root/'state.json'
    if not path.exists():
        state = dict(version=VERSION, last_admitted_at=None, backoff=None)
    else:
        with path.open('rb') as stream:
            body = stream.read(65537)
        if len(body) > 65536:
            raise ValueError('Shared-source state exceeds its byte budget')
        try:
            state = json.loads(body.decode('utf-8'), object_pairs_hook=_unique,
                               parse_constant=_constant, parse_float=_float)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('Invalid shared-source state JSON') from exc
    if not isinstance(state, dict) or set(state) != {'version','last_admitted_at','backoff'} or state['version'] != VERSION:
        raise ValueError('Unknown shared-source state contract')
    if state['last_admitted_at'] is not None:
        _clock(state['last_admitted_at'])
    checked_backoff(state['backoff'])
    journals = list(root.glob('limit-*.json'))
    if len(journals)>64:
        raise ValueError('Unapplied source-limit journal exceeds its receipt budget')
    for journal in journals:
        info=journal.lstat()
        if (not re.fullmatch(r'limit-[0-9a-f]{64}\.json',journal.name)
                or stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
                or Path(settings.DB_PATH).exists() and journal.samefile(settings.DB_PATH)):
            raise ValueError('Invalid source-limit journal path')
        with journal.open('rb')as stream:
            body=stream.read(65537)
        if len(body)>65536:
            raise ValueError('Source-limit journal exceeds its byte budget')
        try:
            value=json.loads(body.decode('utf-8'),object_pairs_hook=_unique,
                parse_constant=_constant,parse_float=_float)
        except (UnicodeError,json.JSONDecodeError,RecursionError)as exc:
            raise ValueError('Interrupted source-limit journal requires review')from exc
        value=checked_backoff(value)
        if value is None or journal.name!='limit-'+value['receipt_sha256']+'.json':
            raise ValueError('Source-limit journal identity differs from its receipt')
        state['backoff']=stronger(state['backoff'],value)
    state['backoff']=stronger(state['backoff'],_VOLATILE_LIMITS.get(str(root)))
    return state


def _retry_windows_file_lock(operation):
    """Retry only brief local Windows sharing/access denials, never network requests."""
    original = None
    for delay in (*_LOCK_RETRY_DELAYS, None):
        try:
            return operation()
        except PermissionError as error:
            if not _WINDOWS or getattr(error, 'winerror', None) not in (5, 32, 33):
                raise
            if original is None:
                original = error
            if delay is None:
                raise original
            time.sleep(delay)


def _write(root, state):
    body = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    primary_error = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                prefix='.state-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _retry_windows_file_lock(lambda: os.replace(temporary, root/'state.json'))
        temporary = None
        # The merged cooldown is durable before its temporary recovery receipts retire.
        for path in root.glob('limit-*.json'):
            path.unlink()
        _VOLATILE_LIMITS.pop(str(root),None)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary is not None:
            try:
                _retry_windows_file_lock(lambda: temporary.unlink(missing_ok=True))
            except OSError as error:
                if primary_error is None:
                    raise
                primary_error.add_note('Temporary source-state cleanup failed '
                                       f'({type(error).__name__}); manual review may be required')


def source_status():
    root = _directory()
    with _locked(root):
        return _read(root)


def checked_transport_evidence(value):
    if value is None:
        return None
    schemas = {
        'admitted': {'url','requested_at'},
        'response_received': {'url','requested_at','received_at','http_status'},
        'rate_limited': {'url','requested_at','received_at','http_status','backoff'},
        'source_cooldown': {'checked_at','backoff'},
        'admission_timeout': {'checked_at'},
        'gate_error': {'error_type'},
    }
    if (not isinstance(value,dict) or value.get('status') not in schemas or value.get('version') != VERSION
            or set(value) != {'version','status','admitted'} | schemas[value['status']]
            or type(value['admitted']) is not bool
            or value['admitted'] != (value['status'] in ('admitted','response_received','rate_limited'))):
        raise ValueError('Unknown public transport evidence schema')
    if 'url' in value:
        public_url(value['url'])
        _clock(value['requested_at'])
    if 'received_at' in value:
        if (_clock(value['received_at']) < _clock(value['requested_at'])
                or value['http_status'] is not None and (type(value['http_status']) is not int or not 100 <= value['http_status'] <= 599)):
            raise ValueError('Invalid public transport response metadata')
    if 'checked_at' in value:
        _clock(value['checked_at'])
    if 'error_type' in value and (not isinstance(value['error_type'],str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',value['error_type'])):
        raise ValueError('Transport error metadata cannot contain arbitrary text')
    if 'backoff' in value:
        checked_backoff(value['backoff'])
        if value['backoff'] is None:
            raise ValueError('A limited/deferred transport requires its receipt')
        if value['status'] == 'rate_limited':
            observation = value['backoff']['observation']
            if any(observation[k] != value[k] for k in ('url','requested_at','received_at','http_status')):
                raise ValueError('Transport cooldown differs from the response receipt')
        elif not deferred(value['backoff'],at=value['checked_at']):
            raise ValueError('Deferred source evidence must have an active cooldown')
    return json.loads(json.dumps(value,allow_nan=False))


class SourceGate:
    def __init__(self):
        self.last_evidence = None

    def _admit(self, *, max_wait):
        delay = settings.RATE_LIMIT_DELAY
        if isinstance(delay, bool) or not isinstance(delay, (int,float)) or not math.isfinite(delay) or delay < 0:
            raise ValueError('Shared-source request spacing must be finite and nonnegative')
        started = time.monotonic()
        root = _directory()
        while True:
            with _locked(root):
                state = _read(root)
                at = now_tehran_iso()
                if deferred(state['backoff'], at=at):
                    self.last_evidence = dict(version=VERSION,status='source_cooldown',admitted=False,
                                               checked_at=at,backoff=state['backoff'])
                    raise SourceDeferred('Public TSETMC source is in a preserved cooldown')
                elapsed = None if state['last_admitted_at'] is None else (_clock(at)-_clock(state['last_admitted_at'])).total_seconds()
                if elapsed is not None and elapsed < 0:
                    raise ValueError('Clock rollback cannot reset shared-source request spacing')
                remaining = 0 if elapsed is None else delay-elapsed
                if remaining <= 0:
                    state['last_admitted_at'] = at
                    _write(root,state)
                    return at
            if time.monotonic()-started >= max_wait:
                self.last_evidence = dict(version=VERSION,status='admission_timeout',admitted=False,checked_at=now_tehran_iso())
                raise SourceDeferred('Shared-source request spacing remained busy')
            time.sleep(min(.05,remaining))

    def get(self, session, url, **options):
        """Only new admissions are blocked; previously admitted requests may still finish."""
        self.last_evidence = None
        public_url(url)
        timeout = options.get('timeout',5)
        timeout = max(timeout) if isinstance(timeout,tuple) else timeout
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('An explicit bounded I/O timeout is required')
        try:
            if isinstance(session, requests.Session):
                retry = session.get_adapter(url).max_retries
                if any(retry.is_retry('GET',status,has_retry_after=True) for status in (429,503)):
                    raise ValueError('Injected HTTP adapter can hide retryable source-limit responses')
            at = self._admit(max_wait=min(10,timeout))
            self.last_evidence = dict(version=VERSION,status='admitted',admitted=True,url=url,requested_at=at)
            response = session.get(url,**options)
            status = getattr(response,'status_code',None)
            received = now_tehran_iso()
            self.last_evidence.update(status='response_received',http_status=status,received_at=received)
            if type(status) is int and status in (429,503):
                try:
                    try:
                        seconds = retry_after(getattr(response,'headers',{}).get('Retry-After'),received_at=received)
                        unsupported = False
                    except ValueError:
                        seconds,unsupported = None,True
                    observation = dict(url=url,requested_at=at,received_at=received,http_status=status,
                                       retry_after_seconds=seconds,retry_after_unsupported=unsupported)
                    backoff = make_backoff(observation)
                    self.last_evidence.update(status='rate_limited',backoff=backoff)
                    root = _directory()
                    with _locked(root):
                        _VOLATILE_LIMITS[str(root)]=stronger(_VOLATILE_LIMITS.get(str(root)),backoff)
                        journal=root/('limit-'+backoff['receipt_sha256']+'.json')
                        if not journal.exists():
                            with journal.open('xb')as stream:
                                stream.write(json.dumps(backoff,allow_nan=False).encode())
                                stream.flush();os.fsync(stream.fileno())
                        state = _read(root)
                        state['backoff'] = stronger(state['backoff'],backoff)
                        _write(root,state)
                except BaseException:
                    if hasattr(response,'close'):
                        response.close()
                    raise
            return response
        except BaseException as exc:
            if self.last_evidence is None:
                self.last_evidence = dict(version=VERSION,status='gate_error',admitted=False,error_type=type(exc).__name__)
            raise
