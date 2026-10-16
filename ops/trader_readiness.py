"""گزارش آفلاین نصب و کیفیت بازار؛ فقط‌خواندنی و بدون اطلاعات حساب یا پذیرش روش."""

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from database.migrator import discover_migrations
from ops.environment import inspect_environment
from ops.report_files import private_report_path, write_private_report
from ops.research_preflight import research_preflight

VERSION = 'trader-readiness-v1'
CODE_FILES = ('brs.py', 'ops/trader_readiness.py', 'ops/research_preflight.py', 'ops/environment.py', 'ops/report_files.py',
              'core/price_quality.py', 'core/client_quality.py', 'features/provenance.py', 'database/db.py',
              'database/migrator.py',
              'config/settings.py', 'config/default.yaml', 'config/production.yaml', 'requirements.lock.txt')
NOT_ASSESSED = ['historical_membership', 'economic_corporate_entitlements', 'historical_fee_schedule',
                'live_source_access', 'full_intraday_session_coverage', 'execution_calibration',
                'independent_future_performance', 'private_owner_profile']


def code_identity():
    return {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            for name in CODE_FILES}


def collect_readiness(db):
    """فقط جداول بازار در یک تصویر سازگار؛ محتوای حساب و پروفایل شخصی خوانده نمی‌شوند."""
    sources = code_identity()
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        preflight = research_preflight(db, detail_limit=0)
        integrity = 'ok' if db.integrity_check() == 'ok' else 'failed'
        foreign_keys = len(db.conn.execute('PRAGMA foreign_key_check').fetchall())
        ids = [s['id'] for s in db.get_universe_symbols(include_inactive=True) if s['instrument_type_code'] in (300, 303)]
        snapshots = dict(rows=0, symbols=0, missing_provider_timestamp_rows=0)
        if ids:
            placeholders = ','.join('?' for _ in ids)
            row = db.conn.execute(f'''SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol_id) AS symbols,
                COALESCE(SUM(provider_timestamp IS NULL OR trim(provider_timestamp)=''),0) AS missing_provider_timestamp_rows
                FROM snapshots WHERE source='tsetmc' AND symbol_id IN ({placeholders})''', ids).fetchone()
            snapshots = dict(row)
    finally:
        if own_snapshot:
            db.conn.rollback()
    environment = inspect_environment(settings.BASE_DIR / 'requirements.lock.txt', Path(sys.prefix) / '.brs-lock.sha256')
    schema_current = [(row['version'], row['name']) for row in db.conn.execute(
        'SELECT version,name FROM schema_migrations ORDER BY version')] == [(number, name) for number, name, _ in discover_migrations()]
    engineering_passed = bool(environment['ready'] and preflight['input_contract_passed']
                              and integrity == 'ok' and foreign_keys == 0 and schema_current)
    blockers = list(preflight['blockers'])
    if not environment['ready']:
        blockers.append('runtime_environment_not_ready')
    if integrity != 'ok' or foreign_keys:
        blockers.append('database_integrity_failed')
    if not schema_current:
        blockers.append('database_schema_not_current')
    # گزارش کوتاه بازار؛ سطر قیمت و محتوای حساب برای این فرمان لازم نیست.
    keys = ('version', 'end_exclusive', 'database_schema', 'development_start', 'price_scope', 'universe_count', 'price_rows',
            'coherent_client_pairs', 'unknown_client_pairs', 'input_contract_passed', 'client_value_pairs_complete',
            'price_issue_counts', 'feature_logic_sha256', 'diagnostic_code_sha256', 'blockers')
    development = {key: preflight[key] for key in keys if key in preflight}
    for key in ('invalid_price_rows', 'development_price_rows', 'prior_price_rows',
                'invalid_development_price_rows', 'invalid_prior_price_rows'):
        development[key] = sum(row[key] for row in preflight['records'])
    development['raw_input_snapshot_sha256'] = hashlib.sha256(
        json.dumps(preflight['raw_inputs'], sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=settings.BASE_DIR,
                                           text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        changes = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal', '--', *CODE_FILES],
                                          cwd=settings.BASE_DIR, text=True, stderr=subprocess.DEVNULL, timeout=5)
        code_tree_clean = not changes.strip()
    except (OSError, subprocess.SubprocessError):
        revision, code_tree_clean = None, None
    if sources != code_identity():
        raise ValueError('کد در میانه بررسی تغییر کرده است؛ گزارش تازه بسازید')
    return dict(version=VERSION, generated_at=now_tehran_iso(), project_version=settings.VERSION,
        git_revision=revision, git_code_tree_clean=code_tree_clean, code=sources,
        status='research_only' if engineering_passed else 'blocked',
        engineering_and_input_contract_passed=engineering_passed,
        environment=environment, database_integrity=integrity, foreign_key_violations=foreign_keys, schema_current=schema_current,
        development=development, public_market_snapshot_counts=snapshots, blockers=blockers, not_assessed=NOT_ASSESSED,
        owner_inputs_needed=[], private_owner_profile_read=False, accepted_for_signals=False, profitability_proven=False,
        network_checked=False, full_session_coverage_verified=False, execution_proven=False,
        privacy_scope='public-market metadata, aggregate quality and code identity; no account content or absolute paths',
        limitations=['عبور قرارداد ورودی فقط اجازه ادامه بررسی مهندسی است؛ اعتبار روش معاملاتی نیست.',
                     'شمار مظنه‌ها، تازگی، پوشش کامل جلسه یا پرشدن سفارش را ثابت نمی‌کند.',
                     'پاسخ‌های قبلی مالک در پروفایل خصوصی‌اند و این گزارش آن‌ها را ارزیابی نمی‌کند.',
                     'تعداد نقض رابطه گزارش می‌شود؛ نام جدول و شناسه ردیف خصوصی صادر نمی‌شوند.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', default=str(settings.DATA_DIR / 'private' / 'trader-readiness.json'))
    args = parser.parse_args(argv)
    try:
        target = private_report_path(args.json, forbidden_paths=[settings.DB_PATH])
        try:
            with DatabaseManager(read_only=True) as db:
                report = collect_readiness(db)
        except (ValueError, OSError, sqlite3.Error) as exc:
            report = dict(version=VERSION, generated_at=now_tehran_iso(), project_version=settings.VERSION,
                status='blocked', engineering_and_input_contract_passed=False, error_type=type(exc).__name__,
                blockers=['database_or_input_contract_unreadable'], not_assessed=NOT_ASSESSED,
                owner_inputs_needed=[], private_owner_profile_read=False,
                accepted_for_signals=False, profitability_proven=False, network_checked=False)
        write_private_report(target, report, forbidden_paths=[settings.DB_PATH])
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps({key: report[key] for key in ('status', 'engineering_and_input_contract_passed',
                                                   'accepted_for_signals', 'profitability_proven')}))
    return 0 if report['engineering_and_input_contract_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
