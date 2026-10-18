"""شواهد دریافت فقط روی داده آزمون و SQLite موقت بررسی می‌شوند."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.capture_evidence import receipts_from_cycle, snapshot_binding
from core.forward_observations import canonical, digest, event_hash, report_trial, validate_archive
from data_collector.scheduler import DataCollector
from database.forward_observations import ForwardRepository
from ops.intraday_audit import audit_intraday
from tests.test_capture_evidence import clock as clock
from tests.test_capture_evidence import collector, finish, start_run
from tests.test_forward_observations import assess, register, snapshot
from tests.test_forward_observations import future_clock as future_clock


def test_unchanged_poll_binds_original_row_without_refreshing_stored_or_price_clock(db, market_watch_rows, clock):
    from tests.conftest import FOOLAD_INS

    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run = start_run(db)
    source = collector(db, market_watch_rows)
    repo.append(run, 1, 'poll_started')
    first = source.collect_market_snapshot()
    finish(repo, run, 1, first)
    stored = dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    clock.at += timedelta(seconds=60)
    repo.append(run, 2, 'poll_started')
    second = source.collect_market_snapshot()
    finish(repo, run, 2, second)
    item = second['coverage']['per_instrument'][0]
    assert item['status'] == 'unchanged' and item['captured_at'] == clock.at.isoformat()
    assert item['snapshot_binding'] == first['coverage']['per_instrument'][0]['snapshot_binding'] == snapshot_binding(stored)
    assert dict(db.conn.execute('SELECT * FROM snapshots').fetchone()) == stored
    audit = audit_intraday(db)['capture']
    assert audit['bound_snapshot_receipts'] == 2 and audit['unbound_snapshot_receipts'] == 0
    assert audit['runs'][0]['per_symbol'][0]['price_time_fresh'] == 0


def test_unchanged_cache_does_not_certify_a_mutated_row_and_next_poll_repairs_binding(db, market_watch_rows, clock):
    from tests.conftest import FOOLAD_INS

    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    source = collector(db, market_watch_rows)
    source.collect_market_snapshot()
    with db.conn:
        db.conn.execute('UPDATE snapshots SET last_price=last_price+1')
    clock.at += timedelta(seconds=1)
    result = source.collect_market_snapshot()
    bad = result['coverage']['per_instrument'][0]
    assert bad['status'] == 'unchanged' and bad['snapshot_binding'] is None
    assert result['status'] == 'partial' and result['symbols_failed'] == 1
    clock.at += timedelta(seconds=1)
    good = source.collect_market_snapshot()['coverage']['per_instrument'][0]
    assert good['status'] == 'changed' and good['snapshot_binding'] is not None


def test_later_same_clock_revision_invalidates_live_link_but_preserves_frozen_observation(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at)
    assess(db, key)
    snapshot(db, future_clock.at, price=1200, receipt=False)
    frozen = repo.report(key)['rows'][0]['assessment']
    assert frozen['capture_receipt_verified'] and frozen['last_price_rial'] == 1000
    audit = audit_intraday(db, '2026-10-17')['capture']
    assert audit['bound_snapshot_receipts'] == 0 and audit['unbound_snapshot_receipts'] == 1
    assert audit['runs'][0]['per_symbol'][0]['snapshot_binding_conflict'] == 1


def test_raw_snapshot_without_a_matching_receipt_is_unknown_in_v2(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at, receipt=False)
    assess(db, key)
    quote = repo.report(key)['rows'][0]['assessment']
    assert quote['last_price_rial'] is None and not quote['capture_receipt_verified']
    assert quote['problems'] == ['capture_receipt_missing']


@pytest.mark.parametrize('status', ['failed', 'error', 'missing'])
def test_latest_failed_or_missing_receipt_blocks_older_good_quote(db, future_clock, status):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at)
    with patch('database.capture.now_tehran_iso', return_value=future_clock.at.isoformat()):
        capture, run = start_run(db, provider='tsetmc')
        capture.append(run, 1, 'poll_started')
        if status == 'missing':
            source = DataCollector(db=db, provider=SimpleNamespace(get_market_snapshots=lambda: []))
            finish(capture, run, 1, source.collect_market_snapshot())
        else:
            capture.append(run, 1, 'poll_finished', dict(status=status, coverage=None))
    assess(db, key)
    quote = repo.report(key)['rows'][0]['assessment']
    assert quote['last_price_rial'] is None and 'capture_observation_unavailable' in quote['problems']
    assert quote['capture_receipt']['run_id'] == run


@pytest.mark.parametrize('change', ['clock', 'scope', 'code', 'snapshot'])
def test_conflicting_receipt_is_not_promoted_to_a_known_price(db, future_clock, change):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    if change == 'scope':
        with db.conn:
            db.conn.execute("UPDATE symbols SET ins_code='changed-ins' WHERE symbol_fa='آزمون'")
        # فقط دامنه رسید تغییر می‌کند؛ شناسه ثبت‌شده برنامه ثابت می‌ماند.
    if change == 'code':
        with patch('database.capture.capture_code_identity', return_value=dict(files={}, sha256=digest({}))):
            snapshot(db, future_clock.at)
    elif change == 'scope':
        values = dict(captured_at=future_clock.at.isoformat(), provider_timestamp=future_clock.at.isoformat(),
                      last_price=1000, low_price=900, high_price=1300, ref_price=980, volume=10000, value=10000000)
        source = DataCollector(db=db, provider=SimpleNamespace(get_market_snapshots=lambda: [dict(values, ins_code='changed-ins')]))
        with patch('database.capture.now_tehran_iso', return_value=future_clock.at.isoformat()):
            capture, run = start_run(db, provider='tsetmc')
            capture.append(run, 1, 'poll_started')
            finish(capture, run, 1, source.collect_market_snapshot())
    else:
        snapshot(db, future_clock.at)
        if change == 'snapshot':
            snapshot(db, future_clock.at, price=1200, receipt=False)
        else:
            future_clock.at -= timedelta(seconds=1)
    assess(db, key)
    quote = repo.report(key)['rows'][0]['assessment']
    expected = {'clock': 'capture_receipt_time_invalid', 'scope': 'capture_scope_conflict',
                'code': 'capture_code_conflict', 'snapshot': 'snapshot_binding_conflict'}[change]
    assert expected in quote['problems'] and quote['last_price_rial'] is None


def test_receipt_does_not_refresh_an_old_price_clock(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at, provider_timestamp=(future_clock.at - timedelta(seconds=46)).isoformat())
    assess(db, key)
    quote = repo.report(key)['rows'][0]['assessment']
    assert quote['capture_receipt_verified'] and quote['last_price_rial'] is None
    assert quote['problems'] == ['provider_timestamp_stale']


@pytest.mark.parametrize('offset', [timedelta(seconds=-46), timedelta(days=1), timedelta(days=-1)])
def test_stale_or_other_day_receipt_cannot_fall_back_to_the_previous_good_one(db, future_clock, offset):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at)
    snapshot(db, future_clock.at + offset)
    assess(db, key)
    quote = repo.report(key)['rows'][0]['assessment']
    assert quote['last_price_rial'] is None and not quote['capture_receipt_verified']
    assert any(p in quote['problems'] for p in ('capture_receipt_stale', 'capture_receipt_time_invalid'))


def test_capture_code_change_stops_new_polls_but_allows_stop_receipt(db, clock, monkeypatch):
    db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    repo, run = start_run(db)
    monkeypatch.setattr('database.capture.capture_code_identity', lambda: dict(files={}, sha256=digest({})))
    with pytest.raises(ValueError, match='کد دریافت'):
        repo.append(run, 1, 'poll_started')
    repo.append(run, 0, 'run_stopped', dict(reason='error'))
    assert db.conn.execute('SELECT COUNT(*) FROM capture_events').fetchone()[0] == 1


def test_legacy_capture_is_readable_but_does_not_get_a_v2_binding(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    with patch('database.capture.CAPTURE_VERSION', 'capture-protocol-v1'):
        snapshot(db, future_clock.at)
    assess(db, key)
    quote = repo.report(key)['rows'][0]['assessment']
    assert quote['last_price_rial'] is None and 'snapshot_binding_missing' in quote['problems']
    audit = audit_intraday(db, '2026-10-17')['capture']
    assert audit['unbound_snapshot_receipts'] == 1 and audit['bound_snapshot_receipts'] == 0
    assert audit['runs'][0]['capture_version'] == 'capture-protocol-v1'


def test_legacy_forward_archive_replays_under_its_original_quote_contract(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    trial = json.loads(repo.export(key))['trial']
    protocol = json.loads(trial['protocol_json'])
    protocol['version'] = 'forward-observation-v1'
    protocol.pop('capture_code')
    trial.update(protocol_json=canonical(protocol), protocol_sha256=digest(protocol))
    snapshot(db, future_clock.at.replace(hour=9, minute=6), receipt=False)
    stored = dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    quote = dict(snapshot=stored, problems=[], last_price_rial=1000,
                 independent_receipt_verified=False, executable=False)
    payload = dict(code_sha256=protocol['code']['sha256'], rows=[
        dict(symbol_id=protocol['scope'][0]['symbol_id'], quote=quote, choice='watch', rationale='قاعده قدیمی'),
        dict(symbol_id=protocol['scope'][1]['symbol_id'], quote=dict(snapshot=None, problems=['missing_snapshot'],
            last_price_rial=None, independent_receipt_verified=False, executable=False), choice='not_reviewed', rationale='')])
    at = stored['captured_at']
    event = dict(trial_id=key, phase='assessment', recorded_at=at, payload_json=canonical(payload),
                 previous_sha256=trial['protocol_sha256'],
                 event_sha256=event_hash(key, 'assessment', at, payload, trial['protocol_sha256']))
    report = report_trial(trial, [event], at)
    assert report['version'] == 'forward-observation-v1' and report['rows'][0]['assessment'] == quote
    assert report['receipt_bound_quote_pairs'] == 0 and not report['accepted_for_signals']


def test_cycle_evidence_is_shared_and_export_replay_needs_no_live_database(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at)
    assess(db, key)
    future_clock.at = future_clock.at.replace(hour=12, minute=1)
    snapshot(db, future_clock.at, price=1100)
    repo.record(key, 'followup')
    saved = json.loads(repo.export(key))
    for event in saved['events']:
        payload = json.loads(event['payload_json'])
        assert len(payload['capture_cycles']) == 1 and len(payload['rows']) == 2
        assert len(receipts_from_cycle(payload['capture_cycles'][0])) == 2
    assert saved['report']['receipt_bound_quote_pairs'] == 1
    db.close()
    assert report_trial(saved['trial'], saved['events'], future_clock.at.isoformat()) == saved['report']
    # حتی اگر هش بیرونی رویداد دوباره ساخته شود، ناسازگاری درونی نقل قیمت پذیرفته نیست.
    event = saved['events'][0]
    payload = json.loads(event['payload_json'])
    payload['rows'][0]['quote']['snapshot']['value'] += 1
    event['payload_json'] = canonical(payload)
    event['event_sha256'] = event_hash(key, 'assessment', event['recorded_at'], payload, event['previous_sha256'])
    with pytest.raises(ValueError, match='کیفیت نقل قیمت'):
        validate_archive(saved['trial'], saved['events'][:1])
