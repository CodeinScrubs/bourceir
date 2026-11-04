"""Opt-in, durable CODAL search transport gate; no credentials or response bodies."""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

from config import settings
from core.daily_publications import _constant, _unique
from core.timeutils import now_tehran_iso
from data_collector.codal import parse_retry_after
from data_collector.source_cooldown import SourceDeferred, _locked

VERSION = 'codal-search-gate-v1'
_VOLATILE_BLOCKS = set()
MAX_RESPONSE_BYTES = 1048576


def _public_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise ValueError('bounded public URL required')
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != 'search.codal.ir'
            or parts.path != '/api/search/v2/q' or parts.fragment or parts.username or parts.password):
        raise ValueError('only the public CODAL search endpoint is permitted')
    query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    if (len(query) != 4 or {k for k, _ in query} != {'FromDate', 'ToDate', 'PageNumber', 'LetterType'}
            or any(not re.fullmatch(r'\d{4}/\d{2}/\d{2}', v) for k, v in query if k in ('FromDate', 'ToDate'))
            or any(not re.fullmatch(r'[1-9]\d{0,2}', v) for k, v in query if k in ('PageNumber', 'LetterType'))):
        raise ValueError('unsupported public query parameters')
    return url


def _root():
    root = (settings.DATA_DIR/'private/source-health/codal').absolute()
    for path in (*reversed(root.parents), root, root/'state.lock'):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('source gate cannot follow symlinks or junctions')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _blockers(root):
    """Any blocker or partial blocker is fail-closed, without parsing unsafe content."""
    if str(root) in _VOLATILE_BLOCKS:
        return ['in_memory_persistence_failure']
    found, releases = [], []
    for p in root.iterdir():
        if p.name == 'state.lock':
            continue
        if p.is_symlink() or not p.is_file() or not re.fullmatch(r'(?:block|release)-[0-9a-f]{64}\.json', p.name):
            raise ValueError('unexpected source gate file; manual review required')
        if p.stat().st_size > 65536:
            raise ValueError('oversized source receipt; manual review required')
        body = p.read_bytes()
        if hashlib.sha256(body).hexdigest() not in p.name:
            raise ValueError('source receipt hash mismatch')
        if p.name.startswith('block-'):
            found.append(p.name)
        else:
            try:
                data = json.loads(body.decode('utf-8'))
            except (ValueError, UnicodeError) as error:
                raise ValueError('invalid source review receipt') from error
            releases.append(data)
    if not found:
        return ['gate_not_initialized']
    from core.codal_search import stamp
    for release in releases:
        if (isinstance(release, dict) and set(release) == {
                'version', 'blocker_receipts', 'reviewed_at', 'not_before', 'review_basis'}
                and release['version'] == VERSION and release['blocker_receipts'] == sorted(found)
                and isinstance(release['review_basis'], str) and 12 <= len(release['review_basis']) <= 2000
                and stamp(release['not_before']) <= stamp(release['reviewed_at']) <= stamp(now_tehran_iso())):
            return []
    return sorted(found)


