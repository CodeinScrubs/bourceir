"""مسیر کامل بازیابی با دیتابیس موقت و تأمین‌کننده آزمون؛ شبکه واقعی ندارد."""

import json

import pandas as pd
import pytest

from brs import build_parser
from config import settings
from database.migrator import _run_sql_migration, apply_migrations, discover_migrations
from ops.price_recovery import apply_recovery_run, collect_recovery, verified_recovery
from ops.report_files import write_private_report
from ops.research_preflight import research_preflight
from tests.test_price_recovery import stored  # noqa: F401 (fixture)
from tests.test_query_screener import bar, trades


@pytest.fixture
def source(db, request, tmp_path, monkeypatch):
    request.getfixturevalue('stored')
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    path = tmp_path / 'private' / 'preflight.json'
    write_private_report(path, research_preflight(db))
    return path


class Provider:
    name = 'tsetmc'
    def __init__(self, db, root):
        self.db, self.root = db, root

    def get_trade_session(self, ins_code, day):
        folder = next((self.root / 'private' / 'price-recovery').iterdir())
        plan = json.loads((folder / 'plan.json').read_text(encoding='utf-8'))
        assert plan['selected'][0]['date'] == day and plan['selected'][0]['ins_code'] == ins_code
        assert not self.db.conn.in_transaction
        return trades()


def test_register_fetch_verify_backup_apply_replay_and_keep_all_old_evidence(db, source, tmp_path):
    before = db.conn.total_changes
    result, folder = collect_recovery(db, from_preflight=source, provider=Provider(db, tmp_path))
    assert db.conn.total_changes == before
    assert result['statuses'] == {'verified_same_source_candidate': 1} and result['requests_made'] == 1
    assert not result['applied'] and not result['accepted_for_signals']
    assert verified_recovery(folder)[2][0][1]['after']['open_raw'] == 100
    note = 'تطبیق کامل شاهد مصنوعی در دیتابیس موقت آزمون'
    applied, path = apply_recovery_run(db, folder, review_note=note)
    assert path.exists() and applied['applied'] == 1 and applied['feature_rebuild_required']
    assert (db.db_path.parent / 'backups' / applied['backup_name']).exists()
    again, _ = apply_recovery_run(db, folder, review_note=note)
    assert again['applied'] == 0 and again['already_applied'] == 1 and again['backup_name'] is None
    assert len(list((db.db_path.parent / 'backups').glob('*.db'))) == 1
    assert (folder / 'result.json').read_text(encoding='utf-8') == json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)


def test_no_network_by_default_and_unknown_is_not_repaired(db, source):
    result, folder = collect_recovery(db, from_preflight=source)
    assert result['requests_made'] == 0 and result['statuses'] == {'unverified_trade_path': 1}
    applied, _ = apply_recovery_run(db, folder, review_note='بدون شاهد معتبر؛ هیچ تغییری در آزمون نمی‌دهیم')
    assert applied['applied'] == 0 and applied['backup_name'] is None


@pytest.mark.parametrize('target', ['plan.json', 'result.json', 'observation-000.json', 'code/core/price_recovery.py', 'trade'])
def test_tampered_evidence_prevents_any_database_repair(db, source, tmp_path, target):
    _, folder = collect_recovery(db, from_preflight=source, provider=Provider(db, tmp_path))
    path = next((folder / 'trades').glob('*.gz')) if target == 'trade' else folder / target
    path.write_bytes(b'corrupted test-only data')
    with pytest.raises((ValueError, OSError, KeyError, TypeError)):
        apply_recovery_run(db, folder, review_note='تلاش اعمال شاهد دستکاری‌شده فقط در آزمون')
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0
    assert db.get_daily_prices_df('آزمون').open_raw.isna().all()


def test_local_change_after_fetch_blocks_application_without_overwriting_it(db, source, tmp_path):
    _, folder = collect_recovery(db, from_preflight=source, provider=Provider(db, tmp_path))
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET open_raw=99')
    with pytest.raises(ValueError, match='تغییر'):
        apply_recovery_run(db, folder, review_note='نمونه تغییرکرده پس از دریافت؛ آزمون عدم بازنویسی')
    assert db.get_daily_prices_df('آزمون').open_raw.iloc[0] == 99


