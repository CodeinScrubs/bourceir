"""Read-only, bounded native price cache; misses never dispatch network requests."""

import hashlib
import json
import re
import stat
from pathlib import Path

from config import settings
from core.daily_publications import _clock, _constant, _float, _unique
from core.timeutils import is_canonical_iso_date
from data_collector.daily_publications import LEGACY_VERSION, _decode, _parsed, _tasks, checked_transport_clock
from data_collector.daily_publications import VERSION as ARCHIVE_VERSION
from data_collector.publication_compat import archive_contract

VERSION = 'native-price-cache-snapshot-v2'
MAX_ARCHIVES = 64
MAX_TASKS = 12000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 512 * 1024
ARCHIVE = re.compile(r'DAYPUB-[0-9a-f]{32}')
FILE = re.compile(r'dated-DAYPUB-[0-9a-f]{32}-(?:plan|observation-[0-9]{5})\.json')
HASH = re.compile(r'[0-9a-f]{64}')
POLICY = dict(network_on_miss=False, max_archives=MAX_ARCHIVES, max_tasks=MAX_TASKS,
              max_total_bytes=MAX_TOTAL_BYTES, max_source_age_seconds=None,
              source_age_is_not_refreshed_by_cache_read=True,
              archive_layout='flat legacy + requested ISO-date + mixed; client-only archives excluded',
              original_application_omitted=True, independent_external_timestamp=False)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _json(body):
    try:
        result = json.loads(body.decode('utf-8'), object_pairs_hook=_unique,
                            parse_constant=_constant, parse_float=_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Invalid bounded native-cache JSON') from exc
    if not isinstance(result, dict):
        raise ValueError('Native-cache JSON must be an object')
    return result


def _no_reparse(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('Native cache cannot follow a symlink or junction')


def _root(value):
    expected = Path(settings.DATA_DIR).absolute() / 'private' / 'daily-publications'
    candidate = expected if value is None else Path(value).absolute()
    if '..' in candidate.parts or candidate != expected:
        raise ValueError('Only the designated public-publication cache is admissible')
    _no_reparse(expected.parent)
    _no_reparse(Path(settings.DATA_DIR).absolute())
    _no_reparse(expected)
    return expected


def _child(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('Native-cache path is not a fixed relative member')
    current = root
    for part in name.split('/'):
        current = current / part
        _no_reparse(current)
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('Native-cache member escapes its designated directory')
    forbidden = Path(settings.DB_PATH)
    if current.exists() and forbidden.exists() and current.samefile(forbidden):
        raise ValueError('The account database is not a cache member')
    return current


def _scope(scope):
    if not isinstance(scope, list) or not 1 <= len(scope) <= 1000:
        raise ValueError('A bounded explicit ordinary-stock scope is required')
    result = {}
    for row in scope:
        code, kind = row.get('ins_code'), row.get('instrument_type_code')
        if (not isinstance(code, str) or not re.fullmatch(r'[1-9][0-9]{0,29}', code)
                or type(kind) is not int or kind not in (300, 303) or code in result):
            raise ValueError('Invalid or duplicate cache scope identity')
        result[code] = kind
    return dict(sorted(result.items()))


def _plan(plan):
    if (set(plan) != {'version', 'registered_at', 'code', 'tasks', 'workers', 'source_scope',
                     'price_unit', 'model_selection', 'holdout_unsealed', 'accepted_for_signals'}
            or plan['version'] not in (ARCHIVE_VERSION,LEGACY_VERSION) or plan['price_unit'] != 'Rial'
            or plan['source_scope'] != 'public_market_only'
            or any(plan[k] is not False for k in ('model_selection', 'holdout_unsealed', 'accepted_for_signals'))
            or type(plan['workers']) is not int or not 1 <= plan['workers'] <= 3):
        raise ValueError('Unknown source-publication plan contract')
    _tasks(plan['tasks'], plan['registered_at'],version=plan['version'])


def _result(result, plan):
    expected = {'plan.json', *(f'observation-{i:05d}.json' for i in range(len(plan['tasks'])))}
    if (set(result) != {'version', 'completed_at', 'artifacts_sha256', 'application',
                       'accepted_for_signals', 'profitability_proven'}
            or result['version'] != plan['version'] or not isinstance(result['artifacts_sha256'], dict)
            or set(result['artifacts_sha256']) != expected
            or any(not isinstance(h, str) or not HASH.fullmatch(h) for h in result['artifacts_sha256'].values())
            or result['accepted_for_signals'] is not False or result['profitability_proven'] is not False
            or _clock(result['completed_at']) < _clock(plan['registered_at'])):
        raise ValueError('Unknown or incomplete source-publication result manifest')


def _record(record, task, plan, completed):
    fields = {'version', 'task', 'attempted_at', 'completed_at', 'request_profile',
              'provider_error_type', 'source', 'parsed'}
    if plan['version'] == ARCHIVE_VERSION:
        fields.add('transport')
    if (set(record) != fields
            or record['version'] != plan['version'] or record['task'] != task
            or not _clock(plan['registered_at']) <= _clock(record['attempted_at'])
                   <= _clock(record['completed_at']) <= _clock(completed)):
        raise ValueError('Cache record identity or wrapper clock differs')
    if plan['version'] == ARCHIVE_VERSION:
        checked_transport_clock(record['transport'],record['attempted_at'],record['completed_at'])
    parsed = _parsed(record['source'], task, record['attempted_at'], record['completed_at'])
    if record['parsed'] != parsed:
        raise ValueError('Cache record does not reparse from its native bytes')
    return parsed


def capture_cache(scope, session_date, *, observed_at, cache_root=None):
    """Validate consumed origin members, return a projection plus unchanged public JSON.

    The application section of the origin result is never copied or interpreted.
    Its original digest is retained as a provenance link, not an independently
    reproducible digest of the projected result or an external authenticity proof.
    """
    identities, at, root = _scope(scope), _clock(observed_at), _root(cache_root)
    if not is_canonical_iso_date(session_date) or session_date > at.date().isoformat():
        raise ValueError('Cache observation needs a non-future explicit session')
    snapshot = dict(version=VERSION, policy=dict(POLICY), observed_at=observed_at,
        session_date=session_date, scope=identities, archives={}, skipped_archives=[], records=[],
        original_result_application_copied=False, native_requests=0,
        complete_publication_coverage_verified=False, accepted_for_signals=False)
    bodies, total_bytes, total_tasks = {}, 0, 0

    def read(name, maximum):
        nonlocal total_bytes
        path = _child(root, name)
        with path.open('rb') as source:
            body = source.read(min(maximum, MAX_TOTAL_BYTES-total_bytes)+1)
        total_bytes += len(body)
        if len(body) > maximum or total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('Native cache exceeds its registered byte budget')
        return body

    names = []
    for relative in ('',session_date,'mixed'):
        folder = root if not relative else _child(root,relative)
        if folder.exists():
            names.extend((p.name,relative) for p in folder.iterdir() if p.name.startswith('DAYPUB-'))
    names.sort()
    if len(names) > MAX_ARCHIVES:
        raise ValueError('Native cache exceeds its registered archive budget')
    if len({name for name,_ in names}) != len(names):
        raise ValueError('Duplicate native-cache archive identity across date directories')
    for name,relative in names:
        if not ARCHIVE.fullmatch(name):
            raise ValueError('Unknown native-cache archive identity')
        prefix = relative+'/'+name if relative else name
        folder = _child(root, prefix)
        if not folder.is_dir():
            raise ValueError('Native-cache archive must be a directory')
        if not _child(root, prefix+'/result.json').is_file():
            snapshot['skipped_archives'].append(dict(archive_id=name, status='incomplete_archive'))
            continue
        plan_body = read(prefix+'/plan.json', MAX_METADATA_BYTES)
        plan = _json(plan_body)
        _plan(plan)
        selected = [(i, task) for i, task in enumerate(plan['tasks']) if task['date'] == session_date
                    and task['kind'] == 'price' and task['ins_code'] in identities]
        if not selected:
            snapshot['skipped_archives'].append(dict(archive_id=name, status='outside_requested_session_or_scope'))
            continue
        result_body = read(prefix+'/result.json', MAX_METADATA_BYTES)
        result = _json(result_body)
        _result(result, plan)
        if _clock(result['completed_at']) > at:
            snapshot['skipped_archives'].append(dict(archive_id=name, status='not_available_at_observation'))
            continue
        total_tasks += len(plan['tasks'])
        if total_tasks > MAX_TASKS:
            raise ValueError('Native cache exceeds its registered task budget')
        code_files = archive_contract(plan['version'],plan['code'])
        if result['artifacts_sha256']['plan.json'] != _sha(plan_body):
            raise ValueError('Cache plan or parser/collector code identity differs')
        for file in code_files:
            if _sha(read(prefix+'/code/'+file, 2*1024*1024)) != plan['code'][file]:
                raise ValueError('Archived native-cache source file differs')
        plan_file = f'dated-{name}-plan.json'
        bodies[plan_file] = plan_body
        snapshot['archives'][name] = dict(plan_file=plan_file, plan_sha256=_sha(plan_body),
            origin_result_sha256=_sha(result_body),
            result_projection={k: result[k] for k in ('version', 'completed_at', 'artifacts_sha256')},
            application_omitted=True, origin_code_verified=True)
        for index, task in selected:
            if identities[task['ins_code']] != task['instrument_type_code']:
                raise ValueError('Current scope and dated task ordinary-stock types differ')
            file = f'observation-{index:05d}.json'
            body = read(prefix+'/'+file, MAX_RECORD_BYTES)
            if _sha(body) != result['artifacts_sha256'][file]:
                raise ValueError('Native-cache record hash differs')
            record = _json(body)
            parsed = _record(record, task, plan, result['completed_at'])
            target = f'dated-{name}-{file}'
            bodies[target] = body
            snapshot['records'].append(dict(archive_id=name, index=index, file=target,
                record_sha256=_sha(body), ins_code=task['ins_code'], status=parsed['status']))
    snapshot['origin_bytes_read'] = total_bytes
    snapshot['declared_tasks_in_consumed_archives'] = total_tasks
    # Exercise exactly the same membership and native-byte checks used offline.
    replay_cache(snapshot, lambda file: bodies[file], scope, session_date, observed_at=observed_at)
    return snapshot, bodies


def replay_cache(snapshot, read_file, scope, session_date, *, observed_at):
    """Rebuild admitted receipts solely from the frozen snapshot and public bytes."""
    identities, at = _scope(scope), _clock(observed_at)
    fields = {'version', 'policy', 'observed_at', 'session_date', 'scope', 'archives', 'skipped_archives',
              'records', 'original_result_application_copied', 'native_requests',
              'complete_publication_coverage_verified', 'accepted_for_signals', 'origin_bytes_read',
              'declared_tasks_in_consumed_archives'}
    if (not isinstance(snapshot, dict) or set(snapshot) != fields
            or snapshot.get('version') != VERSION or snapshot.get('policy') != POLICY
            or snapshot.get('scope') != identities or snapshot.get('session_date') != session_date
            or snapshot.get('observed_at') != observed_at or snapshot.get('native_requests') != 0
            or snapshot.get('original_result_application_copied') is not False
            or snapshot.get('accepted_for_signals') is not False
            or snapshot.get('complete_publication_coverage_verified') is not False
            or not isinstance(snapshot.get('archives'), dict) or len(snapshot['archives']) > MAX_ARCHIVES
            or not isinstance(snapshot.get('records'), list) or len(snapshot['records']) > MAX_TASKS
            or not isinstance(snapshot.get('skipped_archives'), list)
            or len(snapshot['skipped_archives']) + len(snapshot['archives']) > MAX_ARCHIVES
            or type(snapshot.get('origin_bytes_read')) is not int or not 0 <= snapshot['origin_bytes_read'] <= MAX_TOTAL_BYTES
            or type(snapshot.get('declared_tasks_in_consumed_archives')) is not int
            or not 0 <= snapshot['declared_tasks_in_consumed_archives'] <= MAX_TASKS):
        raise ValueError('Native-cache snapshot contract differs')
    skipped = set()
    for item in snapshot['skipped_archives']:
        if (not isinstance(item, dict) or set(item) != {'archive_id', 'status'}
                or not isinstance(item['archive_id'], str) or not ARCHIVE.fullmatch(item['archive_id'])
                or item['archive_id'] in snapshot['archives'] or item['archive_id'] in skipped
                or item['status'] not in ('incomplete_archive', 'outside_requested_session_or_scope',
                                          'not_available_at_observation')):
            raise ValueError('Invalid skipped native-cache archive state')
        skipped.add(item['archive_id'])
    output = {code: [] for code in identities}
    plans, seen = {}, set()
    for identity, archive in snapshot['archives'].items():
        if (not isinstance(identity, str) or not ARCHIVE.fullmatch(identity) or not isinstance(archive, dict)
                or set(archive) != {'plan_file', 'plan_sha256', 'origin_result_sha256', 'result_projection',
                                   'application_omitted', 'origin_code_verified'}
                or archive.get('plan_file') != f'dated-{identity}-plan.json'
                or archive.get('application_omitted') is not True or archive.get('origin_code_verified') is not True
                or not isinstance(archive.get('origin_result_sha256'), str)
                or not HASH.fullmatch(archive['origin_result_sha256'])):
            raise ValueError('Invalid native-cache origin projection')
        body = read_file(archive['plan_file'])
        if not isinstance(body, bytes) or len(body) > MAX_METADATA_BYTES or _sha(body) != archive['plan_sha256']:
            raise ValueError('Frozen cache plan differs')
        plan = _json(body)
        _plan(plan)
        projected = archive['result_projection']
        _result(dict(projected, application={}, accepted_for_signals=False, profitability_proven=False), plan)
        archive_contract(plan['version'],plan['code'])
        if (projected['artifacts_sha256']['plan.json'] != _sha(body)
                or _clock(projected['completed_at']) > at):
            raise ValueError('Frozen cache origin code, clock or plan differs')
        plans[identity] = plan
    if sum(len(plan['tasks']) for plan in plans.values()) != snapshot['declared_tasks_in_consumed_archives']:
        raise ValueError('Native-cache task denominator differs')
    for pointer in snapshot['records']:
        if not isinstance(pointer, dict) or set(pointer) != {'archive_id', 'index', 'file', 'record_sha256', 'ins_code', 'status'}:
            raise ValueError('Invalid frozen-cache record schema')
        identity, index, file = (pointer.get(key) for key in ('archive_id', 'index', 'file'))
        if (identity not in plans or type(index) is not int or not 0 <= index < len(plans[identity]['tasks'])
                or not isinstance(file, str) or not FILE.fullmatch(file)
                or file != f'dated-{identity}-observation-{index:05d}.json' or file in seen):
            raise ValueError('Invalid or duplicate frozen-cache record pointer')
        seen.add(file)
        plan = plans[identity]
        task = plan['tasks'][index]
        source_file = f'observation-{index:05d}.json'
        projected = snapshot['archives'][identity]['result_projection']
        if (task['kind'] != 'price' or task['date'] != session_date or task['ins_code'] not in identities
                or task['instrument_type_code'] != identities[task['ins_code']]
                or pointer['ins_code'] != task['ins_code']):
            raise ValueError('Frozen cache record was not requested for this scope and session')
        body = read_file(file)
        if (not isinstance(body, bytes) or len(body) > MAX_RECORD_BYTES or _sha(body) != pointer['record_sha256']
                or _sha(body) != projected['artifacts_sha256'][source_file]):
            raise ValueError('Frozen native record differs from its source manifest')
        record = _json(body)
        parsed = _record(record, task, plan, projected['completed_at'])
        if pointer['status'] != parsed['status']:
            raise ValueError('Frozen-cache parse status differs')
        if parsed['status'] in ('valid_price', 'observed_zero_activity'):
            output[task['ins_code']].append(dict(archive_id=identity, file=source_file,
                record_sha256=_sha(body), archive_registered_at=plan['registered_at'],
                archive_completed_at=projected['completed_at'], attempted_at=record['attempted_at'],
                completed_at=record['completed_at'], observation=_decode(record['source']), parsed=parsed))
    expected = {(origin, i) for origin, plan in plans.items() for i, task in enumerate(plan['tasks'])
                if task['kind'] == 'price' and task['date'] == session_date and task['ins_code'] in identities}
    if {(p['archive_id'], p['index']) for p in snapshot['records']} != expected:
        raise ValueError('Frozen cache omitted a requested record from its consumed archives')
    return output
