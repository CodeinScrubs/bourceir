"""Bounded, resumable public rights-symbol discovery; never changes the stock universe."""

import argparse
import base64
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import settings
from core.rights_discovery import MAX_BYTES, VERSION, assess_search, search_request
from core.timeutils import now_tehran_iso
from data_collector.source_cooldown import checked_transport_evidence
from data_collector.tsetmc import TsetmcProvider
from database.db import DatabaseManager
from ops.report_files import private_report_path, read_private_report, write_private_report

PLAN_VERSION = 'rights-discovery-plan-v1'
SOURCE_FILES = ('core/rights_discovery.py', 'core/instrument_history.py', 'core/text.py', 'core/timeutils.py',
                'data_collector/base.py', 'data_collector/tsetmc.py', 'data_collector/source_cooldown.py',
                'ops/rights_discovery.py', 'ops/report_files.py', 'config/settings.py', 'config/default.yaml',
                'requirements.lock.txt')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _code_fingerprints(names):
    return {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            for name in names}


def _code_matches(plan, names):
    recorded = plan.get('capture_code_sha256')
    if recorded is None:
        return None  # Older plans did not freeze code; do not fabricate evidence.
    if not isinstance(recorded, dict) or set(recorded) != set(names):
        raise ValueError('Frozen capture-code fingerprint schema differs')
    return recorded == _code_fingerprints(names)


