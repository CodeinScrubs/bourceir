"""Polling evidence must distinguish unchanged quotes, missing scope and unknown outcomes."""

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.timeutils import TEHRAN_TZ
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from database.capture import CaptureRepository
from database.db import DatabaseManager
from ops.intraday_audit import audit_intraday
from tests.conftest import FOOLAD_INS, FakeSession
from tests.test_dashboard import DASHBOARD
from tests.test_dashboard import dashboard_db as dashboard_db


def collector(db, rows):
    return DataCollector(db=db, provider=TsetmcProvider(session=FakeSession({
        'GetMarketWatch': {'marketwatch': rows}, 'GetClientTypeAll': {}})))


def add_scope(db):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='missing', instrument_type_code=303))


def test_response_without_any_requested_instrument_is_not_success(db, market_watch_rows):
    add_scope(db)
    result = collector(db, market_watch_rows).collect_market_snapshot(symbols=['آزمون'])
    assert result['status'] == 'failed'
    assert result['coverage']['missing'] == 1 and result['symbols_requested'] == 1
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0


def test_partial_response_keeps_missing_instruments_in_denominator(db, market_watch_rows):
    add_scope(db)
    result = collector(db, market_watch_rows).collect_market_snapshot()
    assert result['status'] == 'partial'
    assert result['coverage']['expected'] == 2 and result['coverage']['valid'] == 1
    assert result['coverage']['missing'] == 1 and result['symbols_failed'] == 1


def test_duplicate_instrument_rows_are_quarantined_as_ambiguous(db, market_watch_rows):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    result = collector(db, [market_watch_rows[0], market_watch_rows[0]]).collect_market_snapshot()
    assert result['status'] == 'failed' and result['coverage']['duplicate'] == 1
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(at=datetime(2026, 10, 14, 9, 0, tzinfo=TEHRAN_TZ))
    for name in ('database.capture', 'data_collector.scheduler', 'data_collector.tsetmc'):
        monkeypatch.setattr(name + '.now_tehran_iso', lambda: value.at.isoformat())
    monkeypatch.setattr('core.timeutils.now_tehran', lambda: value.at)
    return value


def start_run(db, **kwargs):
    repo = CaptureRepository(db)
    params = dict(interval=30, cycles=0, forced=False, provider='test_only', explicit_scope=False)
    params.update(kwargs)
    run_id = repo.start(db.get_universe_symbols(), **params)
    return repo, run_id


def finish(repo, run_id, cycle, report):
    repo.append(run_id, cycle, 'poll_finished', dict(status=report['status'], coverage=report['coverage'], elapsed_seconds=0))


def test_unchanged_receipts_are_counted_independently_of_stored_changes(db, market_watch_rows, clock):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run_id = start_run(db)
    source = collector(db, market_watch_rows)
    for cycle in range(1, 5):
        if cycle == 4:
            market_watch_rows[0]['blDs'][1]['qmd'] += 1
        repo.append(run_id, cycle, 'poll_started')
        finish(repo, run_id, cycle, source.collect_market_snapshot())
        clock.at += timedelta(seconds=30)
    repo.append(run_id, 0, 'run_stopped', dict(reason='cycles_completed'))
    report = audit_intraday(db, '2026-10-14')
    run = report['capture']['runs'][0]
    assert report['rows'] == 2 and report['per_symbol'][0]['max_gap_seconds'] == 90
    assert run['attempted_polls'] == run['complete_scope_polls'] == 4
    assert run['max_poll_start_gap_seconds'] == 30
    assert run['per_symbol'][0]['max_valid_poll_gap_seconds'] == 30
    assert run['per_symbol'][0]['unchanged'] == 2 and run['per_symbol'][0]['valid'] == 4
    assert not run['full_session_coverage_known'] and not report['prospective_independence']


def test_unchanged_valid_symbol_plus_missing_symbol_is_partial(db, market_watch_rows):
    add_scope(db)
    source = collector(db, market_watch_rows)
    source.collect_market_snapshot()
    second = source.collect_market_snapshot()
    assert second['symbols_succeeded'] == 0 and second['status'] == 'partial'
    assert second['coverage']['unchanged'] == 1 and second['coverage']['missing'] == 1


