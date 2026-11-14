"""Append-only local trial accounting and atomic duplicate/compute admission.

Only integrated runners are covered. This is neither a cache nor a complete
historical search count, an external timestamp, or statistical acceptance.
"""

import argparse
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager

VERSION = 'research-search-ledger-v1'
DEFAULT_ATTEMPT_BUDGET = 100000


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def identity(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def ledger_path():
    return settings.DATA_DIR / 'private' / 'research-search' / 'ledger.db'


def _path(path):
    target = Path(path or ledger_path()).resolve()
    if (not target.is_relative_to((settings.DATA_DIR / 'private').resolve())
            or target == settings.DB_PATH.resolve()
            or target.exists() and settings.DB_PATH.exists() and target.samefile(settings.DB_PATH)):
        raise ValueError('Search ledger must be private and separate from the market/account database')
    return target


@contextmanager
def _connection(path):
    with DatabaseManager(_path(path)) as db:
        # COMMIT means durable ledger registration before any evaluation starts.
        db.conn.execute('PRAGMA synchronous=FULL')
        yield db.conn


def reserve_trials(contract, policies, *, run_id, allow_repeat=False, attempt_budget=DEFAULT_ATTEMPT_BUDGET, path=None):
    """All policies are admitted or none are. Pending duplicates always block."""
    if (not isinstance(contract, dict) or not contract or type(allow_repeat) is not bool
            or type(attempt_budget) is not int or attempt_budget < 1
            or not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256
            or not isinstance(policies, list) or not policies):
        raise ValueError('Explicit contract, policies, run identity and positive attempt budget are required')
    contract_json, contract_id = canonical(contract), identity(contract)
    prepared = []
    for item in policies:
        if (not isinstance(item, dict) or set(item) != {'name', 'policy'} or not isinstance(item['name'], str)
                or not item['name'] or len(item['name']) > 512 or not isinstance(item['policy'], dict) or not item['policy']):
            raise ValueError('A named nonempty explicit policy is required')
        policy_json = canonical(item['policy'])
        trial_id = identity(dict(contract_id=contract_id, policy=item['policy']))
        prepared.append(dict(case=item['name'], trial_id=trial_id, policy_json=policy_json))
    if len({p['trial_id'] for p in prepared}) != len(prepared) or len({p['case'] for p in prepared}) != len(prepared):
        raise ValueError('Duplicate policy aliases within one batch are not separate trials')
    now = now_tehran_iso()
    registrations = []
    with _connection(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            total_before = conn.execute('SELECT COUNT(*) FROM search_attempts').fetchone()[0]
            if total_before + len(prepared) > attempt_budget:
                raise ValueError('Local search attempt budget exceeded before evaluation')
            old_contract = conn.execute('SELECT spec_json FROM search_contracts WHERE id=?', (contract_id,)).fetchone()
            if old_contract and old_contract[0] != contract_json:
                raise ValueError('Search contract hash collision or corruption')
            for row in prepared:
                previous = conn.execute('SELECT policy_json FROM search_trials WHERE id=?', (row['trial_id'],)).fetchone()
                attempts = conn.execute('''SELECT a.id,e.status FROM search_attempts a LEFT JOIN search_terminal_events e
                    ON e.attempt_id=a.id WHERE a.trial_id=?''', (row['trial_id'],)).fetchall()
                if previous and previous[0] != row['policy_json']:
                    raise ValueError('Search policy hash collision or corruption')
                if any(a['status'] is None for a in attempts):
                    raise ValueError('Identical policy already has an unfinished attempt; inspect it before retrying')
                if attempts and not allow_repeat:
                    raise ValueError('Identical policy already evaluated; explicit repeat authorization is required')
                registrations.append(dict(case=row['case'], trial_id=row['trial_id'],
                    attempt_id=uuid.uuid4().hex, previous_attempts=len(attempts)))
            conn.execute('INSERT OR IGNORE INTO search_contracts VALUES (?,?,?)', (contract_id, now, contract_json))
            for row, registration in zip(prepared, registrations):
                conn.execute('INSERT OR IGNORE INTO search_trials VALUES (?,?,?,?,?)',
                    (row['trial_id'], contract_id, row['case'], now, row['policy_json']))
                conn.execute('INSERT INTO search_attempts VALUES (?,?,?,?,?)',
                    (registration['attempt_id'], row['trial_id'], run_id, now, int(allow_repeat)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return dict(version=VERSION, run_id=run_id, contract_id=contract_id, registered_at=now,
        registrations=registrations, attempts_before_registration=total_before, attempt_budget=attempt_budget,
        allow_repeat=allow_repeat, earlier_history_complete=False, independent_timestamp=False,
        statistical_trial_count_verified=False, accepted_for_signals=False)


def finish_trials(receipt, *, status, evidence, path=None):
    """Terminal evidence is immutable; interruption/failed runs are retained."""
    if status not in ('complete', 'failed', 'interrupted') or not isinstance(evidence, dict) or not evidence:
        raise ValueError('An explicit terminal status and evidence are required')
    if (not isinstance(receipt, dict) or receipt.get('version') != VERSION
            or not receipt.get('registrations') or not isinstance(receipt.get('run_id'), str)):
        raise ValueError('A registered search receipt is required')
    body, digest, now = canonical(evidence), identity(evidence), now_tehran_iso()
    ids = [r['attempt_id'] for r in receipt['registrations']]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate attempt identities in completion receipt')
    with _connection(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            for registration in receipt['registrations']:
                row = conn.execute('''SELECT a.run_id,a.trial_id,t.contract_id,e.status,e.evidence_json,e.evidence_sha256
                    FROM search_attempts a JOIN search_trials t ON t.id=a.trial_id
                    LEFT JOIN search_terminal_events e ON e.attempt_id=a.id WHERE a.id=?''',
                    (registration['attempt_id'],)).fetchone()
                if (row is None or row['run_id'] != receipt['run_id'] or row['trial_id'] != registration['trial_id']
                        or row['contract_id'] != receipt['contract_id']):
                    raise ValueError('Completion receipt does not match the registered attempts')
                if row['status'] is not None:
                    if row['status'] != status or row['evidence_json'] != body or row['evidence_sha256'] != digest:
                        raise ValueError('A terminal search result cannot be overwritten')
                    continue
                conn.execute('INSERT INTO search_terminal_events VALUES (?,?,?,?,?)',
                    (registration['attempt_id'], now, status, body, digest))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def validate_registration(contract, policies, receipt):
    """Validate a portable receipt without pretending that its local DB is external evidence."""
    if (not isinstance(receipt, dict) or receipt.get('version') != VERSION
            or receipt.get('contract_id') != identity(contract)
            or type(receipt.get('attempt_budget')) is not int or receipt['attempt_budget'] < 1
            or type(receipt.get('attempts_before_registration')) is not int or receipt['attempts_before_registration'] < 0
            or type(receipt.get('allow_repeat')) is not bool
            or receipt.get('earlier_history_complete') is not False
            or receipt.get('statistical_trial_count_verified') is not False
            or receipt.get('independent_timestamp') is not False or receipt.get('accepted_for_signals') is not False):
        raise ValueError('Search registration contract or scope is inconsistent')
    registrations = receipt.get('registrations')
    if (not isinstance(registrations, list) or len(registrations) != len(policies)
            or receipt['attempts_before_registration'] + len(registrations) > receipt['attempt_budget']):
        raise ValueError('Search registration denominator or budget is inconsistent')
    ids = []
    for registration, policy in zip(registrations, policies):
        if (not isinstance(registration, dict) or registration.get('case') != policy['name']
                or registration.get('trial_id') != identity(dict(contract_id=receipt['contract_id'], policy=policy['policy']))
                or type(registration.get('previous_attempts')) is not int or registration['previous_attempts'] < 0
                or registration['previous_attempts'] > receipt['attempts_before_registration']
                or registration['previous_attempts'] > 0 and not receipt['allow_repeat']):
            raise ValueError('Search policy registration differs from the declared batch')
        attempt = registration.get('attempt_id')
        if not isinstance(attempt, str) or len(attempt) != 32 or any(c not in '0123456789abcdef' for c in attempt):
            raise ValueError('Search attempt identity is malformed')
        ids.append(attempt)
    if len(set(ids)) != len(ids):
        raise ValueError('Search registration repeats an attempt identity')
    return True


def summarize_ledger(path=None):
    """Read-only inventory. No historical outcomes are opened or ranked."""
    target = _path(path)
    if not target.exists():
        return dict(version=VERSION, status='not_created', registered_attempts=0, unique_policy_contracts=0,
                    coverage='integrated runners only', earlier_history_complete=False, accepted_for_signals=False)
    with DatabaseManager(target, read_only=True) as db:
        conn = db.conn
        status = {row['status']: row['n'] for row in conn.execute('''SELECT COALESCE(e.status,'unfinished') status,
            COUNT(*) n FROM search_attempts a LEFT JOIN search_terminal_events e ON e.attempt_id=a.id GROUP BY status''')}
        unique = conn.execute('SELECT COUNT(*) FROM search_trials').fetchone()[0]
        attempts = sum(status.values())
        return dict(version=VERSION, status='local_inventory', registered_attempts=attempts,
            unique_policy_contracts=unique, repeat_attempts=attempts-unique, status_counts=status,
            coverage='integrated runners only; strategy suite, native-execution follow-up and bounded CPU learning',
            earlier_history_complete=False, independent_timestamp=False,
            statistical_trial_count_verified=False, accepted_for_signals=False,
            limitations=['local append-only tables do not prevent database replacement or deletion',
                'code/data changes create new contracts; semantic duplicates across contracts are not detected',
                'unfinished includes active processes and hard crashes; no automatic retry or result reuse'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', action='store_true', required=True)
    parser.parse_args(argv)
    try:
        print(json.dumps(summarize_ledger(), ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