def _save_block(root, receipt):
    body = json.dumps(receipt, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    name = 'block-'+hashlib.sha256(body).hexdigest()+'.json'
    target = root/name
    _VOLATILE_BLOCKS.add(str(root))
    try:
        if not target.exists():
            with target.open('xb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        elif target.read_bytes() != body:
            raise ValueError('existing block receipt conflicts')
        _VOLATILE_BLOCKS.discard(str(root))
    except BaseException:
        # Persistence failed; this process must not make a subsequent request.
        raise
    return name


def source_status():
    root = _root()
    with _locked(root):
        blockers = _blockers(root)
        return dict(version=VERSION, source='codal_search', blocked=bool(blockers), blocker_receipts=blockers,
                    automatic_release=False)


def import_diagnostic(path):
    """Preserve an explicit existing diagnostic as a blocker; never contact the source."""
    source = Path(path).resolve()
    allowed = (settings.DATA_DIR/'private/codal-registration-chains').resolve()
    if not source.is_relative_to(allowed) or source.suffix.lower() != '.json':
        raise ValueError('diagnostic must be an existing private chain receipt')
    body = source.read_bytes()
    if len(body) > 65536:
        raise ValueError('diagnostic too large')
    payload = json.loads(body.decode('utf-8'))
    if (not isinstance(payload, dict) or payload.get('diagnostic_only') is not True
            or payload.get('http_status') != 429 or payload.get('redirected') is not False):
        raise ValueError('an explicit 429 diagnostic is required')
    _public_url(payload['url'])
    from core.codal_search import stamp
    if not stamp(payload['requested_at']) <= stamp(payload['received_at']) <= stamp(now_tehran_iso()):
        raise ValueError('invalid diagnostic timestamps')
    try:
        seconds = parse_retry_after(payload.get('retry_after'), received_at=payload['received_at'])
        unsupported = False
    except ValueError:
        seconds, unsupported = None, True
    receipt = dict(version=VERSION, source='codal_search', origin='imported_diagnostic',
                   diagnostic_sha256=hashlib.sha256(body).hexdigest(), url=payload['url'],
                   requested_at=payload['requested_at'], received_at=payload['received_at'], http_status=429,
                   content_type=payload.get('content_type'), retry_after_seconds=seconds,
                   retry_after_unsupported=unsupported, release_requires_manual_review=True)
    root = _root()
    with _locked(root):
        name = _save_block(root, receipt)
    return dict(receipt=name, **source_status())


def record_manual_review(*, not_before, review_basis):
    """Explicit operator attestation; no source request and no evidence deletion."""
    from datetime import timedelta

    from core.codal_search import stamp
    if not isinstance(review_basis, str) or not 12 <= len(review_basis.strip()) <= 2000:
        raise ValueError('a concrete written source review is required')
    requested_time = stamp(not_before)
    reviewed_at = now_tehran_iso()
    if requested_time > stamp(reviewed_at):
        raise ValueError('review cannot release before its explicit time')
    root = _root()
    with _locked(root):
        blocked = _blockers(root)
        if not blocked or 'gate_not_initialized' in blocked or 'in_memory_persistence_failure' in blocked:
            raise ValueError('a persisted source-limit receipt is required')
        for name in blocked:
            data = json.loads((root/name).read_text(encoding='utf-8'))
            received = stamp(data['received_at'])
            delay = data.get('retry_after_seconds')
            if data.get('retry_after_unsupported') is not False:
                raise ValueError('unsupported server delay needs separate resolution')
            if requested_time <= received or (delay is not None and requested_time < received+timedelta(seconds=delay)):
                raise ValueError('requested time must respect every source limit')
        receipt = dict(version=VERSION, blocker_receipts=blocked, reviewed_at=reviewed_at,
                       not_before=requested_time.isoformat(), review_basis=review_basis.strip())
        body = json.dumps(receipt, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
        name = 'release-'+hashlib.sha256(body).hexdigest()+'.json'
        with (root/name).open('xb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    return dict(review_receipt=name, **source_status())


class CodalGate:
    def __init__(self):
        self.last_evidence = None

    def _verification_receipt(self):
        from core.codal_search import stamp

        evidence = self.last_evidence
        if (not isinstance(evidence, dict) or evidence.get('version') != VERSION
                or evidence.get('status') != 'response_received' or evidence.get('http_status') != 200):
            raise ValueError('a preceding successful transport receipt is required')
        _public_url(evidence['url'])
        if not stamp(evidence['requested_at']) <= stamp(evidence['received_at']) <= stamp(now_tehran_iso()):
            raise ValueError('invalid transport timestamps')
        return dict(version=VERSION, source='codal_search', origin='verification_required',
                    url=evidence['url'], requested_at=evidence['requested_at'],
                    received_at=evidence['received_at'], http_status=200, content_type=None,
                    retry_after_seconds=None, retry_after_unsupported=False,
                    release_requires_manual_review=True)

    def preserve_verification_required(self):
        """Persist the collector's parsed IsAttacker=true without another request."""
        evidence = self.last_evidence
        receipt = self._verification_receipt()
        root = _root()
        with _locked(root):
            name = _save_block(root, receipt)
        self.last_evidence = {**evidence, 'status': 'verification_required', 'blocker_receipt': name}
        return name

    def preserve_invalid_response(self):
        """Record additional evaluator rejections after a valid transport response."""
        evidence = self.last_evidence
        receipt = self._verification_receipt()
        receipt['origin'] = 'invalid_response_after_evaluation'
        root = _root()
        with _locked(root):
            name = _save_block(root, receipt)
        self.last_evidence = {**evidence, 'status': 'invalid_response', 'blocker_receipt': name}
        return name

    def _preserve_unexpected(self, root, response, reason):
        """An ambiguous or rejected response requires review before another request."""
        evidence = self.last_evidence
        content_type = response.headers.get('Content-Type', '')
        if not isinstance(content_type, str):
            content_type = ''
        receipt = dict(version=VERSION, source='codal_search', origin=reason,
                       url=evidence['url'], requested_at=evidence['requested_at'],
                       received_at=evidence['received_at'], http_status=evidence['http_status'],
                       content_type=content_type[:128], retry_after_seconds=None,
                       retry_after_unsupported=False, release_requires_manual_review=True)
        name = _save_block(root, receipt)
        self.last_evidence.update(status='unexpected_response', blocker_receipt=name, reason=reason)

    def get(self, session, url, **options):
        # A rejected new call must never expose an earlier request as its evidence.
        self.last_evidence = None
        _public_url(url)
        if options.get('allow_redirects') is not False:
            raise ValueError('redirects must be disabled')
        if options.get('stream') is not True:
            raise ValueError('streaming must be explicitly enabled')
        timeout = options.get('timeout')
        values = timeout if isinstance(timeout, tuple) else (timeout,)
        if (not values or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 < x <= 10
                              for x in values)):
            raise ValueError('bounded source timeout required')
        if isinstance(session, requests.Session):
            retry = session.get_adapter(url).max_retries
            if any(retry.is_retry('GET', status, has_retry_after=True) for status in (429, 503)):
                raise ValueError('HTTP adapter may hide source limitations')
        root = _root()
        # Serialize admission and HTTP headers across participating processes.
        with _locked(root):
            if _blockers(root):
                self.last_evidence = dict(version=VERSION, status='blocked', checked_at=now_tehran_iso())
                raise SourceDeferred('CODAL search is blocked pending manual source review')
            requested = now_tehran_iso()
            # Entering the HTTP call is an attempt, even when no response headers
            # arrive. It does not prove that the remote server received anything.
            self.last_evidence = dict(version=VERSION, status='request_started', url=url,
                                      requested_at=requested, http_status=None)
            try:
                response = session.get(url, **options)
            except Exception as error:
                self.last_evidence.update(status='transport_error', failed_at=now_tehran_iso(),
                                          error_type=type(error).__name__)
                raise
            received = now_tehran_iso()
            status = getattr(response, 'status_code', None)
            self.last_evidence = dict(version=VERSION, status='response_received', url=url,
                                      requested_at=requested, received_at=received, http_status=status)
            if status in (429, 503):
                try:
                    try:
                        seconds = parse_retry_after(response.headers.get('Retry-After'), received_at=received)
                        unsupported = False
                    except ValueError:
                        seconds, unsupported = None, True
                    receipt = dict(version=VERSION, source='codal_search', origin='guarded_response', url=url,
                                   requested_at=requested, received_at=received, http_status=status,
                                   content_type=response.headers.get('Content-Type', '')[:128],
                                   retry_after_seconds=seconds, retry_after_unsupported=unsupported,
                                   release_requires_manual_review=True)
                    name = _save_block(root, receipt)
                    self.last_evidence.update(status='limited', blocker_receipt=name)
                except BaseException:
                    response.close()
                    raise
            elif (status != 200 or getattr(response, 'url', None) != url
                  or response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                  != 'application/json'):
                try:
                    reason = ('unexpected_http_status' if status != 200 else
                              'unexpected_response_url' if getattr(response, 'url', None) != url else
                              'unexpected_content_type')
                    self._preserve_unexpected(root, response, reason)
                except BaseException:
                    response.close()
                    raise
            elif (status == 200 and response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                  == 'application/json'):
                # Keep admission serialized until the one bounded body read has revealed
                # IsAttacker. Other guarded processes cannot slip through between headers
                # and publishing a verification blocker.
                started = time.monotonic()
                chunks, size = [], 0
                try:
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES or time.monotonic()-started > 30:
                            raise ValueError('CODAL response exceeds bounded read')
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    try:
                        payload = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_unique,
                                             parse_constant=_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        payload = None
                    if isinstance(payload, dict) and payload.get('IsAttacker') is True:
                        name = _save_block(root, self._verification_receipt())
                        self.last_evidence.update(status='verification_required', blocker_receipt=name)
                    elif not isinstance(payload, dict) or payload.get('IsAttacker') is not False:
                        self._preserve_unexpected(root, response, 'invalid_or_ambiguous_verification_json')
                    elif (set(payload) != {'IsAttacker', 'Letters', 'Page', 'Total'}
                          or not isinstance(payload['Letters'], list) or len(payload['Letters']) > 100
                          or type(payload['Page']) is not int or payload['Page'] < 0
                          or type(payload['Total']) is not int or payload['Total'] < len(payload['Letters'])):
                        self._preserve_unexpected(root, response, 'invalid_response_structure')
                    response = _BufferedResponse(response, body)
                except BaseException as error:
                    try:
                        if isinstance(error, Exception) and self.last_evidence['status'] == 'response_received':
                            self._preserve_unexpected(root, response, 'body_read_or_validation_failed')
                    finally:
                        response.close()
                    raise
            return response


class _BufferedResponse:
    """Expose the already-read bounded bytes to the existing provider once more in memory."""
    def __init__(self, response, body):
        self.status_code, self.headers, self.url = response.status_code, response.headers, response.url
        self.body = body
        response.close()

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index+chunk_size]

    def close(self):
        self.body = b''


class GatedSession:
    """Session adapter for CodalProvider; only callers using it share this gate."""
    def __init__(self, session, gate):
        self.session, self.gate, self.headers = session, gate, session.headers

    def get(self, url, **options):
        return self.gate.get(self.session, url, **options)

    def close(self):
        self.session.close()