def test_empty_provider_response_retains_whole_scope_and_no_fake_snapshots(db):
    add_scope(db)
    result = collector(db, []).collect_market_snapshot()
    assert result['status'] == 'failed' and result['symbols_requested'] == 2
    assert result['coverage']['missing'] == 2 and result['coverage']['valid'] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0


def test_capture_scope_is_frozen_when_current_metadata_grows(db, market_watch_rows, clock):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    instruments = db.get_universe_symbols()
    repo, run_id = start_run(db)
    db.upsert_symbol(dict(symbol_fa='جدید', ins_code='new', instrument_type_code=303))
    repo.append(run_id, 1, 'poll_started')
    finish(repo, run_id, 1, collector(db, market_watch_rows).collect_market_snapshot(instruments=instruments))
    run = audit_intraday(db)['capture']['runs'][0]
    assert run['expected_instruments'] == 1 and run['complete_scope_polls'] == 1
    assert len(db.get_universe_symbols()) == 2


def test_unfinished_request_and_missing_stop_are_unknown_after_reopening(db, clock):
    add_scope(db)
    repo, run_id = start_run(db)
    repo.append(run_id, 1, 'poll_started')
    path = db.db_path
    db.close()
    with DatabaseManager(db_path=path, read_only=True) as reader:
        report = audit_intraday(reader)
    assert report['status'] == 'capture_only' and report['rows'] == 0
    run = report['capture']['runs'][0]
    assert run['pending_polls'] == 1 and run['completed_polls'] == 0
    assert run['stop_reason'] is None
    assert all(row['outcome_unknown'] == 1 for row in run['per_symbol'])


def test_protocol_without_even_a_first_request_is_visible_in_latest_audit(db, clock):
    add_scope(db)
    start_run(db)
    report = audit_intraday(db)
    assert report['session_date'] == '2026-10-14' and report['capture']['protocols'] == 1
    assert report['capture']['attempted_polls'] == 0 and report['capture']['runs'][0]['stop_reason'] is None


def test_protocol_and_events_are_immutable_and_missing_starts_are_rejected(db, clock):
    add_scope(db)
    repo, run_id = start_run(db)
    with pytest.raises(ValueError, match='شروع'):
        repo.append(run_id, 1, 'poll_finished', dict(status='failed'))
    repo.append(run_id, 1, 'poll_started')
    for statement in ('DELETE FROM capture_runs', 'UPDATE capture_runs SET started_at="x"',
                      'DELETE FROM capture_events', 'UPDATE capture_events SET cycle=7'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            with db.conn:
                db.conn.execute(statement)
    with pytest.raises(ValueError, match='پیوسته'):
        repo.append(run_id, 3, 'poll_started')
    with pytest.raises(ValueError, match='قبلاً'):
        repo.append(run_id, 1, 'market_closed')
    repo.append(run_id, 0, 'run_stopped', dict(reason='error'))
    with pytest.raises(ValueError, match='پایان'):
        repo.append(run_id, 2, 'poll_started')


def test_capture_does_not_relabel_legacy_snapshots_as_prospective(db):
    from tests.test_audit_followup import live_book
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    _, book = live_book()
    db.insert_snapshot(sid, book)
    report = audit_intraday(db)
    assert report['capture']['status'] == 'not_recorded'
    assert report['capture']['protocols'] == 0 and not report['prospective_independence']


def test_clock_reversal_is_reported_instead_of_sorted_away(db, market_watch_rows, clock):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run_id = start_run(db)
    repo.append(run_id, 1, 'poll_started')
    clock.at -= timedelta(seconds=20)
    finish(repo, run_id, 1, collector(db, market_watch_rows).collect_market_snapshot())
    run = audit_intraday(db)['capture']['runs'][0]
    assert run['wall_clock_inconsistencies'] == 1 and not run['full_session_coverage_known']


@pytest.mark.parametrize('bad', ['duplicate', 'wrong_scope', 'unknown_status'])
def test_audit_does_not_accept_inconsistent_event_counts(db, market_watch_rows, clock, bad):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run_id = start_run(db)
    repo.append(run_id, 1, 'poll_started')
    report = collector(db, market_watch_rows).collect_market_snapshot()
    if bad == 'duplicate':
        report['coverage']['per_instrument'] *= 2
    elif bad == 'wrong_scope':
        report['coverage']['per_instrument'][0]['ins_code'] = 'different'
    else:
        report['coverage']['per_instrument'][0]['status'] = 'invented'
    finish(repo, run_id, 1, report)
    with pytest.raises(ValueError, match='سازگار'):
        audit_intraday(db)


@pytest.mark.parametrize('failure', [None, RuntimeError, KeyboardInterrupt])
def test_cli_records_actual_poll_or_failure_and_always_closes_database(db, market_watch_rows, monkeypatch, clock, failure):
    import brs
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    source = collector(db, market_watch_rows)
    if failure:
        def fail(**kwargs):
            raise failure('test-only interruption')
        monkeypatch.setattr(source, 'collect_market_snapshot', fail)
    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kw: source)
    args = SimpleNamespace(symbols='', interval=30, cycles=1, force=True)
    if failure is RuntimeError:
        with pytest.raises(RuntimeError):
            brs.cmd_live(args)
    else:
        assert brs.cmd_live(args) == (130 if failure else 0)
    assert db.conn is None
    with DatabaseManager(db_path=db.db_path, read_only=True) as reader:
        run = audit_intraday(reader)['capture']['runs'][0]
    assert run['attempted_polls'] == 1 and run['forced']
    assert run['pending_polls'] == (1 if failure is KeyboardInterrupt else 0)
    assert run['stop_reason'] == ('user_interrupted' if failure is KeyboardInterrupt else 'error' if failure else 'cycles_completed')
    if failure is RuntimeError:
        assert run['status_counts']['error'] == 1 and run['per_symbol'][0]['outcome_unknown'] == 1


