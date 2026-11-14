"""Atomic trial registration, crash visibility and repeat accounting."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from config import settings
from database.db import DatabaseManager
from database.migrator import _run_sql_migration, apply_migrations
from research.search_ledger import (
    finish_trials,
    identity,
    ledger_path,
    reserve_trials,
    summarize_ledger,
    validate_registration,
)

CONTRACT = dict(data_sha256='a' * 64, code_sha256='b' * 64, decision_clock='close_t', execution='t+1',
                fees=dict(buy=.003712, sell=.0088), environment=dict(python='3.11'),
                calendar=['2023-01-01', '2023-01-02'], initial_capital_rial=3000000000)
POLICIES = [dict(name='momentum20', policy=dict(selector='momentum', window=20, horizon=2, seed=41))]


@pytest.fixture(autouse=True)
def private_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'market.db')


def reserve(run='run-1', **kwargs):
    return reserve_trials(CONTRACT, POLICIES, run_id=run, **kwargs)


def test_summary_is_read_only_and_does_not_create_database_or_claim_coverage():
    result = summarize_ledger()
    assert result['status'] == 'not_created' and not ledger_path().exists()
    assert result['earlier_history_complete'] is False
    assert not settings.DB_PATH.exists()


def test_full_lifecycle_preserves_failed_runs_and_counts_repeat_as_one_policy():
    first = reserve()
    assert validate_registration(CONTRACT, POLICIES, first)
    assert summarize_ledger()['status_counts'] == {'unfinished': 1}
    finish_trials(first, status='failed', evidence={'error_type': 'ValueError'})
    with pytest.raises(ValueError, match='explicit repeat'):
        reserve('run-2')
    second = reserve('run-2', allow_repeat=True)
    assert second['registrations'][0]['previous_attempts'] == 1
    assert first['registrations'][0]['trial_id'] == second['registrations'][0]['trial_id']
    finish_trials(second, status='complete', evidence={'manifest_sha256': 'c' * 64})
    finish_trials(second, status='complete', evidence={'manifest_sha256': 'c' * 64})
    report = summarize_ledger()
    assert report['registered_attempts'] == 2 and report['unique_policy_contracts'] == 1
    assert report['repeat_attempts'] == 1 and report['status_counts'] == {'complete': 1, 'failed': 1}
    assert not report['statistical_trial_count_verified'] and not settings.DB_PATH.exists()


def test_hard_crash_stays_unfinished_and_even_explicit_repeat_cannot_duplicate_it():
    first = reserve()
    with pytest.raises(ValueError, match='unfinished'):
        reserve('run-2', allow_repeat=True)
    assert summarize_ledger()['registered_attempts'] == 1
    finish_trials(first, status='interrupted', evidence={'reason': 'operator confirmed process stopped'})
    assert reserve('run-2', allow_repeat=True)['registrations'][0]['previous_attempts'] == 1


def test_batch_duplicate_or_budget_failure_rolls_back_every_new_policy():
    first = reserve()
    finish_trials(first, status='complete', evidence={'hash': 'c' * 64})
    batch = [dict(name='new', policy={'window': 5}), *POLICIES]
    with pytest.raises(ValueError, match='explicit repeat'):
        reserve_trials(CONTRACT, batch, run_id='run-2')
    assert summarize_ledger()['registered_attempts'] == 1
    assert summarize_ledger()['unique_policy_contracts'] == 1
    with pytest.raises(ValueError, match='budget'):
        reserve_trials(CONTRACT, batch, run_id='run-2', allow_repeat=True, attempt_budget=2)
    assert summarize_ledger()['registered_attempts'] == 1


def test_alias_and_json_key_order_do_not_bypass_duplicate_detection():
    first = reserve()
    finish_trials(first, status='complete', evidence={'hash': 'c' * 64})
    aliases = [dict(name='a different label', policy=dict(reversed(list(POLICIES[0]['policy'].items()))))]
    with pytest.raises(ValueError, match='explicit repeat'):
        reserve_trials(dict(reversed(list(CONTRACT.items()))), aliases, run_id='alias')
    with pytest.raises(ValueError, match='aliases'):
        reserve_trials(CONTRACT, [*POLICIES, *aliases], run_id='batch')


@pytest.mark.parametrize('field,value', [('data_sha256', 'd' * 64), ('code_sha256', 'e' * 64),
    ('initial_capital_rial', 3000000001), ('execution', 'next_open'), ('fees', {'buy': .004, 'sell': .009}),
    ('environment', {'python': '3.11.99'}), ('calendar', ['2023-01-01', '2023-01-03'])])
def test_contract_changes_are_distinct_but_old_attempts_are_never_forgotten(field, value):
    reserve()
    changed = dict(CONTRACT, **{field: value})
    second = reserve_trials(changed, POLICIES, run_id='changed')
    assert second['contract_id'] != identity(CONTRACT)
    assert summarize_ledger()['registered_attempts'] == 2


def test_two_workers_cannot_start_the_same_policy_twice():
    # Initialize once, then race admission itself (BEGIN IMMEDIATE), not schema creation.
    with DatabaseManager(ledger_path()):
        pass
    barrier = Barrier(2)
    def worker(n):
        barrier.wait()
        try:
            return reserve(f'worker-{n}')
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, (1, 2)))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert sum(isinstance(result, str) and 'unfinished' in result for result in outcomes) == 1
    assert summarize_ledger()['registered_attempts'] == 1


def test_completion_is_immutable_and_whole_batch_is_checked():
    receipt = reserve_trials(CONTRACT, [*POLICIES, dict(name='other', policy={'window': 5})], run_id='batch')
    forged = json.loads(json.dumps(receipt))
    forged['registrations'][-1]['attempt_id'] = 'f' * 32
    with pytest.raises(ValueError, match='does not match'):
        finish_trials(forged, status='complete', evidence={'hash': 'c' * 64})
    assert summarize_ledger()['status_counts'] == {'unfinished': 2}
    finish_trials(receipt, status='complete', evidence={'hash': 'c' * 64})
    with pytest.raises(ValueError, match='overwritten'):
        finish_trials(receipt, status='failed', evidence={'hash': 'd' * 64})
    with DatabaseManager(ledger_path()) as db:
        for table in ('search_contracts', 'search_trials', 'search_attempts', 'search_terminal_events'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.conn.execute(f'DELETE FROM {table}')
            db.conn.rollback()


def test_schema_is_additive_idempotent_and_backup_is_taken_before_upgrade(tmp_path):
    target = settings.DATA_DIR / 'private' / 'upgrade.db'
    with DatabaseManager(target) as db:
        assert apply_migrations(db.conn) == []
        migration = settings.BASE_DIR / 'database' / 'migrations' / '017_research_search_ledger.sql'
        _run_sql_migration(db.conn, migration)
        db.conn.commit()
        db.conn.execute("INSERT INTO search_contracts VALUES ('old','2026-01-01','{}')")
        db.conn.commit()
    with sqlite3.connect(target) as conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=17')
        conn.execute('DELETE FROM migration_checksums WHERE version=17')
    with DatabaseManager(target) as db:
        assert db.schema_version() == 17
        assert db.conn.execute('SELECT id FROM search_contracts').fetchone()[0] == 'old'
    assert list((target.parent / 'backups').glob('upgrade-*-v16.db'))


@pytest.mark.parametrize('defect', ['contract', 'case', 'trial', 'attempt', 'history', 'budget', 'duplicate'])
def test_portable_registration_rejects_contract_scope_or_denominator_forgery(defect):
    receipt = reserve()
    if defect == 'contract':
        receipt['contract_id'] = 'f' * 64
    elif defect == 'case':
        receipt['registrations'][0]['case'] = 'different'
    elif defect == 'trial':
        receipt['registrations'][0]['trial_id'] = 'f' * 64
    elif defect == 'attempt':
        receipt['registrations'][0]['attempt_id'] = 'bad'
    elif defect == 'history':
        receipt['earlier_history_complete'] = True
    elif defect == 'budget':
        receipt['attempt_budget'] = 0
    else:
        receipt['registrations'] *= 2
    with pytest.raises(ValueError):
        validate_registration(CONTRACT, POLICIES, receipt)


def test_ledger_cannot_be_the_market_database_or_escape_private_storage(tmp_path):
    for path in (settings.DB_PATH, tmp_path / 'public.db'):
        with pytest.raises(ValueError, match='private'):
            reserve(path=path)
    assert not settings.DB_PATH.exists()
