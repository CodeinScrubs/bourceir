"""دفتر آینده فقط روی دیتابیس موقت؛ آزمون جعل زمان، سانسور نمونه و دست‌کاری شواهد."""

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import settings
from core.forward_observations import quote_evidence, validate_archive
from core.timeutils import TEHRAN_TZ
from data_collector.scheduler import DataCollector
from database.capture import CaptureRepository
from database.db import DatabaseManager
from database.forward_observations import ForwardRepository
from tests.test_dashboard import DASHBOARD
from tests.test_dashboard import dashboard_db as dashboard_db


@pytest.fixture
def future_clock(monkeypatch):
    clock = SimpleNamespace(at=datetime(2026, 10, 17, 8, 45, tzinfo=TEHRAN_TZ))
    monkeypatch.setattr('database.forward_observations.now_tehran_iso', lambda: clock.at.isoformat())
    monkeypatch.setattr('dashboard.forward_observations.now_tehran', lambda: clock.at)
    return clock


def schedule():
    return {key: '2026-10-17T' + time + ':00+03:30' for key, time in zip(
        ('assessment_start', 'assessment_end', 'followup_start', 'followup_end'), ('09:05', '09:10', '12:00', '12:05'))}


def register(db, **kwargs):
    db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='test', instrument_type_code=300))
    db.upsert_symbol(dict(symbol_fa='دوم', ins_code='test2', instrument_type_code=303))
    args = dict(symbols=['آزمون', 'دوم'], rationale='معیار ثابت آزمایشی؛ فقط در دیتابیس موقت', schedule=schedule())
    args.update(kwargs)
    return ForwardRepository(db).register(**args)


def snapshot(db, at, price=1000, *, receipt=True, **kwargs):
    values = dict(captured_at=at.isoformat(), provider_timestamp=at.isoformat(), last_price=price,
                  low_price=900, high_price=1300, ref_price=980, volume=10000, value=10000000)
    values.update(kwargs)
    if not receipt:
        db.insert_snapshot(db.get_symbol('آزمون')['id'], values)
        return
    # این تأمین‌کننده فقط در آزمون و دیتابیس موقت است؛ مسیر واقعی ذخیره و رسید اجرا می‌شود.
    scope = db.get_universe_symbols()
    source = DataCollector(db=db, provider=SimpleNamespace(get_market_snapshots=lambda: [dict(values, ins_code='test')]))
    with patch('database.capture.now_tehran_iso', return_value=at.isoformat()):
        repo = CaptureRepository(db)
        run = repo.start(scope, interval=30, cycles=1, forced=False, provider='tsetmc', explicit_scope=True)
        repo.append(run, 1, 'poll_started')
        report = source.collect_market_snapshot(instruments=scope)
        repo.append(run, 1, 'poll_finished', dict(status=report['status'], coverage=report['coverage']))


def assess(db, key):
    ForwardRepository(db).record(key, 'assessment', assessments={'آزمون': dict(choice='watch', rationale='شرط از پیش تعیین‌شده')})


