"""Recorded source selection remains independent of returns and personal data."""

import json
import sqlite3
import zipfile

import pytest

from config import settings
from ops.research_bundle import export_research_bundle, latest_baseline_source, main

pytest_plugins = ['tests.test_flow_research']

SOURCE = 'EXP-2024-01-01-BASELINES-'+'a'*32


@pytest.mark.parametrize('value', [
    [], None, {'experiment_id': None}, {'experiment_id': 1},
    {'experiment_id': '../private/account'}, {'experiment_id': 'EXP-2024-01-01-EXPECTANCY-'+'a'*32},
    {'experiment_id': 'EXP-2024-02-31-BASELINES-'+'a'*32},
    {'experiment_id': SOURCE, 'choose_best_return': True},
])
def test_invalid_latest_pointer_is_not_a_source(value, tmp_path):
    (tmp_path/'latest.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):
        latest_baseline_source(root=tmp_path)


def test_missing_pointer_never_scans_other_experiments(tmp_path):
    (tmp_path/(SOURCE+'.json')).write_text('{}')
    with pytest.raises(ValueError, match='آرشیو BASELINES'):
        latest_baseline_source(root=tmp_path)


def test_duplicate_pointer_key_is_rejected(tmp_path):
    (tmp_path/'latest.json').write_text('{"experiment_id":"bad","experiment_id":"'+SOURCE+'"}')
    with pytest.raises(ValueError, match='تکراری'):
        latest_baseline_source(root=tmp_path)


def test_explicit_latest_cli_cannot_create_an_empty_database(tmp_path, monkeypatch):
    database = tmp_path/'absent.db'
    monkeypatch.setattr(settings, 'DB_PATH', database)
    with pytest.raises(ValueError, match='دیتابیس موجود'):
        main(['--latest'])
    assert not database.exists()


@pytest.mark.parametrize('arguments', [[], ['--latest', '--source', SOURCE]])
def test_cli_requires_exactly_one_source_mode(arguments):
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_latest_export_retains_registration_hash_scope_and_no_account_reads(db, flow_baseline):
    source, root = flow_baseline
    selected = latest_baseline_source(root=root/'research')
    assert selected == source['experiment_id']
    def guard(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table != 'experiments' else sqlite3.SQLITE_OK
    db.conn.set_authorizer(guard)
    try:
        exported = export_research_bundle(db, selected)
    finally:
        db.conn.set_authorizer(None)
    with zipfile.ZipFile(exported['path']) as archive:
        manifest = json.loads(archive.read('bundle-manifest.json'))
        assert manifest['source_id'] == selected
        assert not manifest['account_tables_included']
        assert all(not name.endswith(('.db', '.env', '.py')) for name in archive.namelist())
    # Latest source resolution must not weaken the exporter's input validation.
    prices = root/'research'/selected/'prices.parquet'
    prices.write_bytes(prices.read_bytes()+b'changed')
    with pytest.raises(ValueError):
        export_research_bundle(db, latest_baseline_source(root=root/'research'))


def test_cli_uses_readonly_database_and_exact_resolved_source(tmp_path, monkeypatch, capsys):
    from database import db as db_module
    from ops import research_bundle

    database = tmp_path/'existing.db'
    database.touch()
    monkeypatch.setattr(settings, 'DB_PATH', database)
    monkeypatch.setattr(research_bundle, 'latest_baseline_source', lambda: SOURCE)
    calls = []
    class ReadOnlyDatabase:
        def __init__(self, *, read_only):
            calls.append(read_only)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
    monkeypatch.setattr(db_module, 'DatabaseManager', ReadOnlyDatabase)
    def export(db, source, *, output):
        assert isinstance(db, ReadOnlyDatabase) and source == SOURCE and output is None
        return {'path': str(tmp_path/'market.zip')}
    monkeypatch.setattr(research_bundle, 'export_research_bundle', export)
    assert main(['--latest']) == 0
    assert calls == [True]
    assert capsys.readouterr().out.strip() == str(tmp_path/'market.zip')
