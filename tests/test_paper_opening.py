"""انتقال آغازین با داده ساختگی فقط در آزمون و دیتابیس موقت؛ هیچ اطلاعات واقعی حساب اینجا نیست."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from core.paper import canonical, digest
from core.timeutils import TEHRAN_TZ
from data_collector.farabi import PORTFOLIO_HEADERS, import_report
from database.broker import BrokerRepository
from database.paper import PaperRepository
from tests.test_dashboard import dashboard_db as dashboard_db


def source_data(at, *, cash=1000, power=800, blocked=200, right_quantity=4):
    rows = [['آزمونالف', '10', '0', '0', '0', '(0%) 1000', '9912', '(+∞%) +9912'],
            ['آزمونب', '20', '0', '0', '0', '(0%) 500', '9912', '(+∞%) +9912'],
            ['آزمونح', str(right_quantity), '0', '0', '0', '(0%) 100', '396', '(+∞%) +396']]
    return dict(format='farabi-dom-v1', kind='portfolio', currency='IRR', source_url='https://farabixo.irfarabi.com/',
                captured_at=at.isoformat(), market_date=at.date().isoformat(), expected_positions=3, headers=PORTFOLIO_HEADERS,
                rows=rows, summary=dict(net_assets_rial=20220+cash, portfolio_value_rial=20220,
                cash_rial=cash, buying_power_rial=power, blocked_rial=blocked, buy_total_rial=0,
                broker_pnl_rial=20220, broker_return_pct=None))


def store_source(db, at, **changes):
    data = json.dumps(source_data(at, **changes), ensure_ascii=False).encode()
    return import_report(BrokerRepository(db), data, 'temporary-test-dom.json')


def price(db, symbol, day, value, ref=None):
    db.insert_daily_price(db.get_symbol_id(symbol), day, open=value, high=value*1.1, low=value*.9, close=value,
                         close_raw=value, close_final_raw=value, ref_price_raw=ref if ref is not None else value)


def prepare(db, monkeypatch):
    clock = [datetime(2026, 10, 13, 15, tzinfo=TEHRAN_TZ)]
    monkeypatch.setattr('database.paper.now_tehran', lambda: clock[0])
    monkeypatch.setattr('data_collector.farabi.now_tehran', lambda: clock[0])
    monkeypatch.setattr('dashboard.paper.now_tehran', lambda: clock[0])
    for name, code, kind, quote in [('آزمونالف','111',300,1000), ('آزمونب','222',303,500), ('آزمونح','333',400,100)]:
        db.upsert_symbol(dict(symbol_fa=name, ins_code=code, instrument_type_code=kind))
        if kind in (300, 303):
            price(db, name, clock[0].date().isoformat(), quote)
    source = store_source(db, clock[0])
    return PaperRepository(db), clock, source


@pytest.fixture
def mirror(db, monkeypatch):
    return prepare(db, monkeypatch)


def create(mirror, **changes):
    repo, _, source = mirror
    return repo.create_from_broker(source, mode='manual', **changes)


def first_lot(report):
    return next(k for k, p in report['opening']['positions'].items() if p['symbol_fa'] == 'آزمونالف')


def test_copy_transfers_quantities_not_purchases_and_zero_cost_is_not_profit(mirror, db):
    repo, _, _ = mirror
    key = create(mirror)
    report = repo.report(key)
    assert len(report['ledger']['positions']) == 2 and len(report['opening']['passive']) == 1
    assert report['ledger']['positions'][first_lot(report)]['quantity'] == 10
    assert report['ledger']['cash_rial'] == 800 and report['locked_cash_rial'] == 200
    assert report['equity_rial'] == pytest.approx(21220)
    assert report['total_net_rial'] == pytest.approx(0)
    assert report['ledger']['realized_net_rial'] == 0 and report['ledger']['fees_rial'] == 0
    assert report['opening']['historical_profit_known'] is False
    assert report['events'] == [] and all(a['imported_inventory'] for a in report['attribution'])
    assert db.conn.execute('SELECT count(*) FROM journal_executions').fetchone()[0] == 0
    assert db.conn.execute('SELECT count(*) FROM personal_profile').fetchone()[0] == 0
    assert db.conn.execute('SELECT count(*) FROM broker_baselines').fetchone()[0] == 0


def test_shared_cash_cannot_spend_stock_value_or_locked_cash(mirror):
    repo, _, _ = mirror
    key = create(mirror)
    iid = repo.register_idea(key, symbol='آزمونالف', label='فرضیه تازه', thesis='شاهد آزمون', invalidation='شرط رد')
    with pytest.raises(ValueError, match='نقدینگی'):
        repo.trade(key, idea_id=iid, side='buy', quantity=1, price_rial=1000, rationale='بیش از نقد')
    assert repo.report(key)['events'] == []


def test_missing_market_marks_do_not_reuse_opening_prices_as_current(mirror, db):
    repo, _, _ = mirror
    key = create(mirror)
    with db.conn:
        db.conn.execute('DELETE FROM daily_prices')
    report = repo.report(key)
    assert len(report['ledger']['unknown_marks']) == 2
    assert report['ledger']['positions'][first_lot(report)]['quantity'] == 10
    assert report['equity_rial'] is None and report['return_pct'] is None
    assert report['opening']['positions'][first_lot(report)]['source_price_rial'] == 1000
    iid = repo.register_idea(key, symbol='آزمونالف', label='فرضیه تازه', thesis='شاهد', invalidation='رد')
    with pytest.raises(ValueError, match='نامعلوم'):
        repo.trade(key, idea_id=iid, side='buy', quantity=1, price_rial=10, rationale='ارزش کل نامعلوم است')
    assert repo.report(key)['events'] == []


def test_partial_sale_uses_opening_value_and_no_new_buy_fee(mirror):
    repo, _, _ = mirror
    key = create(mirror)
    iid = first_lot(repo.report(key))
    repo.trade(key, idea_id=iid, side='sell', quantity=5, price_rial=1100, rationale='خروج بخشی از موجودی آغازین')
    report = repo.report(key)
    assert report['ledger']['cash_rial'] == pytest.approx(800 + 5500 * .9912)
    assert report['ledger']['realized_net_rial'] == pytest.approx(5500*.9912 - 9912/2)
    assert report['ledger']['fees_rial'] == pytest.approx(5500*.0088)
    assert report['ledger']['positions'][iid]['quantity'] == 5
    assert report['ledger']['positions'][iid]['cost_rial'] == 4956
    with pytest.raises(ValueError, match='موجودی'):
        repo.trade(key, idea_id=iid, side='sell', quantity=6, price_rial=1100, rationale='فروش بیش از باقی‌مانده')


def test_opening_inventory_does_not_become_new_buy_thesis(mirror):
    repo, _, _ = mirror
    key = create(mirror)
    report = repo.report(key)
    assert all(p['historical_purchase_analysis_known'] is False for p in report['ideas'].values())
    with pytest.raises(ValueError, match='تحلیل جدید'):
        repo.trade(key, idea_id=first_lot(report), side='buy', quantity=1, price_rial=1000, rationale='ورود')
    with pytest.raises(ValueError, match='سهام عادی'):
        repo.register_idea(key, symbol='آزمونح', label='آزمون', thesis='آزمون', invalidation='رد')


def test_copy_idempotency_and_source_kept_independent(mirror, db):
    repo, clock, source = mirror
    first = create(mirror)
    assert create(mirror) == first
    assert len(repo.portfolios()) == 1
    with pytest.raises(ValueError, match='متفاوت'):
        create(mirror, name='نام دیگر')
    clock[0] += timedelta(seconds=1)
    new_source = store_source(db, clock[0], right_quantity=5)
    assert new_source != source
    report = repo.report(first)
    assert report['opening']['source']['id'] == source
    assert report['opening']['passive'][0]['quantity'] == 4
    assert report['passive_unknown'] == ['آزمونح'] and report['equity_rial'] is None


@pytest.mark.parametrize('seconds', [-1, 901, 86400])
def test_copy_requires_recent_capture_without_backdating(mirror, seconds):
    repo, clock, _ = mirror
    clock[0] += timedelta(seconds=seconds)
    with pytest.raises(ValueError, match='۱۵ دقیقه'):
        create(mirror)
    assert repo.portfolios() == []


def test_passive_value_is_not_silently_frozen_after_report_goes_stale(mirror):
    repo, clock, _ = mirror
    key = create(mirror)
    clock[0] += timedelta(days=1)
    report = repo.report(key)
    assert report['passive_values']['آزمونح'] is None
    assert report['return_pct'] is None
    assert report['opening']['passive'][0]['quantity'] == 4


def test_reference_break_after_opening_does_not_create_synthetic_shares(mirror, db):
    repo, clock, _ = mirror
    key = create(mirror)
    iid = first_lot(repo.report(key))
    clock[0] += timedelta(days=1)
    price(db, 'آزمونالف', clock[0].date().isoformat(), 500, ref=500)
    price(db, 'آزمونب', clock[0].date().isoformat(), 500)
    store_source(db, clock[0])
    report = repo.report(key)
    assert any('شکست مرجع' in p for p in report['integrity'][iid])
    assert report['ledger']['positions'][iid]['quantity'] == 10
    assert report['total_net_rial'] is None


def test_credit_not_converted_to_paper_cash(mirror, db):
    repo, clock, _ = mirror
    source = store_source(db, clock[0] - timedelta(seconds=1), cash=1000, power=5000, blocked=200)
    key = repo.create_from_broker(source, mode='manual')
    report = repo.report(key)
    assert report['ledger']['cash_rial'] == 800
    assert report['equity_rial'] == pytest.approx(21220)


def test_non_cash_rounding_difference_cannot_be_spent(mirror, db):
    from data_collector.farabi import parse_portfolio

    repo, clock, _ = mirror
    payload = source_data(clock[0])
    payload['summary']['portfolio_value_rial'] += 2
    payload['summary']['net_assets_rial'] += 2
    data = json.dumps(payload).encode()
    source = BrokerRepository(db).store_report(data, 'rounding-test.json', parse_portfolio(data), parser_version='farabi-v3')
    key = repo.create_from_broker(source, mode='manual')
    report = repo.report(key)
    assert report['opening_rounding_rial'] == 2
    assert report['ledger']['cash_rial'] == 800
    assert report['equity_rial'] == pytest.approx(21222)
    assert report['total_net_rial'] == pytest.approx(0)


def test_opening_is_append_only_and_tampering_is_detected(mirror, db):
    repo, _, _ = mirror
    key = create(mirror)
    for sql in ('DELETE FROM paper_openings', 'UPDATE paper_openings SET source_captured_at=source_captured_at'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.conn.execute(sql)
        db.conn.rollback()
    db.conn.execute('DROP TRIGGER paper_openings_no_update')
    db.conn.execute("UPDATE paper_openings SET payload_json='{}' WHERE portfolio_id=?", (key,))
    db.conn.commit()
    with pytest.raises(ValueError, match='مخدوش'):
        repo.report(key)


def test_legacy_cash_protocol_is_readable_and_not_rewritten(mirror, db):
    repo, _, _ = mirror
    protocol = repo._protocol(name='قدیمی', capital_rial=10000, rationale='آزمون', mode='manual')
    protocol['version'] = 'paper-portfolio-v1'
    protocol.pop('opening_kind')
    protocol['code']['sha256'] = 'old-code'
    with db.conn:
        repo._insert_protocol(protocol)
    report = repo.report(protocol['id'])
    assert report['opening'] is None and report['runtime_matches'] is False
    assert report['ledger']['cash_rial'] == 10000 and report['equity_rial'] is None
    raw = db.conn.execute('SELECT protocol_json,protocol_sha256 FROM paper_portfolios WHERE id=?', (protocol['id'],)).fetchone()
    assert raw[0] == canonical(protocol) and raw[1] == digest(protocol)


def test_upgrade_from_thirteen_preserves_journal_and_backups(tmp_path, monkeypatch):
    from database import migrator
    from database.db import DatabaseManager

    original = migrator.discover_migrations
    path = tmp_path / 'existing.db'
    with monkeypatch.context() as scoped:
        scoped.setattr(migrator, 'discover_migrations', lambda: [m for m in original() if m[0] <= 13])
        with DatabaseManager(db_path=path) as old:
            old.upsert_symbol(dict(symbol_fa='آزمون', ins_code='987', instrument_type_code=300))
            assert old.schema_version() == 13
    with DatabaseManager(db_path=path) as updated:
        assert updated.schema_version() == 14
        assert updated.get_symbol('آزمون')['ins_code'] == '987'
        migrator._run_sql_migration(updated.conn, migrator.MIGRATIONS_DIR / '014_paper_opening_holdings.sql')
        updated.conn.commit()
        assert updated.conn.execute('SELECT count(*) FROM paper_openings').fetchone()[0] == 0
    backups = list((tmp_path / 'backups').glob('*-v13.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 13


def test_copy_ui_is_explicit_and_lists_passive_holdings(dashboard_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD

    with DatabaseManager(db_path=dashboard_db) as db:
        repo, _, _ = prepare(db, monkeypatch)
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('💼 پورتفوی مجازی').run()
    next(b for b in at.button if b.label == 'ساخت کپی مجازی از همین گزارش فارابی').click().run()
    assert not at.exception
    assert any('کپی شد' in s.value for s in at.success)
    assert any('فقط برای نگهداری' in h.value for h in at.subheader)
    assert not any(n.label == 'سرمایه آغازین مجازی (تومان)' and n.value for n in at.number_input)
    with DatabaseManager(db_path=dashboard_db, read_only=True) as db:
        repo = PaperRepository(db)
        assert len(repo.portfolios()) == 1
        report = repo.report(repo.portfolios()[0]['id'])
        assert len(report['opening']['source']['rows']) == 3 and report['events'] == []