def test_change_of_current_code_rejects_old_apply_but_does_not_edit_the_run(db, source, tmp_path, monkeypatch):
    _, folder = collect_recovery(db, from_preflight=source, provider=Provider(db, tmp_path))
    monkeypatch.setattr('ops.price_recovery.code_identity', lambda *_: {'changed': 'test'})
    with pytest.raises(ValueError, match='کد'):
        verified_recovery(folder)


def test_evidence_change_after_backup_rolls_back_rows_and_receipts(db, source, tmp_path, monkeypatch):
    from database import price_recovery

    _, folder = collect_recovery(db, from_preflight=source, provider=Provider(db, tmp_path))
    original = price_recovery.apply_recoveries
    def mutate_before_commit(*args, **kwargs):
        callback = kwargs['before_commit']
        def changed():
            (folder / 'plan.json').write_text('{}', encoding='utf-8')
            return callback()
        return original(*args, **(kwargs | dict(before_commit=changed)))
    monkeypatch.setattr('ops.price_recovery.apply_recoveries', mutate_before_commit)
    with pytest.raises(ValueError):
        apply_recovery_run(db, folder, review_note='آزمون تغییر فایل پس از پشتیبان و پیش از commit')
    assert db.get_daily_prices_df('آزمون').open_raw.isna().all()
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0


def test_recovery_cli_and_migration_are_explicit_and_repeatable(db):
    args = build_parser().parse_args(['recover-prices', '--from-preflight', 'test.json'])
    assert not args.fetch and args.apply_run is None and args.max_rows == 5
    assert apply_migrations(db.conn) == []
    path = next(path for number, _, path in discover_migrations() if number == 15)
    _run_sql_migration(db.conn, path)
    db.conn.commit()
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0


@pytest.fixture
def multi_source(db, source):
    sid = db.conn.execute('SELECT id FROM symbols').fetchone()[0]
    db.upsert_daily_prices_df(sid, pd.DataFrame([bar(date=day, open_raw=None)
        for day in ('2024-02-05', '2024-02-06', '2010-02-03', '2025-03-24')]))
    write_private_report(source, research_preflight(db))
    return source


class PagedProvider:
    name = 'tsetmc'
    def __init__(self, db, root):
        self.db, self.root, self.calls = db, root, []

    def get_trade_session(self, ins_code, day):
        assert not self.db.conn.in_transaction
        plans = [json.loads(path.read_bytes()) for path in (self.root / 'private/price-recovery').glob('*/plan.json')]
        assert any(plan['network_requested'] and any(row['date'] == day and row['ins_code'] == ins_code
                                                   for row in plan['selected']) for plan in plans)
        self.calls.append((ins_code, day))
        result = trades(requested_date=day)
        for row in result['response']['tradeHistory']:
            row['dEven'] = int(day.replace('-', ''))
        return result


def test_next_page_keeps_fixed_frame_after_first_page_is_applied(db, multi_source, tmp_path):
    provider = PagedProvider(db, tmp_path)
    first, first_folder = collect_recovery(db, from_preflight=multi_source, provider=provider, max_rows=1)
    assert first['eligible_rows_in_frame'] == 3 and first['next_offset'] == 1 and not first['frame_exhausted']
    apply_recovery_run(db, first_folder, review_note='اعمال اولین دسته فقط در دیتابیس موقت آزمون')
    second, second_folder = collect_recovery(db, from_preflight=multi_source, provider=provider, max_rows=1, offset=1)
    assert provider.calls == [('123', '2024-02-06'), ('123', '2024-02-05')]
    assert second['next_offset'] == 2 and not second['current_global_quality_verified']
    assert verified_recovery(second_folder)[2][0][0]['date'] == '2024-02-05'
    with pytest.raises(ValueError, match='تغییر'):
        collect_recovery(db, from_preflight=multi_source, provider=provider, max_rows=1)
    assert len(provider.calls) == 2


