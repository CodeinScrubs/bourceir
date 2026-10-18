"""پیوند محتوای مظنه به چرخه دریافت محلی؛ زمان بیرونی یا اجرای سفارش را ثابت نمی‌کند."""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime

from core.timeutils import TEHRAN_TZ

CAPTURE_VERSION = 'capture-protocol-v2'
SUPPORTED_CAPTURE_VERSIONS = ('capture-protocol-v1', CAPTURE_VERSION)
BINDING_VERSION = 'snapshot-binding-v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def snapshot_sha256(snapshot):
    # SQLite با affinity REAL عدد صحیح را float برمی‌گرداند؛ برابری عددی، هش برابر دارد.
    normalized = {key: int(value) if isinstance(value, float) and math.isfinite(value) and value.is_integer()
                  else value for key, value in snapshot.items()}
    return sha256(normalized)


def snapshot_binding(snapshot):
    return dict(version=BINDING_VERSION, snapshot_id=snapshot['id'],
                snapshot_sha256=snapshot_sha256(snapshot))


def _clock(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return stamp.astimezone(TEHRAN_TZ)
    except (ValueError, TypeError):
        pass
    raise ValueError('ساعت دریافت باید ISO دارای منطقه زمانی باشد')


def receipts_from_cycle(evidence):
    """کپی کامل یک چرخه یک‌بار حفظ می‌شود، نه یک نسخه به ازای هر نماد دامنه."""
    run, started, finished = (evidence[key] for key in ('run', 'started', 'finished'))
    protocol = json.loads(run['protocol_json'])
    if (hashlib.sha256(run['protocol_json'].encode()).hexdigest() != run['protocol_sha256']
            or protocol.get('version') not in SUPPORTED_CAPTURE_VERSIONS):
        raise ValueError('هش یا نسخه پروتکل دریافت ناسازگار است')
    code = protocol['code']
    if sha256(code['files']) != code['sha256']:
        raise ValueError('هش کد دریافت ناسازگار است')
    if (started['event'] != 'poll_started' or finished['event'] != 'poll_finished'
            or started['run_id'] != run['id'] or finished['run_id'] != run['id']
            or started['cycle'] != finished['cycle'] or started['cycle'] <= 0
            or started['id'] >= finished['id']):
        raise ValueError('هویت یا ترتیب رویداد دریافت ناسازگار است')
    scope = protocol['instruments']
    if (not scope or len({r['symbol_id'] for r in scope}) != len(scope)
            or len({r['ins_code'] for r in scope}) != len(scope)
            or any(not isinstance(r['ins_code'], str) or not r['ins_code']
                   or isinstance(r['symbol_id'], bool) or not isinstance(r['symbol_id'], int) or r['symbol_id'] <= 0
                   for r in scope)
            or any(r['instrument_type_code'] not in (300, 303) for r in scope)):
        raise ValueError('دامنه چرخه دریافت ناسازگار است')
    payload = json.loads(finished['payload_json'])
    coverage = payload.get('coverage')
    if payload.get('status') not in ('success', 'partial', 'failed', 'error'):
        raise ValueError('وضعیت پایان دریافت ناسازگار است')
    observed = [] if coverage is None else coverage['per_instrument']
    if coverage is not None:
        counts = Counter(row['status'] for row in observed)
        if (len(observed) != len(scope) or {r['ins_code'] for r in observed} != {r['ins_code'] for r in scope}
                or coverage['expected'] != len(scope) or coverage.get('unresolved_symbols')
                or set(counts) - {'changed', 'unchanged', 'missing', 'duplicate', 'rejected'}
                or coverage['valid'] != counts['changed'] + counts['unchanged']
                or any(coverage[k] != counts[k] for k in ('changed', 'unchanged', 'missing', 'duplicate', 'rejected'))):
            raise ValueError('پوشش چرخه دریافت ناسازگار است')
    by_ins = {row['ins_code']: row for row in observed}
    return {instrument['symbol_id']: dict(run_id=run['id'], protocol_sha256=run['protocol_sha256'],
        capture_version=protocol['version'], cycle=finished['cycle'], finished_event_id=finished['id'],
        poll_started_at=started['recorded_at'], poll_finished_at=finished['recorded_at'],
        run_started_at=run['started_at'], provider=protocol['provider'], code=code,
        symbol_id=instrument['symbol_id'], ins_code=instrument['ins_code'],
        instrument_type_code=instrument['instrument_type_code'],
        observation=by_ins.get(instrument['ins_code'])) for instrument in scope}


def binding_problems(snapshot, receipt, recorded_at, *, max_age=45):
    if receipt is None:
        return ['capture_receipt_missing']
    problems = []
    at = _clock(recorded_at)
    try:
        finish, start, run_start = (_clock(receipt[key]) for key in
                                   ('poll_finished_at', 'poll_started_at', 'run_started_at'))
    except ValueError:
        return ['capture_receipt_time_unknown']
    if not run_start <= start <= finish <= at or finish.date() != at.date():
        problems.append('capture_receipt_time_invalid')
    elif (at - finish).total_seconds() > max_age:
        problems.append('capture_receipt_stale')
    observation = receipt['observation']
    if not observation or observation.get('status') not in ('changed', 'unchanged'):
        return problems + ['capture_observation_unavailable']
    binding = observation.get('snapshot_binding')
    if receipt['capture_version'] != CAPTURE_VERSION or not binding:
        return problems + ['snapshot_binding_missing']
    if (snapshot is None or snapshot.get('symbol_id') != receipt['symbol_id']
            or binding != snapshot_binding(snapshot)
            or observation.get('provider_timestamp') != snapshot.get('provider_timestamp')):
        return problems + ['snapshot_binding_conflict']
    try:
        received = _clock(observation.get('captured_at'))
        stored = _clock(snapshot.get('captured_at'))
        if not start <= received <= finish or stored > received or stored.date() != received.date():
            problems.append('snapshot_receipt_clock_conflict')
    except ValueError:
        problems.append('snapshot_receipt_clock_unknown')
    return problems