def _folder(folder):
    marker = private_report_path(Path(folder) / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    return marker.parent


def create_plan(parents, folder):
    """Freeze the entire caller-supplied stock snapshot before any discovery request."""
    folder = _folder(folder)
    if not isinstance(parents, list) or not parents or len(parents) > 10000:
        raise ValueError('A bounded nonempty stock snapshot is required')
    cleaned, seen = [], set()
    for parent in parents:
        if not isinstance(parent, dict) or set(parent) != {'parent_code', 'parent_symbol', 'type_code', 'is_active'}:
            raise ValueError('Explicit source metadata are required for every parent')
        request = search_request(parent['parent_code'], parent['parent_symbol'])
        if (request['parent_code'] in seen or type(parent['type_code']) is not int
                or parent['type_code'] not in (300, 303) or type(parent['is_active']) is not bool):
            raise ValueError('Unique ordinary stock identities and active flags are required')
        seen.add(request['parent_code'])
        cleaned.append(parent)
    cleaned.sort(key=lambda item: (item['parent_symbol'], item['parent_code']))
    plan = {'version': PLAN_VERSION, 'registered_at': now_tehran_iso(), 'search_contract': VERSION,
            'parents': cleaned, 'parent_snapshot_sha256': hashlib.sha256(_canonical(cleaned)).hexdigest(),
            'capture_code_sha256': _code_fingerprints(SOURCE_FILES),
            'holdout_start': settings.RESEARCH_CONFIG['holdout_start'],
            'source_snapshot_is_present_day': True, 'historical_universe_complete': False,
            'historical_instrument_identity_verified': False, 'accepted_for_signals': False}
    folder.mkdir(parents=True, exist_ok=False)
    write_private_report(folder / 'plan.json', plan, forbidden_paths=(settings.DB_PATH,))
    return folder


def _read_plan(folder):
    plan = read_private_report(folder / 'plan.json')
    parents = plan.get('parents')
    if (plan.get('version') != PLAN_VERSION or plan.get('search_contract') != VERSION
            or not isinstance(parents, list) or not parents or len(parents) > 10000
            or plan.get('holdout_start') != settings.RESEARCH_CONFIG['holdout_start']
            or plan.get('parent_snapshot_sha256') != hashlib.sha256(_canonical(parents)).hexdigest()):
        raise ValueError('Frozen discovery plan has changed or is incompatible')
    seen = set()
    for parent in parents:
        if not isinstance(parent, dict) or set(parent) != {'parent_code', 'parent_symbol', 'type_code', 'is_active'}:
            raise ValueError('Frozen parent metadata have changed')
        request = search_request(parent['parent_code'], parent['parent_symbol'])
        if (request['parent_code'] in seen or type(parent['type_code']) is not int
                or parent['type_code'] not in (300, 303) or type(parent['is_active']) is not bool):
            raise ValueError('Frozen parent identities are invalid')
        seen.add(request['parent_code'])
    return plan


def _read_attempt(path, request, plan_sha256):
    record = read_private_report(path, max_bytes=4 * MAX_BYTES)
    if (record.get('request') != request or record.get('version') != PLAN_VERSION
            or record.get('plan_sha256') != plan_sha256):
        raise ValueError('Saved search attempt request differs')
    checked_transport_evidence(record.get('transport_evidence'))
    receipt, encoded = record.get('observation'), record.get('body_base64')
    if receipt is None:
        if encoded is not None or record.get('assessment') != assess_search(request, None):
            raise ValueError('Unavailable attempt contains unbound evidence')
        return record
    if not isinstance(receipt, dict) or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError('Saved search body is invalid')
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('Saved search body encoding is invalid') from error
    if (len(body) > MAX_BYTES or type(receipt.get('response_bytes')) is not int
            or receipt['response_bytes'] != len(body)
            or receipt.get('body_sha256') != hashlib.sha256(body).hexdigest()):
        raise ValueError('Saved native search bytes differ from their receipt')
    try:
        assessment = assess_search(request, receipt | {'content': body})
    except ValueError:
        assessment = {'status': 'invalid_response'}
    if record.get('assessment') != assessment:
        raise ValueError('Saved search assessment differs from its native body')
    return record


@contextmanager
def _exclusive_capture(folder):
    lock = folder / '.capture.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ValueError('Capture lock exists; check a prior running or interrupted process') from error
    try:
        os.write(descriptor, _canonical({'pid': os.getpid(), 'started_at': now_tehran_iso()}))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def capture(provider, folder, *, max_new=100, retry_failed=False, progress=None):
    """Persist each attempt and stop on source error; retries require an explicit later run."""
    folder = _folder(folder)
    plan = _read_plan(folder)
    if type(max_new) is not int or not 1 <= max_new <= 10000 or type(retry_failed) is not bool:
        raise ValueError('Explicit bounded batch size and retry policy are required')
    with _exclusive_capture(folder):
        # Recheck after claiming the lock so a changed plan cannot race the capture.
        if plan != _read_plan(folder):
            raise ValueError('Frozen plan changed before capture')
        return _capture(provider, folder, plan, max_new=max_new, retry_failed=retry_failed, progress=progress)


def _capture(provider, folder, plan, *, max_new, retry_failed, progress):
    plan_sha256 = hashlib.sha256(_canonical(plan)).hexdigest()
    attempted = 0
    stop = None
    for index, parent in enumerate(plan['parents'], 1):
        if _code_matches(plan, SOURCE_FILES) is False:
            stop = 'capture_code_changed'
            break
        request = search_request(parent['parent_code'], parent['parent_symbol'])
        parent_dir = folder / 'observations' / request['parent_code']
        previous = sorted(parent_dir.glob('*.json')) if parent_dir.exists() else []
        if previous:
            if [path.name for path in previous] != [f'{number:04d}.json' for number in range(1, len(previous)+1)]:
                raise ValueError('Saved search attempt sequence is incomplete')
            checked = [_read_attempt(path, request, plan_sha256) for path in previous]
            last = checked[-1]
            if last['assessment']['status'] in ('observed_matches', 'observed_no_match'):
                continue
            if not retry_failed:
                stop = 'prior_failed_attempt_requires_explicit_retry'
                break
        if attempted >= max_new:
            stop = 'batch_complete'
            break
        observed = provider.get_research_rights_search_observation(parent['parent_code'], parent['parent_symbol'])
        if observed is None:
            receipt, encoded, assessment = None, None, assess_search(request, None)
        else:
            try:
                assessment = assess_search(request, observed)
            except ValueError:
                assessment = {'status': 'invalid_response'}
            content = observed.get('content') if isinstance(observed, dict) else None
            if not isinstance(content, bytes) or len(content) > MAX_BYTES:
                raise ValueError('Provider returned an unarchivable native body')
            receipt = {key: value for key, value in observed.items() if key != 'content'}
            encoded = base64.b64encode(content).decode('ascii')
        transport = checked_transport_evidence(getattr(provider, 'last_transport_evidence', None))
        record = {'version': PLAN_VERSION, 'attempted_at': now_tehran_iso(), 'request': request,
                  'plan_sha256': plan_sha256, 'transport_evidence': transport,
                  'observation': receipt, 'body_base64': encoded, 'assessment': assessment}
        parent_dir.mkdir(parents=True, exist_ok=True)
        path = parent_dir / f'{len(previous) + 1:04d}.json'
        if path.exists():
            raise ValueError('Existing attempt cannot be overwritten')
        write_private_report(path, record, forbidden_paths=(settings.DB_PATH,))
        _read_attempt(path, request, plan_sha256)
        attempted += 1
        if progress is not None:
            progress(index, len(plan['parents']), parent, assessment)
        if assessment['status'] not in ('observed_matches', 'observed_no_match'):
            stop = 'source_unavailable_or_invalid'
            break
    return {'folder': str(folder), 'new_attempts': attempted, 'stop_reason': stop or 'plan_processed',
            'historical_universe_complete': False, 'accepted_for_signals': False}


def review(folder):
    """Replay receipts, retaining failed and unattempted parents in the denominator."""
    folder = _folder(folder)
    plan = _read_plan(folder)
    plan_sha256 = hashlib.sha256(_canonical(plan)).hexdigest()
    rows = []
    for parent in plan['parents']:
        request = search_request(parent['parent_code'], parent['parent_symbol'])
        parent_dir = folder / 'observations' / request['parent_code']
        attempts = sorted(parent_dir.glob('*.json')) if parent_dir.exists() else []
        if [path.name for path in attempts] != [f'{number:04d}.json' for number in range(1, len(attempts)+1)]:
            raise ValueError('Saved search attempt sequence is incomplete')
        assessments = [_read_attempt(path, request, plan_sha256)['assessment'] for path in attempts]
        latest = assessments[-1] if assessments else None
        rows.append({'parent': parent, 'attempt_count': len(attempts),
                     'status': latest['status'] if latest else 'not_attempted',
                     'candidate_codes': latest.get('candidate_codes', []) if latest else []})
    by_candidate = {}
    for row in rows:
        for candidate in row['candidate_codes']:
            by_candidate.setdefault(candidate, []).append(row['parent']['parent_code'])
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return {'version': PLAN_VERSION, 'folder': str(folder), 'parent_count': len(rows), 'status_counts': counts,
            'unique_candidate_codes': len(by_candidate),
            'candidate_codes_with_multiple_parents': {k: sorted(v) for k, v in by_candidate.items() if len(v) > 1},
            'capture_code_fingerprint_present': plan.get('capture_code_sha256') is not None,
            'current_code_matches_capture_plan': _code_matches(plan, SOURCE_FILES),
            'rows': rows, 'historical_universe_complete': False,
            'historical_instrument_identity_verified': False, 'accepted_for_signals': False}


def parent_snapshot(db):
    """Only current public stock identity columns are read; no account tables are queried."""
    rows = db.conn.execute('SELECT ins_code, symbol_fa, instrument_type_code, is_active FROM symbols '
                           'WHERE instrument_type_code IN (300, 303) ORDER BY symbol_fa, ins_code').fetchall()
    if any(type(row['is_active']) is not int or row['is_active'] not in (0, 1) for row in rows):
        raise ValueError('Current active status must not be fabricated')
    return [{'parent_code': str(row['ins_code']), 'parent_symbol': row['symbol_fa'],
             'type_code': row['instrument_type_code'], 'is_active': bool(row['is_active'])} for row in rows]


def main():
    parser = argparse.ArgumentParser(description='Research-only public rights discovery')
    parser.add_argument('--folder', help='Resume a previously frozen private plan')
    parser.add_argument('--max-new', type=int, default=100)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--review-only', action='store_true')
    args = parser.parse_args()
    if args.review_only and not args.folder:
        parser.error('--review-only requires --folder')
    if args.folder:
        folder = _folder(args.folder)
    else:
        with DatabaseManager(read_only=True) as db:
            parents = parent_snapshot(db)
        folder = create_plan(parents, settings.DATA_DIR / 'private' / 'rights-discovery' / ('DISC-' + uuid.uuid4().hex))
    print('Frozen plan:', folder, flush=True)
    if args.review_only:
        report = review(folder)
        print(json.dumps({key: value for key, value in report.items() if key != 'rows'}, ensure_ascii=False), flush=True)
        return
    provider = TsetmcProvider(realtime=True)
    try:
        def progress(index, total, parent, assessment):
            if index % 20 == 0 or assessment['status'] not in ('observed_matches', 'observed_no_match'):
                print(index, '/', total, parent['parent_symbol'], assessment['status'], flush=True)
        print(json.dumps(capture(provider, folder, max_new=args.max_new, retry_failed=args.retry_failed,
                                 progress=progress), ensure_ascii=False), flush=True)
    finally:
        provider.close()


if __name__ == '__main__':
    main()