def test_warmup_is_explicit_and_never_includes_the_sealed_period(db, multi_source, tmp_path):
    provider = PagedProvider(db, tmp_path)
    result, folder = collect_recovery(db, from_preflight=multi_source, provider=provider, max_rows=50,
                                      offset=3, include_warmup=True)
    assert result['eligible_rows_in_frame'] == 4 and result['selected_phases'] == {'prior_history': 1}
    assert result['next_offset'] == 4 and result['frame_exhausted']
    assert provider.calls == [('123', '2010-02-03')]
    assert verified_recovery(folder)[2][0][0]['phase'] == 'prior_history'
    with pytest.raises(ValueError, match='بیشتر'):
        collect_recovery(db, from_preflight=multi_source, offset=4)


def test_exhausting_truncated_frame_does_not_claim_a_complete_census(db, multi_source):
    write_private_report(multi_source, research_preflight(db, detail_limit=1))
    result, folder = collect_recovery(db, from_preflight=multi_source, include_warmup=True)
    assert result['frame_exhausted'] and not result['sampling_frame_complete']
    assert result['frame_issue_rows'] == result['eligible_rows_in_frame'] == 1
    plan = json.loads((folder / 'plan.json').read_bytes())
    assert not plan['current_global_quality_verified'] and not result['accepted_for_signals']


def test_exact_frame_end_is_an_explicit_empty_page_without_requests(db, multi_source, tmp_path):
    provider = PagedProvider(db, tmp_path)
    result, folder = collect_recovery(db, from_preflight=multi_source, provider=provider, offset=3)
    assert result['frame_exhausted'] and result['sampling_frame_complete']
    assert result['selected_rows'] == result['requests_made'] == 0 and not provider.calls
    assert verified_recovery(folder)[2] == []


@pytest.mark.parametrize('offset', [-1, True, 1.5, 10001])
def test_invalid_page_options_do_not_request_or_register_a_run(db, source, tmp_path, offset):
    provider = PagedProvider(db, tmp_path)
    with pytest.raises(ValueError):
        collect_recovery(db, from_preflight=source, provider=provider, offset=offset)
    assert not provider.calls and not (tmp_path / 'private/price-recovery').exists()


def test_reconstructed_selection_rejects_a_changed_offset_even_with_an_updated_plan_hash(db, multi_source, tmp_path):
    from ops.price_recovery import sha

    _, folder = collect_recovery(db, from_preflight=multi_source, provider=PagedProvider(db, tmp_path), max_rows=1)
    plan = json.loads((folder / 'plan.json').read_bytes())
    plan['selection_offset'] = 1
    write_private_report(folder / 'plan.json', plan)
    result = json.loads((folder / 'result.json').read_bytes())
    result['artifacts_sha256']['plan.json'] = sha(folder / 'plan.json')
    write_private_report(folder / 'result.json', result)
    with pytest.raises(ValueError, match='چارچوب'):
        verified_recovery(folder)


def test_page_cli_forwards_options(monkeypatch):
    from ops import price_recovery

    argv = []
    monkeypatch.setattr(price_recovery, 'main', lambda args: argv.extend(args) or 0)
    args = build_parser().parse_args(['recover-prices', '--from-preflight', 'test.json', '--offset', '50', '--include-warmup'])
    assert args.func(args) == 0
    assert argv == ['--from-preflight', 'test.json', '--max-rows', '5', '--offset', '50', '--include-warmup']


@pytest.mark.parametrize('options', [['--offset', '1'], ['--include-warmup'], ['--fetch']])
def test_apply_rejects_new_selection_or_network_before_reading_a_run(monkeypatch, options):
    from ops import price_recovery

    monkeypatch.setattr(price_recovery, 'verified_recovery', lambda *_: pytest.fail('نباید برنامه خوانده شود'))
    with pytest.raises(SystemExit) as raised:
        price_recovery.main(['--apply-run', 'not-a-real-folder', *options])
    assert raised.value.code == 2