def test_fixed_scope_missing_data_and_skips_survive_without_creating_fills(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = datetime(2026, 10, 17, 9, 6, tzinfo=TEHRAN_TZ)
    snapshot(db, future_clock.at - timedelta(seconds=1))
    assess(db, key)
    future_clock.at = future_clock.at.replace(hour=12, minute=1)
    snapshot(db, future_clock.at - timedelta(seconds=1), price=1100)
    repo.record(key, 'followup')
    report = repo.report(key)
    assert report['scope_count'] == 2 and report['known_quote_pairs'] == report['unknown_quote_pairs'] == 1
    assert report['choice_counts'] == {'watch': 1, 'not_reviewed': 1}
    assert report['rows'][0]['raw_quote_change'] == pytest.approx(.1)
    assert report['rows'][1]['raw_quote_change'] is None
    assert report['portfolio_return'] is None and not report['accepted_for_signals']
    assert not report['externally_timestamped'] and not report['independent_trial_verified']
    for table in ('journal_executions', 'trade_plans'):
        assert db.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    saved = json.loads(repo.export(key))
    validate_archive(saved['trial'], saved['events'])
    assert saved['report'] == report


@pytest.mark.parametrize('field,value', [
    ('assessment_start', '2026-10-17T08:44:00+03:30'),
    ('assessment_end', '2026-10-17T09:11:00+03:30'),
    ('followup_start', '2026-10-17T09:09:00+03:30'),
    ('followup_end', '2026-10-18T12:05:00+03:30'),
    ('assessment_start', '2026-10-17T09:05:00'),
    ('followup_end', '2026-10-17T12:30:00+03:30'),
])
def test_registration_rejects_backdating_ambiguous_clock_and_bad_windows(db, future_clock, field, value):
    bad = schedule() | {field: value}
    with pytest.raises(ValueError):
        register(db, schedule=bad)
    assert not ForwardRepository(db).list_trials()


def test_registration_rejects_duplicate_arabic_persian_names_and_non_equity(db, future_clock):
    db.upsert_symbol(dict(symbol_fa='کیا', ins_code='x', instrument_type_code=300))
    with pytest.raises(ValueError, match='تکراری'):
        register(db, symbols=['کیا', 'كيا'])
    db.upsert_symbol(dict(symbol_fa='صندوق', ins_code='fund', instrument_type_code=305))
    with pytest.raises(ValueError, match='سهام عادی'):
        register(db, symbols=['صندوق'])


def test_calendar_and_code_preserved_but_new_code_cannot_continue_trial(db, future_clock, monkeypatch):
    key, repo = register(db), ForwardRepository(db)
    monkeypatch.setattr(settings, 'TRADING_DAYS', [])
    monkeypatch.setattr(settings, 'TRADING_HOURS', {'start': '10:00', 'end': '11:00'})
    assert repo.report(key)['phases']['assessment'] == 'pending'
    monkeypatch.setattr('database.forward_observations.code_identity', lambda: {'sources': {}, 'sha256': 'changed'})
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    with pytest.raises(ValueError, match='کد'):
        assess(db, key)
    assert db.conn.execute('SELECT COUNT(*) FROM forward_events').fetchone()[0] == 0


def test_no_late_backfill_no_overwrite_and_missed_trial_visible(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    with pytest.raises(ValueError, match='پنجره'):
        assess(db, key)
    future_clock.at = future_clock.at.replace(hour=9, minute=10)
    with pytest.raises(ValueError, match='پنجره'):
        assess(db, key)
    future_clock.at = future_clock.at.replace(hour=12, minute=1)
    repo.record(key, 'followup')
    with pytest.raises(ValueError):
        repo.record(key, 'followup')
    report = repo.report(key)
    assert report['phases'] == {'assessment': 'missed', 'followup': 'recorded'}
    assert report['unknown_quote_pairs'] == 2 and report['known_quote_pairs'] == 0
    assert len(repo.list_trials()) == 1
    future_clock.at = future_clock.at.replace(hour=8)
    with pytest.raises(ValueError, match='ساعت'):
        repo.report(key)


@pytest.mark.parametrize('choice,reason', [('buy', 'x'), ('watch', ''), ('skip', ''), ('watch', None)])
def test_bad_assessment_is_atomic(db, future_clock, choice, reason):
    key = register(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    with pytest.raises(ValueError):
        ForwardRepository(db).record(key, 'assessment', assessments={'آزمون': dict(choice=choice, rationale=reason)})
    assert db.conn.execute('SELECT COUNT(*) FROM forward_events').fetchone()[0] == 0


def test_declined_symbol_is_preserved_and_database_revisions_do_not_rewrite_evidence(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at)
    repo.record(key, 'assessment', assessments={'آزمون': dict(choice='skip', rationale='شرط برقرار نیست')})
    snapshot(db, future_clock.at, price=1200)
    assert repo.report(key)['rows'][0]['assessment']['last_price_rial'] == 1000
    assert repo.report(key)['choice_counts']['skip'] == 1
    with pytest.raises(ValueError):
        assess(db, key)
    for table, field in [('forward_trials', 'registered_at'), ('forward_events', 'recorded_at')]:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            with db.conn:
                db.conn.execute(f"UPDATE {table} SET {field}='x'")
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            with db.conn:
                db.conn.execute(f'DELETE FROM {table}')
    exported = json.loads(repo.export(key))
    exported['events'][0]['recorded_at'] = '2026-10-17T09:07:00+03:30'
    with pytest.raises(ValueError, match='زنجیره'):
        validate_archive(exported['trial'], exported['events'])


@pytest.mark.parametrize('field,value,problem', [
    ('captured_at', '2026-10-17T09:00:00+03:30', 'captured_at_stale'),
    ('provider_timestamp', '2026-10-17T10:00:00+03:30', 'provider_timestamp_future_or_other_day'),
    ('provider_timestamp', None, 'provider_timestamp_unknown'),
    ('price_unit', 'TOMAN', 'source_or_unit_invalid'),
    ('last_price', True, 'last_price_invalid'),
    ('last_price', float('inf'), 'last_price_invalid'),
    ('last_price', 2000, 'price_outside_range'),
    ('volume', 0, 'volume_invalid'),
])
def test_bad_quotes_stay_unknown(field, value, problem):
    at = '2026-10-17T09:06:00+03:30'
    quote = dict(captured_at=at, provider_timestamp=at, source='tsetmc', price_unit='Rial',
                 last_price=1000, low_price=900, high_price=1100, ref_price=980, volume=100, value=100000)
    quote[field] = value
    evidence = quote_evidence(quote, at)
    assert problem in evidence['problems'] and evidence['last_price_rial'] is None


def test_no_observation_can_be_an_outcome_for_itself(db, future_clock):
    times = schedule() | {'followup_start': '2026-10-17T09:10:01+03:30', 'followup_end': '2026-10-17T09:11:00+03:30'}
    key, repo = register(db, schedule=times), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=9, second=59)
    snapshot(db, future_clock.at)
    assess(db, key)
    future_clock.at += timedelta(seconds=3)
    repo.record(key, 'followup')
    assert repo.report(key)['rows'][0]['problems'] == ['no_new_quote_after_assessment']


def test_reference_change_does_not_create_jump_return(db, future_clock):
    key, repo = register(db), ForwardRepository(db)
    future_clock.at = future_clock.at.replace(hour=9, minute=6)
    snapshot(db, future_clock.at)
    assess(db, key)
    future_clock.at = future_clock.at.replace(hour=12, minute=1)
    snapshot(db, future_clock.at, ref_price=490)
    repo.record(key, 'followup')
    assert repo.report(key)['rows'][0]['problems'] == ['reference_price_changed']


def test_migration_12_idempotent_and_upgrade_backs_up_existing_data(tmp_path, monkeypatch):
    from database import migrator

    path = tmp_path / 'old.db'
    migration_dir = tmp_path / 'migrations'
    migration_dir.mkdir()
    for migration in migrator.MIGRATIONS_DIR.iterdir():
        if migration.name[:3].isdigit() and int(migration.name[:3]) <= 11:
            (migration_dir / migration.name).write_bytes(migration.read_bytes())
    with monkeypatch.context() as patch:
        patch.setattr(migrator, 'MIGRATIONS_DIR', migration_dir)
        with DatabaseManager(path) as old:
            assert old.schema_version() == 11
            old.upsert_symbol(dict(symbol_fa='آزمون', ins_code='test', instrument_type_code=300))
    with DatabaseManager(path) as upgraded:
        assert upgraded.schema_version() == max(m[0] for m in migrator.discover_migrations()) and upgraded.get_symbol('آزمون')
        migration = settings.BASE_DIR / 'database/migrations/012_forward_observations.sql'
        upgraded.conn.executescript(migration.read_text(encoding='utf-8'))
        assert upgraded.init_db() == []
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert backups
    with DatabaseManager(backups[0], read_only=True) as old:
        assert old.schema_version() == 11 and old.get_symbol('آزمون')


def test_forward_dashboard_registration_and_missed_windows(dashboard_db, future_clock):
    from streamlit.testing.v1 import AppTest

    with DatabaseManager(dashboard_db) as db:
        key = register(db)
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🗓️ دفتر مشاهده آینده').run()
    assert not at.exception
    assert at.dataframe[0].value['بررسی'].tolist() == ['هنوز نرسیده']
    future_clock.at = future_clock.at.replace(hour=12, minute=10)
    at.run()
    assert not at.exception
    assert at.dataframe[0].value['بررسی'].tolist() == ['مهلت از دست رفت']
    with DatabaseManager(dashboard_db, read_only=True) as db:
        assert ForwardRepository(db).report(key)['unknown_quote_pairs'] == 2


def test_dashboard_can_register_and_record_both_phases_without_market_fabrication(dashboard_db, future_clock):
    from streamlit.testing.v1 import AppTest

    with DatabaseManager(dashboard_db) as db:
        db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='test', instrument_type_code=300))
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🗓️ دفتر مشاهده آینده').run()
    app.multiselect[0].set_value(['آزمون'])
    app.text_area[0].set_value('پروتکل آزمون فقط در دیتابیس موقت')
    next(b for b in app.button if b.label == 'ثبت دامنه و زمان ثابت').click().run()
    assert not app.exception and not app.error
    with DatabaseManager(dashboard_db, read_only=True) as db:
        repo = ForwardRepository(db)
        trials = repo.list_trials()
        assert len(trials) == 1
        key = trials[0]['id']
        fixed = json.loads(trials[0]['protocol_json'])['schedule']
    future_clock.at = datetime.fromisoformat(fixed['assessment_start']) + timedelta(seconds=1)
    app.run()
    next(b for b in app.button if b.label == 'ثبت یک‌باره بررسی همه دامنه').click().run()
    assert not app.exception and not app.error
    future_clock.at = datetime.fromisoformat(fixed['followup_start']) + timedelta(seconds=1)
    app.run()
    next(b for b in app.button if b.label == 'ثبت یک‌باره نقل قیمت پیگیری برای همه دامنه').click().run()
    assert not app.exception and not app.error
    with DatabaseManager(dashboard_db, read_only=True) as db:
        report = ForwardRepository(db).report(key)
        assert report['phases'] == {'assessment': 'recorded', 'followup': 'recorded'}
        assert report['choice_counts'] == {'not_reviewed': 1} and report['unknown_quote_pairs'] == 1
        assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0