@pytest.mark.parametrize('partial', [True, False])
def test_cli_partial_and_failed_capture_exit_nonzero(db, market_watch_rows, monkeypatch, clock, partial):
    import brs
    add_scope(db)
    source = collector(db, market_watch_rows if partial else [])
    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kw: source)
    assert brs.cmd_live(SimpleNamespace(symbols='', interval=30, cycles=1, force=True)) == 1
    with DatabaseManager(db_path=db.db_path, read_only=True) as reader:
        run = audit_intraday(reader)['capture']['runs'][0]
    assert run['status_counts']['partial' if partial else 'failed'] == 1
    assert run['complete_scope_polls'] == 0


def test_special_open_weekday_override_reaches_polling_loop(db, market_watch_rows, monkeypatch, clock):
    import brs
    clock.at = datetime(2026, 10, 16, 10, tzinfo=TEHRAN_TZ)  # Friday; only an explicit recorded exception opens it.
    db.insert_market_calendar('2026-10-16', True, holiday_reason='synthetic exception')
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kw: collector(db, market_watch_rows))
    assert brs.cmd_live(SimpleNamespace(symbols='', interval=30, cycles=1, force=False)) == 0
    with DatabaseManager(db_path=db.db_path, read_only=True) as reader:
        run = audit_intraday(reader)['capture']['runs'][0]
    assert run['attempted_polls'] == 1 and run['calendar_date_explicit'] and not run['forced']


