"""Frozen cache membership, bytes, clocks and unknown states; all files temporary."""

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from config import settings
from data_collector import publication_cache as cache
from data_collector.daily_publications import DailyPublicationArchive
from tests.conftest import FOOLAD_INS
from tests.test_daily_publications import DAY, NOW, observation, price, task

AT = '2024-01-03T15:00:00+03:30'
SCOPE = [dict(ins_code=FOOLAD_INS, instrument_type_code=300)]


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    folder = tmp_path/'data'
    monkeypatch.setattr(settings, 'DATA_DIR', folder)
    monkeypatch.setattr(settings, 'DB_PATH', folder/'brs.db')
    monkeypatch.setattr('data_collector.daily_publications.now_tehran_iso', lambda: NOW)
    result = folder/'private/daily-publications'
    result.mkdir(parents=True)
    return result


def make_archive(root, *, tasks=None, response=None):
    archive = DailyPublicationArchive(root, tasks or [task()])
    provider = SimpleNamespace(get_daily_publication_observation=lambda *args: (
        observation() if response is None else response(*args)))
    archive.capture(provider)
    archive.finish(dict(private_application_marker='must not be copied'))
    return archive.folder


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def capture():
    return cache.capture_cache(SCOPE, DAY, observed_at=AT)


def replay(snapshot, bodies):
    return cache.replay_cache(snapshot, bodies.__getitem__, SCOPE, DAY, observed_at=AT)


def test_successful_cache_preserves_native_time_all_members_and_omits_application(cache_root, monkeypatch):
    origin = make_archive(cache_root)
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **kw: pytest.fail('No HTTP on cache hit'))
    snapshot, bodies = capture()
    records = replay(snapshot, bodies)[FOOLAD_INS]
    assert len(records) == 1 and records[0]['observation']['received_at'] == NOW
    assert records[0]['parsed']['status'] == 'valid_price'
    assert snapshot['native_requests'] == 0 and snapshot['original_result_application_copied'] is False
    assert all(b'must not be copied' not in body for body in bodies.values())
    assert 'application' not in snapshot['archives'][origin.name]['result_projection']
    assert snapshot['archives'][origin.name]['origin_result_sha256'] == hashlib.sha256((origin/'result.json').read_bytes()).hexdigest()
    assert snapshot['declared_tasks_in_consumed_archives'] == 1


def test_missing_and_incomplete_caches_make_no_request_and_keep_full_scope(cache_root, monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **kw: pytest.fail('No HTTP on miss'))
    snapshot, bodies = capture()
    assert replay(snapshot, bodies) == {FOOLAD_INS: []}
    assert snapshot['records'] == [] and bodies == {}
    folder = cache_root/('DAYPUB-'+'a'*32)
    folder.mkdir()
    snapshot, bodies = capture()
    assert snapshot['skipped_archives'] == [dict(archive_id=folder.name, status='incomplete_archive')]
    assert replay(snapshot, bodies) == {FOOLAD_INS: []}


@pytest.mark.parametrize('response,status', [
    (lambda *a: None, 'source_unavailable'),
    (lambda *a: observation(native=price() | dict(priceFirst=0)), 'rejected'),
    (lambda *a: observation(native=price() | dict(qTotTran5J=0, qTotCap=0, zTotTran=0)), 'observed_zero_activity'),
])
def test_unavailable_rejected_and_zero_receipts_remain_distinct(cache_root, response, status):
    make_archive(cache_root, response=response)
    snapshot, bodies = capture()
    assert snapshot['records'][0]['status'] == status
    receipts = replay(snapshot, bodies)[FOOLAD_INS]
    assert len(receipts) == int(status == 'observed_zero_activity')


def test_all_vintages_are_kept_even_when_prices_differ(cache_root):
    make_archive(cache_root)
    make_archive(cache_root, response=lambda *a: observation(native=price() | dict(pClosing=97)))
    snapshot, bodies = capture()
    rows = replay(snapshot, bodies)[FOOLAD_INS]
    assert len(rows) == 2 and len(snapshot['archives']) == 2
    assert {r['parsed']['row']['close_final_raw'] for r in rows} == {97, 100}


def test_nonmatching_dates_and_identities_do_not_enter_scope(cache_root):
    make_archive(cache_root, tasks=[task(code='123')], response=lambda *a: observation(code='123'))
    snapshot, bodies = capture()
    assert replay(snapshot, bodies) == {FOOLAD_INS: []}
    assert snapshot['archives'] == {} and snapshot['skipped_archives'][0]['status'] == 'outside_requested_session_or_scope'


