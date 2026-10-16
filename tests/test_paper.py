"""دفتر مجازی، ظرفیت مشترک و مرز شواهد؛ داده ساختگی فقط روی دیتابیس موقت آزمون."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from config import settings
from core.journal import summarize_executions
from core.risk import RiskProfile
from core.timeutils import TEHRAN_TZ
from database.paper import PaperRepository
from tests.test_dashboard import dashboard_db as dashboard_db


@pytest.fixture
def paper(db, monkeypatch):
    clock = [datetime(2026, 10, 13, 10, tzinfo=TEHRAN_TZ)]
    monkeypatch.setattr('database.paper.now_tehran', lambda: clock[0])
    for symbol, code, kind in [('فولاد', '123', 300), ('قاسم', '456', 303), ('اهرم', '789', 305)]:
        db.upsert_symbol(dict(symbol_fa=symbol, ins_code=code, instrument_type_code=kind))
    return PaperRepository(db), clock


def portfolio(paper, **changes):
    repo, _ = paper
    args = dict(name='آزمایش موقت', capital_rial=100_000, rationale='روش دستی ثابت')
    args.update(changes)
    return repo.create(**args)


def idea(repo, key, **changes):
    args = dict(symbol='فولاد', label='تحلیل اول', thesis='شواهد اولیه', invalidation='فرضیه رد شود')
    args.update(changes)
    return repo.register_idea(key, **args)


def snapshot(db, at, **changes):
    values = dict(captured_at=at.isoformat(), provider_timestamp=at.isoformat(), last_price=1000,
                  low_price=950, high_price=1100, ref_price=1000, price_limit_down=900, price_limit_up=1200,
                  bid_price_1=999, bid_vol_1=1000, bid_count_1=10, ask_price_1=1000, ask_vol_1=200,
                  ask_count_1=10, volume=20_000, value=20_000_000, buy_queue_volume=0, sell_queue_volume=0,
                  source='tsetmc', price_unit='Rial')
    values.update(changes)
    assert db.insert_snapshot(db.get_symbol_id('فولاد'), values)


def trade(repo, key, iid, **changes):
    args = dict(idea_id=iid, side='buy', quantity=10, price_rial=1000, rationale='آزمون فرضیه')
    args.update(changes)
    return repo.trade(key, **args)


def test_capital_independent_fixed_fees_and_shared_cash(paper, db, monkeypatch):
    repo, clock = paper
    first, second = portfolio(paper), portfolio(paper, capital_rial=20_000)
    iid = idea(repo, first)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, first, iid)
    report = repo.report(first)
    assert report['ledger']['cash_rial'] == pytest.approx(100_000 - 10_000 * (1 + settings.FEES['buy']))
    assert repo.report(second)['equity_rial'] == 20_000
    assert db.conn.execute('SELECT count(*) FROM personal_profile').fetchone()[0] == 0
    assert db.conn.execute('SELECT count(*) FROM journal_executions').fetchone()[0] == 0
    expected = report['equity_rial']
    monkeypatch.setitem(settings.FEES, 'sell', .5)
    assert repo.report(first)['equity_rial'] == expected
    assert repo.report(first)['protocol']['fees']['sell'] != .5


def test_partial_exit_attributes_costs_per_idea_without_blending(paper, db):
    repo, clock = paper
    key = portfolio(paper)
    one, two = idea(repo, key), idea(repo, key, label='تحلیل مستقل')
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, one, quantity=20)
    trade(repo, key, two, quantity=5, price_rial=1050)
    trade(repo, key, one, side='sell', quantity=10, price_rial=1100)
    report = repo.report(key)
    by_idea = {a['id']: a for a in report['attribution']}
    expected = 11000 * (1 - settings.FEES['sell']) - 10000 * (1 + settings.FEES['buy'])
    assert by_idea[one]['realized_net_rial'] == pytest.approx(expected)
    assert by_idea[two]['realized_net_rial'] == 0
    assert report['ledger']['positions'][two]['cost_rial'] == pytest.approx(5250 * (1 + settings.FEES['buy']))
    assert sum(a['realized_net_rial'] for a in by_idea.values()) == pytest.approx(report['ledger']['realized_net_rial'])
    with pytest.raises(ValueError, match='موجودی'):
        trade(repo, key, two, side='sell', quantity=6)
    with pytest.raises(ValueError, match='نقدینگی'):
        trade(repo, key, two, quantity=100)


def test_idempotency_identical_request_and_global_key_collision(paper):
    repo, _ = paper
    key, other = portfolio(paper), portfolio(paper)
    iid, oi = idea(repo, key), idea(repo, other)
    assert trade(repo, key, iid, request_id='same') == 'same'
    assert trade(repo, key, iid, request_id='same') == 'same'
    assert len(repo.report(key)['events']) == 1
    for pid, analysis, quantity in [(key, iid, 11), (other, oi, 10)]:
        with pytest.raises(ValueError, match='متفاوت'):
            trade(repo, pid, analysis, request_id='same', quantity=quantity)


@pytest.mark.parametrize('changes', [dict(capital_rial=True), dict(capital_rial=10.1), dict(capital_rial=float('nan')),
                                     dict(capital_rial=2**54), dict(mode='actual'), dict(name=''), dict(rationale='')])
def test_invalid_portfolio_inputs(paper, changes):
    with pytest.raises(ValueError):
        portfolio(paper, **changes)


def test_scope_idea_binding_and_closed_idea_cannot_be_reopened(paper, db):
    repo, clock = paper
    key, other = portfolio(paper), portfolio(paper)
    for changes in [dict(symbol='اهرم'), dict(horizon='one_year'), dict(thesis=''), dict(stop_rial=1100, target_rial=1000)]:
        with pytest.raises(ValueError):
            idea(repo, key, **changes)
    iid = idea(repo, key)
    with pytest.raises(ValueError, match='مربوط'):
        trade(repo, other, iid)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, iid)
    trade(repo, key, iid, side='sell')
    report = repo.report(key)
    assert report['attribution'][0]['status'] == 'closed'
    assert report['ledger']['positions'] == {}
    with pytest.raises(ValueError, match='بسته'):
        trade(repo, key, iid)


def test_prospective_clock_and_untraded_denominator(paper):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    assert repo.report(key)['attribution'][0]['status'] == 'untraded'
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(ValueError, match='عقب'):
        trade(repo, key, iid)


def test_unknown_mark_no_numeric_profit_and_loss_floor_does_not_hide_sale(paper, db):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    trade(repo, key, iid, quantity=80)
    assert repo.report(key)['equity_rial'] is None
    with pytest.raises(ValueError, match='نامعلوم'):
        trade(repo, key, iid)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0], last_price=500, bid_price_1=500, ask_price_1=501,
             low_price=450, high_price=600, price_limit_down=400, price_limit_up=700)
    assert repo.report(key)['loss_limit_reached']
    with pytest.raises(ValueError, match='سقف زیان'):
        trade(repo, key, iid)
    trade(repo, key, iid, side='sell', quantity=80, price_rial=500)
    report = repo.report(key)
    assert report['ledger']['realized_net_rial'] < -40_000
    assert report['equity_rial'] == report['ledger']['cash_rial']
    assert report['protocol']['max_loss_rial'] == 10_000


def test_observed_requires_post_decision_fresh_quote_and_records_conservative_price(paper, db):
    repo, clock = paper
    snapshot(db, clock[0])
    key = portfolio(paper, mode='observed')
    iid = idea(repo, key)
    with pytest.raises(ValueError, match='پس از'):
        trade(repo, key, iid, price_rial=None)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    with pytest.raises(ValueError, match='دلخواه'):
        trade(repo, key, iid)
    trade(repo, key, iid, price_rial=None)
    report = repo.report(key)
    event = report['events'][0]
    assert event['payload']['price_rial'] >= 1000
    assert event['payload']['evidence']['snapshot']['captured_at'] > report['ideas'][iid]['created_at']
    assert event['payload']['evidence']['execution_proven'] is False
    assert report['profitability_proven'] is False


def test_price_changed_since_display_requires_review_before_trade(paper, db):
    repo, clock = paper
    key = portfolio(paper, mode='observed')
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    displayed = repo.report(key)['observations'][iid]['snapshot']['id']
    clock[0] += timedelta(seconds=1)
    snapshot(db, clock[0], ask_price_1=1005)
    with pytest.raises(ValueError, match='تغییر کرده'):
        trade(repo, key, iid, price_rial=None, expected_snapshot_id=displayed)
    assert repo.report(key)['events'] == []


@pytest.mark.parametrize('changes,elapsed', [({}, 50), ({'buy_queue_volume': 500}, 1), ({'sell_queue_volume': 1}, 1),
    ({'ask_vol_1': None}, 1), ({'value': None}, 1), ({'price_unit': 'Toman'}, 1),
    ({'bid_price_1': 1050}, 1), ({'high_price': 1000}, 1)])
def test_observed_rejects_stale_queue_missing_capacity_units_and_inconsistent_book(paper, db, changes, elapsed):
    repo, clock = paper
    key = portfolio(paper, mode='observed')
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=1)
    snapshot(db, clock[0], **changes)
    clock[0] += timedelta(seconds=elapsed)
    with pytest.raises(ValueError):
        trade(repo, key, iid, price_rial=None)
    assert repo.report(key)['events'] == []


def test_depth_and_day_participation_are_consumed_across_ideas(paper, db):
    repo, clock = paper
    key = portfolio(paper, mode='observed', capital_rial=1_000_000)
    one, two = idea(repo, key), idea(repo, key, label='تحلیل دوم')
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0], ask_vol_1=15, volume=500)
    trade(repo, key, one, quantity=10, price_rial=None)
    with pytest.raises(ValueError, match='حداکثر 5'):
        trade(repo, key, two, quantity=6, price_rial=None)
    trade(repo, key, two, quantity=5, price_rial=None)
    clock[0] += timedelta(seconds=1)
    snapshot(db, clock[0], ask_vol_1=100, volume=500)
    with pytest.raises(ValueError, match='حداکثر 10'):
        trade(repo, key, two, quantity=11, price_rial=None)


def test_sale_capacity_is_shared_with_buys_and_outside_session_rejected(paper, db):
    repo, clock = paper
    key = portfolio(paper, mode='observed', capital_rial=1_000_000)
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0], volume=300)
    trade(repo, key, iid, price_rial=None)
    with pytest.raises(ValueError, match='حداکثر 5'):
        trade(repo, key, iid, side='sell', quantity=6, price_rial=None)
    clock[0] = clock[0].replace(hour=13)
    snapshot(db, clock[0])
    with pytest.raises(ValueError, match='ساعت'):
        trade(repo, key, iid, side='sell', price_rial=None)


def test_action_issue_survives_sale_and_no_synthetic_shares_or_cash(paper, db):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, iid)
    sid = db.get_symbol_id('فولاد')
    db.insert_daily_price(sid, '2026-10-13', open=1000, high=1100, low=950, close=1000,
                          close_final_raw=1000, ref_price_raw=1000)
    clock[0] += timedelta(days=1)
    snapshot(db, clock[0], ref_price=500, last_price=500, bid_price_1=499, ask_price_1=500,
             low_price=450, high_price=550, price_limit_down=400, price_limit_up=600)
    report = repo.report(key)
    assert report['equity_rial'] is None and any('شکست مرجع' in p for p in report['integrity'][iid])
    assert report['ledger']['positions'][iid]['quantity'] == 10
    trade(repo, key, iid, side='sell', price_rial=500)
    assert repo.report(key)['equity_rial'] is None
    assert repo.report(key)['ledger']['positions'] == {}
    assert db.conn.execute('SELECT count(*) FROM corporate_actions').fetchone()[0] == 0


def test_missing_history_and_stale_price_do_not_create_return(paper, db):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, iid)
    clock[0] = clock[0].replace(day=17)
    snapshot(db, clock[0])
    report = repo.report(key)
    assert report['return_pct'] is None
    assert report['integrity'][iid]


def test_valuation_is_frozen_export_chain_and_tamper_detection(paper, db):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, iid)
    valuation = repo.save_valuation(key)
    exported = repo.export(key)
    assert exported['events'][-1]['payload'] == valuation
    assert exported['protocol']['code']['self_contained_replay'] is False
    assert exported['ideas'][iid]['thesis'] == 'شواهد اولیه'
    for table in ('paper_portfolios', 'paper_ideas', 'paper_events'):
        for operation in (f'DELETE FROM {table}', f'UPDATE {table} SET id=id'):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                db.conn.execute(operation)
            db.conn.rollback()
    db.conn.execute('DROP TRIGGER paper_events_no_update')
    db.conn.execute("UPDATE paper_events SET payload_json='{}' WHERE kind='trade'")
    db.conn.commit()
    with pytest.raises(ValueError, match='مخدوش'):
        repo.report(key)


def test_code_change_blocks_new_trade_and_profit_claim(paper, monkeypatch):
    repo, _ = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    identity = json.loads(repo.portfolios()[0]['protocol_json'])['code']
    monkeypatch.setattr('database.paper.code_identity', lambda: dict(identity, sha256='changed'))
    with pytest.raises(ValueError, match='کد'):
        trade(repo, key, iid)
    assert repo.report(key)['equity_rial'] is None


def test_session_configuration_change_does_not_silently_change_trial(paper, monkeypatch):
    repo, _ = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    monkeypatch.setitem(settings.TRADING_HOURS, 'end', '13:30')
    assert repo.report(key)['runtime_matches'] is False
    with pytest.raises(ValueError, match='کد'):
        trade(repo, key, iid)


def test_migration_thirteen_idempotent(db):
    from database.migrator import MIGRATIONS_DIR, _run_sql_migration

    assert db.schema_version() == 13
    _run_sql_migration(db.conn, MIGRATIONS_DIR / '013_paper_portfolios.sql')
    db.conn.commit()
    assert db.conn.execute('SELECT count(*) FROM paper_portfolios').fetchone()[0] == 0


def test_upgrade_from_twelve_backs_up_and_preserves_data(tmp_path, monkeypatch):
    from database import migrator
    from database.db import DatabaseManager

    original = migrator.discover_migrations
    path = tmp_path / 'existing.db'
    with monkeypatch.context() as scoped:
        scoped.setattr(migrator, 'discover_migrations', lambda: [m for m in original() if m[0] <= 12])
        with DatabaseManager(db_path=path) as old:
            old.upsert_symbol(dict(symbol_fa='فولاد', ins_code='123', instrument_type_code=300))
            assert old.schema_version() == 12
    with DatabaseManager(db_path=path) as upgraded:
        assert upgraded.schema_version() == 13
        assert upgraded.get_symbol('فولاد')['ins_code'] == '123'
        assert upgraded.conn.execute('SELECT count(*) FROM paper_portfolios').fetchone()[0] == 0
    backups = list((tmp_path / 'backups').glob('existing-*.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 12


def test_concurrent_buys_cannot_double_spend(paper, db):
    from concurrent.futures import ThreadPoolExecutor

    from database.db import DatabaseManager

    repo, clock = paper
    key = portfolio(paper)
    first, second = idea(repo, key), idea(repo, key, label='تحلیل دوم')
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])

    def attempt(iid):
        with DatabaseManager(db_path=db.db_path) as connection:
            try:
                trade(PaperRepository(connection), key, iid, quantity=50)
                return 'filled'
            except ValueError as exc:
                assert 'نقدینگی' in str(exc)
                return 'blocked'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [first, second]))
    assert sorted(results) == ['blocked', 'filled']
    assert len(repo.report(key)['events']) == 1


def test_buy_that_would_cross_floor_after_costs_is_rejected(paper, db):
    repo, clock = paper
    key = portfolio(paper, mode='observed')
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0], bid_price_1=500, low_price=400, price_limit_down=400)
    with pytest.raises(ValueError, match='کف زیان'):
        trade(repo, key, iid, quantity=50, price_rial=None)
    assert repo.report(key)['events'] == []


def test_journal_freezes_sell_fee_for_virtual_profile():
    state = summarize_executions([dict(symbol_fa='الف', side='buy', quantity=10, price_rial=1000, fee_rial=40)],
                                RiskProfile(100_000, 10_000), {'الف': 1100}, sell_fee_rate=.01)
    assert state['equity_rial'] == 100_000 - 10_040 + 11_000 * .99


def test_ui_create_analysis_buy_partial_sell_and_export(dashboard_db):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD

    with DatabaseManager(db_path=dashboard_db) as db:
        db.upsert_symbol(dict(symbol_fa='فولاد', ins_code='123', instrument_type_code=300))
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('💼 پورتفوی مجازی').run()
    assert not at.exception
    next(w for w in at.text_input if w.label == 'نام پورتفو یا روش').set_value('دفتر آزمون')
    next(w for w in at.number_input if w.label == 'سرمایه آغازین مجازی (تومان)').set_value(10_000)
    next(w for w in at.selectbox if w.label == 'شیوه ثبت معامله').set_value('سناریوی دستی با قیمت انتخابی من')
    next(w for w in at.text_area if w.label == 'روش این پورتفو و معیار ارزیابی آن').set_value('روش ثابت آزمایشی')
    next(b for b in at.button if b.label == 'افتتاح پورتفوی مجازی').click().run()
    assert not at.exception
    next(w for w in at.text_input if w.label == 'عنوان تحلیل').set_value('تحلیل موقت')
    next(w for w in at.text_area if w.label == 'دلیل انتخاب و شواهد تحلیل').set_value('شاهد موقت')
    next(w for w in at.text_area if 'چه اتفاقی تحلیل' in w.label).set_value('خروج پس از رد فرضیه')
    next(b for b in at.button if b.label == 'ثبت تحلیل مجازی').click().run()
    assert not at.exception
    next(w for w in at.number_input if 'قیمت سناریوی دستی' in w.label).set_value(100.0)
    next(w for w in at.number_input if w.label == 'تعداد سهم مجازی').set_value(10)
    next(w for w in at.text_area if 'دلیل انجام این' in w.label).set_value('خرید طبق تحلیل')
    next(b for b in at.button if b.label == 'ثبت معامله در پورتفوی مجازی').click().run()
    assert not at.exception
    assert any('ثبت شد' in s.value for s in at.success)
    next(w for w in at.radio if w.label == 'سمت معامله مجازی').set_value('فروش مجازی')
    next(w for w in at.number_input if 'قیمت سناریوی دستی' in w.label).set_value(110.0)
    next(w for w in at.number_input if w.label == 'تعداد سهم مجازی').set_value(4)
    next(w for w in at.text_area if 'دلیل انجام این' in w.label).set_value('فروش بخشی')
    next(b for b in at.button if b.label == 'ثبت معامله در پورتفوی مجازی').click().run()
    assert not at.exception
    with DatabaseManager(db_path=dashboard_db, read_only=True) as db:
        repo = PaperRepository(db)
        result = repo.report(repo.portfolios()[0]['id'])
        assert len(result['events']) == 2
        assert next(iter(result['ledger']['positions'].values()))['quantity'] == 6
        assert db.conn.execute('SELECT count(*) FROM journal_executions').fetchone()[0] == 0


def test_observed_ui_displays_quote_and_will_not_fill_changed_price(dashboard_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD

    clock = [datetime(2026, 10, 13, 10, tzinfo=TEHRAN_TZ)]
    monkeypatch.setattr('database.paper.now_tehran', lambda: clock[0])
    monkeypatch.setattr('dashboard.paper.now_tehran', lambda: clock[0])
    with DatabaseManager(db_path=dashboard_db) as db:
        db.upsert_symbol(dict(symbol_fa='فولاد', ins_code='123', instrument_type_code=300))
        repo = PaperRepository(db)
        key = repo.create(name='آزمایش مظنه', capital_rial=100_000, rationale='قاعده ثابت', mode='observed')
        iid = idea(repo, key)
        clock[0] += timedelta(seconds=10)
        snapshot(db, clock[0])
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('💼 پورتفوی مجازی').run()
    assert not at.exception
    assert any(m.label == 'بهترین عرضه مشاهده‌شده برای خرید' for m in at.metric)
    assert next(n for n in at.number_input if 'قیمت سناریوی دستی' in n.label).disabled
    clock[0] += timedelta(seconds=1)
    with DatabaseManager(db_path=dashboard_db) as db:
        snapshot(db, clock[0], ask_price_1=1005)
    next(w for w in at.text_area if 'دلیل انجام این' in w.label).set_value('خرید با قیمت مشاهده‌شده')
    next(b for b in at.button if b.label == 'ثبت معامله در پورتفوی مجازی').click().run()
    assert not at.exception and any('تغییر کرده' in e.value for e in at.error)
    next(w for w in at.text_area if 'دلیل انجام این' in w.label).set_value('تأیید مظنه جدید')
    next(b for b in at.button if b.label == 'ثبت معامله در پورتفوی مجازی').click().run()
    assert not at.exception and any('ثبت شد' in s.value for s in at.success)
    with DatabaseManager(db_path=dashboard_db, read_only=True) as db:
        report = PaperRepository(db).report(key)
        assert len(report['events']) == 1
        assert report['events'][0]['payload']['price_rial'] >= 1005
        assert report['ideas'][iid]['created_at'] < report['events'][0]['recorded_at']
