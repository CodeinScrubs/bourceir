"""Readiness must retain unknowns, preserve the database and exclude private account content."""

import json
import sqlite3

from config import settings
from ops.trader_readiness import collect_readiness, main
from tests.test_research_preflight import store


def test_no_data_is_blocked_and_database_is_not_modified(db):
    before = db.conn.total_changes
    report = collect_readiness(db)
    assert report['status'] == 'blocked' and report['development']['price_rows'] == 0
    assert not report['engineering_and_input_contract_passed']
    assert not report['accepted_for_signals'] and not report['profitability_proven']
    assert db.conn.total_changes == before and not db.conn.in_transaction


def test_passing_input_contract_is_research_only_and_account_content_is_not_queried(db, monkeypatch):
    store(db)
    monkeypatch.setattr('ops.trader_readiness.inspect_environment', lambda *_: dict(ready=True))
    statements = []
    db.conn.set_trace_callback(statements.append)
    before = db.conn.total_changes
    report = collect_readiness(db)
    db.conn.set_trace_callback(None)
    assert report['status'] == 'research_only' and report['engineering_and_input_contract_passed']
    assert report['development']['coherent_client_pairs'] == 25
    assert report['public_market_snapshot_counts']['rows'] == 0
    assert not report['execution_proven'] and not report['profitability_proven']
    assert report['owner_inputs_needed'] == [] and not report['private_owner_profile_read']
    assert 'private_owner_profile' in report['not_assessed'] and db.conn.total_changes == before
    assert 'price_issues' not in report['development'] and 'records' not in report['development']
    for prefix in ('personal_', 'broker_', 'journal_', 'paper_', 'capture_'):
        assert not any(prefix in statement.lower() for statement in statements)
    body = json.dumps(report, allow_nan=False)
    assert str(settings.DB_PATH) not in body and 'TYPESAFE_API_KEY' not in body


def test_missing_database_is_not_created_and_report_is_saved_privately(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'missing.db')
    output = tmp_path / 'private' / 'readiness.json'
    assert main(['--json', str(output)]) == 2
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'blocked' and report['error_type'] == 'OperationalError'
    assert not settings.DB_PATH.exists() and not report['accepted_for_signals']


def test_cli_adapter_exposes_the_same_offline_contract(tmp_path, monkeypatch):
    from brs import build_parser

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'missing.db')
    output = tmp_path / 'private' / 'readiness.json'
    args = build_parser().parse_args(['trader-readiness', '--json', str(output)])
    assert args.func(args) == 2
    assert json.loads(output.read_text(encoding='utf-8'))['network_checked'] is False


def test_read_only_doctor_does_not_initialize_missing_database(tmp_path, monkeypatch):
    from ops.doctor import DoctorReport, check_database

    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'missing.db')
    report = DoctorReport()
    check_database(report, read_only=True)
    assert report.worst == 'error' and not settings.DB_PATH.exists()


def test_read_only_doctor_does_not_upgrade_an_old_schema(tmp_path, monkeypatch):
    from ops.doctor import DoctorReport, check_database

    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE legacy_marker(value TEXT)')
        conn.execute("INSERT INTO legacy_marker VALUES ('original')")
    before = path.read_bytes()
    monkeypatch.setattr(settings, 'DB_PATH', path)
    report = DoctorReport()
    check_database(report, read_only=True)
    assert report.worst == 'error' and any('ساختار' in row.title for row in report.checks)
    assert path.read_bytes() == before


def test_readiness_environment_failure_is_explicit_and_never_approves_signals(db, monkeypatch):
    store(db)
    monkeypatch.setattr('ops.trader_readiness.inspect_environment', lambda *_: dict(ready=False))
    report = collect_readiness(db)
    assert report['status'] == 'blocked' and report['development']['input_contract_passed']
    assert 'runtime_environment_not_ready' in report['blockers']
    assert not report['accepted_for_signals'] and not report['network_checked']


def test_report_rejects_code_changed_during_readiness(db, monkeypatch):
    import pytest

    store(db)
    identities = iter([{'code': 'before'}, {'code': 'after'}])
    monkeypatch.setattr('ops.trader_readiness.code_identity', lambda: next(identities))
    with pytest.raises(ValueError, match='کد'):
        collect_readiness(db)


def test_read_only_doctor_on_existing_database_does_not_query_account_content(db, monkeypatch):
    from ops.doctor import DoctorReport

    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    import ops.doctor  # اتصال همین دیتابیس موقت را با ضبط SQL به doctor می‌دهیم.
    statements = []
    db.conn.set_trace_callback(statements.append)
    monkeypatch.setattr('database.db.DatabaseManager', lambda **_: db)
    report = DoctorReport()
    ops.doctor.check_database(report, read_only=True)
    assert report.worst != 'error'
    for prefix in ('personal_', 'broker_', 'journal_', 'paper_', 'capture_'):
        assert not any(prefix in statement.lower() for statement in statements)


def test_doctor_report_cannot_overwrite_database_before_checks(tmp_path, monkeypatch):
    import pytest

    from brs import build_parser

    target = tmp_path / 'database.json'
    target.write_bytes(b'original database marker')
    monkeypatch.setattr(settings, 'DB_PATH', target)
    def forbidden(**_): raise AssertionError('checks must not run for an invalid output')
    monkeypatch.setattr('ops.doctor.run_doctor', forbidden)
    args = build_parser().parse_args(['doctor', '--offline', '--read-only', '--json', str(target)])
    with pytest.raises(ValueError):
        args.func(args)
    assert target.read_bytes() == b'original database marker'


def test_stale_schema_receipt_blocks_readiness_without_migrating(db, monkeypatch):
    store(db)
    with db.conn:
        db.conn.execute('DELETE FROM schema_migrations WHERE version=14')
    monkeypatch.setattr('ops.trader_readiness.inspect_environment', lambda *_: dict(ready=True))
    before = db.conn.total_changes
    report = collect_readiness(db)
    assert not report['schema_current'] and report['status'] == 'blocked'
    assert 'database_schema_not_current' in report['blockers']
    assert db.conn.total_changes == before and db.schema_version() == 13


def test_intraday_report_cannot_overwrite_database_before_checks(tmp_path, monkeypatch):
    import pytest

    from brs import build_parser

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    target = tmp_path / 'private' / 'database.json'
    target.parent.mkdir()
    target.write_bytes(b'original database marker')
    monkeypatch.setattr(settings, 'DB_PATH', target)
    args = build_parser().parse_args(['intraday-audit', '--report', str(target)])
    with pytest.raises(ValueError):
        args.func(args)
    assert target.read_bytes() == b'original database marker'