def test_migration_from_v10_preserves_records_and_backs_up_before_changes(tmp_path, monkeypatch):
    from database import migrator
    path = tmp_path / 'legacy.db'
    migrations = migrator.discover_migrations()
    with monkeypatch.context() as old:
        old.setattr(migrator, 'discover_migrations', lambda: migrations[:-1])
        with DatabaseManager(db_path=path) as database:
            database.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
            assert database.schema_version() == 10
    with DatabaseManager(db_path=path, read_only=True) as database:
        assert audit_intraday(database)['capture']['status'] == 'schema_unavailable'
    with DatabaseManager(db_path=path) as database:
        assert database.schema_version() == 11 and database.get_symbol('آزمون')
        assert migrator.apply_migrations(database.conn) == []
        # The migration itself also tolerates being executed twice.
        migrator._run_sql_migration(database.conn, migrations[-1][2])
        database.conn.commit()
        assert database.conn.execute('SELECT COUNT(*) FROM capture_runs').fetchone()[0] == 0
    backups = list((tmp_path / 'backups').glob('*-v10.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 10
        assert backup.execute('SELECT COUNT(*) FROM symbols').fetchone()[0] == 1


def test_health_page_explains_polling_evidence_without_claiming_complete_session(dashboard_db, market_watch_rows, clock):
    from streamlit.testing.v1 import AppTest
    with DatabaseManager(db_path=dashboard_db) as database:
        database.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
        repo, run_id = start_run(database)
        repo.append(run_id, 1, 'poll_started')
        finish(repo, run_id, 1, collector(database, market_watch_rows).collect_market_snapshot())
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🩺 سلامت سامانه').run()
    next(button for button in app.button if button.label == 'بررسی کیفیت آخرین جلسه ثبت‌شده').click().run()
    assert not app.exception
    assert any('ثبت مستقل دریافت: 1 شروع' in text.value for text in app.markdown)
    assert any('پوشش کل جلسه' in text.value for text in app.caption)


def test_no_request_is_sent_if_recording_the_start_fails(db, monkeypatch, clock):
    import brs
    db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    calls = []
    source = SimpleNamespace(db=db, provider=SimpleNamespace(name='test_only'),
                             collect_market_snapshot=lambda **kw: calls.append(True))
    original = CaptureRepository.append
    def fail_start(self, run_id, cycle, event, payload=None):
        if event == 'poll_started':
            raise RuntimeError('test-only journal failure')
        return original(self, run_id, cycle, event, payload)
    monkeypatch.setattr(CaptureRepository, 'append', fail_start)
    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kw: source)
    with pytest.raises(RuntimeError, match='journal failure'):
        brs.cmd_live(SimpleNamespace(symbols='', interval=30, cycles=1, force=True))
    assert not calls and db.conn is None


def test_unknown_explicit_symbol_aborts_without_creating_a_run(db, monkeypatch, clock):
    import brs
    source = collector(db, [])
    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kw: source)
    with pytest.raises(ValueError, match='شناسایی نشد'):
        brs.cmd_live(SimpleNamespace(symbols='نامعتبر', interval=30, cycles=1, force=True))
    with DatabaseManager(db_path=db.db_path, read_only=True) as reader:
        assert reader.conn.execute('SELECT COUNT(*) FROM capture_runs').fetchone()[0] == 0


def test_explicit_inactive_instrument_is_retained_and_alias_is_normalized(db, market_watch_rows, monkeypatch, clock):
    import brs
    db.upsert_symbol(dict(symbol_fa='خزامیا', ins_code='33333333333333333', instrument_type_code=300, is_active=False))
    source = collector(db, market_watch_rows)
    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kw: source)
    assert brs.cmd_live(SimpleNamespace(symbols='خزاميا,خزامیا', interval=30, cycles=1, force=True)) == 0
    with DatabaseManager(db_path=db.db_path, read_only=True) as reader:
        run = audit_intraday(reader)['capture']['runs'][0]
    assert run['scope_kind'] == 'explicit' and run['per_symbol'][0]['symbol'] == 'خزامیا'


def test_provider_age_uses_independent_finish_instead_of_a_stale_capture_clock(db, market_watch_rows, clock):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run_id = start_run(db)
    repo.append(run_id, 1, 'poll_started')
    report = collector(db, market_watch_rows).collect_market_snapshot()
    stale = (clock.at - timedelta(hours=1)).isoformat()
    report['coverage']['per_instrument'][0].update(captured_at=stale, provider_timestamp=stale)
    finish(repo, run_id, 1, report)
    item = audit_intraday(db)['capture']['runs'][0]['per_symbol'][0]
    assert item['price_time_stale'] == 1 and item['price_time_fresh'] == 0


@pytest.mark.parametrize('forced', [False, True])
def test_two_window_endpoints_do_not_establish_full_session_coverage(db, market_watch_rows, clock, forced):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run_id = start_run(db, forced=forced)
    source = collector(db, market_watch_rows)
    for cycle, stamp in enumerate((clock.at, clock.at.replace(hour=12, minute=29, second=59)), start=1):
        clock.at = stamp
        repo.append(run_id, cycle, 'poll_started')
        finish(repo, run_id, cycle, source.collect_market_snapshot())
    run = audit_intraday(db)['capture']['runs'][0]
    assert run['configured_window_bracketed'] is (not forced)
    assert run['max_poll_start_gap_seconds'] > 12_000 and not run['full_session_coverage_known']