def test_archive_not_completed_by_observation_time_is_not_admitted(cache_root):
    folder = make_archive(cache_root)
    value = load(folder/'result.json')
    value['completed_at'] = '2024-01-03T16:00:00+03:30'
    write(folder/'result.json', value)
    snapshot, bodies = capture()
    assert replay(snapshot, bodies) == {FOOLAD_INS: []}
    assert snapshot['skipped_archives'][0]['status'] == 'not_available_at_observation'


@pytest.mark.parametrize('target', ['plan.json', 'observation-00000.json', 'code/core/timeutils.py'])
def test_changed_origin_member_is_rejected_without_network_fallback(cache_root, monkeypatch, target):
    folder = make_archive(cache_root)
    path = folder/target
    path.write_bytes(path.read_bytes()+b' ')
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **kw: pytest.fail('No fallback HTTP'))
    with pytest.raises(ValueError):
        capture()


def test_duplicate_key_and_incomplete_source_manifest_are_rejected(cache_root):
    folder = make_archive(cache_root)
    body = (folder/'result.json').read_bytes()
    (folder/'result.json').write_bytes(body[:-1]+b',"version":"extra"}')
    with pytest.raises(ValueError, match='کلید تکراری'):
        capture()
    (folder/'result.json').write_bytes(body)
    value = load(folder/'result.json')
    del value['artifacts_sha256']['observation-00000.json']
    write(folder/'result.json', value)
    with pytest.raises(ValueError):
        capture()


def test_task_type_identity_is_verified_against_current_ordinary_scope(cache_root):
    make_archive(cache_root, tasks=[task() | dict(instrument_type_code=303)])
    with pytest.raises(ValueError, match='types differ'):
        capture()


def test_frozen_replay_does_not_read_live_cache_or_follow_tampered_pointers(cache_root):
    make_archive(cache_root)
    snapshot, bodies = capture()
    replay(snapshot, bodies)
    broken = deepcopy(snapshot)
    broken['records'][0]['file'] = '../brs.db'
    calls = []
    with pytest.raises(ValueError):
        cache.replay_cache(broken, lambda name: (calls.append(name), bodies[name])[1], SCOPE, DAY, observed_at=AT)
    assert '../brs.db' not in calls
    for mutate in ('missing', 'duplicate', 'status', 'body'):
        broken, files = deepcopy(snapshot), dict(bodies)
        if mutate == 'missing': broken['records'].clear()
        elif mutate == 'duplicate': broken['records'].append(deepcopy(broken['records'][0]))
        elif mutate == 'status': broken['records'][0]['status'] = 'rejected'
        else:
            name = broken['records'][0]['file']
            files[name] += b' '
        with pytest.raises(ValueError):
            replay(broken, files)


def test_source_code_version_is_pinned_for_cache_capture_and_replay(cache_root, monkeypatch):
    from data_collector import daily_publications
    make_archive(cache_root)
    snapshot, bodies = capture()
    old = daily_publications._code()
    monkeypatch.setattr(daily_publications, '_code', lambda: old | {'core/timeutils.py': '0'*64})
    with pytest.raises(ValueError): capture()
    with pytest.raises(ValueError): replay(snapshot, bodies)


def test_path_scope_size_and_record_budgets_fail_closed(cache_root, monkeypatch):
    for invalid in (cache_root.parent, cache_root/'..'/'daily-publications', settings.DB_PATH):
        with pytest.raises(ValueError):
            cache.capture_cache(SCOPE, DAY, observed_at=AT, cache_root=invalid)
    make_archive(cache_root)
    monkeypatch.setattr(cache, 'MAX_TOTAL_BYTES', 10)
    with pytest.raises(ValueError, match='byte budget'):
        capture()


def test_symlink_or_windows_junction_escape_is_rejected(cache_root, tmp_path):
    outside = tmp_path/'outside'
    outside.mkdir()
    name = cache_root/('DAYPUB-'+'f'*32)
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(name), str(outside)], capture_output=True, check=False)
        if result.returncode != 0:
            pytest.skip('Windows junction creation unavailable')
    else:
        name.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink or junction'):
        capture()


def test_unknown_snapshot_fields_future_session_and_scope_tampering_rejected(cache_root):
    make_archive(cache_root)
    snapshot, bodies = capture()
    broken = deepcopy(snapshot) | dict(extra='unreviewed')
    with pytest.raises(ValueError): replay(broken, bodies)
    for scope in (SCOPE*2, [dict(ins_code=FOOLAD_INS, instrument_type_code=301)], []):
        with pytest.raises(ValueError):
            cache.capture_cache(scope, DAY, observed_at=AT)
    with pytest.raises(ValueError):
        cache.capture_cache(SCOPE, '2024-01-04', observed_at=AT)
