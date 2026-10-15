"""Synthetic fixtures only. Private account information must never enter this suite."""

import copy
import io
import json
import sqlite3
import zipfile
from xml.sax.saxutils import escape

import pytest

from core.broker_account import compare_trade_cash, reconcile_account
from core.timeutils import now_tehran_iso
from data_collector.farabi import (
    FILLS_HEADERS,
    PORTFOLIO_HEADERS,
    TURNOVER_HEADERS,
    import_report,
    parse_export,
    parse_portfolio,
    xlsx_rows,
)
from database.broker import BrokerRepository, broker_report
from tests.test_dashboard import dashboard_db  # noqa: F401


@pytest.fixture
def capture():
    return dict(format='farabi-dom-v1', kind='portfolio', currency='IRR',
                source_url='https://farabixo.irfarabi.com/', captured_at=now_tehran_iso(), expected_positions=2,
                headers=PORTFOLIO_HEADERS,
                summary=dict(net_assets_rial=3190, portfolio_value_rial=2990, cash_rial=200,
                             buying_power_rial=200, blocked_rial=0, buy_total_rial=2000,
                             broker_pnl_rial=990, broker_return_pct=49.5),
                rows=[['فولاد', '2', '-100', '0', '-101', '(0%) 1,000', '1,990', '(+∞%) +1,990'],
                      ['خزاميا', '1', '2,000', '2,000', '2,026', '(0%) 1,010', '1,000', '(-50%) -1,000']])


def encoded(payload):
    return json.dumps(payload, ensure_ascii=False).encode()


def workbook(rows):
    # A minimal values-only XLSX fixture, created entirely under tests.
    buffer = io.BytesIO()
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    xml_rows = []
    for i, row in enumerate(rows, start=1):
        cells = ''.join(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                        for j, value in enumerate(row))
        xml_rows.append(f'<row r="{i}">{cells}</row>')
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    return buffer.getvalue()


def ledger_rows():
    return [TURNOVER_HEADERS, ['-', '-', 0, 0, 0, 0, 0, 'نقل از قبل...'],
            ['1400/01/01', '-', 0, 0, 0, 10000, 10000, 'دريافت واريز آزمایشی'],
            ['1400/01/02', 'فولاد', 2, 1000, -2008, 0, 7992, 'خريد 2 سهم آزمایشی'],
            ['1400/01/03', 'فولاد', 1, 1100, 0, 1090, 9082, 'فروش 1 سهم آزمایشی']]


def test_capture_keeps_negative_adjusted_cost_and_unknown_infinite_return(capture):
    parsed = parse_portfolio(encoded(capture))
    assert parsed['rows'][0]['broker_net_average_rial'] == -100
    assert parsed['rows'][0]['broker_return_pct'] is None
    assert parsed['rows'][1]['symbol_fa'] == 'خزامیا'
    assert not parsed['actual_history_complete']
    assert parsed['captured_at'].endswith('+03:30')


@pytest.mark.parametrize('mutation', ['missing_row', 'duplicate', 'wrong_currency', 'wrong_header', 'wrong_total', 'future', 'wrong_origin'])
def test_capture_rejects_incomplete_or_inconsistent_sources(capture, mutation):
    if mutation == 'missing_row':
        capture['rows'].pop()
    elif mutation == 'duplicate':
        capture['rows'][1] = capture['rows'][0]
    elif mutation == 'wrong_currency':
        capture['currency'] = 'TOMAN'
    elif mutation == 'wrong_header':
        capture['headers'] = list(reversed(PORTFOLIO_HEADERS))
    elif mutation == 'wrong_total':
        capture['summary']['portfolio_value_rial'] += 100
    elif mutation == 'future':
        capture['captured_at'] = '2999-01-01T00:00:00+03:30'
    else:
        capture['source_url'] = 'https://other.example/'
    with pytest.raises(ValueError):
        parse_portfolio(encoded(capture))


def test_ledger_verifies_every_running_balance_and_debit_sign():
    rows = ledger_rows()
    result = parse_export(workbook(rows))
    assert result['balance_chain_verified'] and result['ending_balance_rial'] == 9082
    assert result['opening_balance_rial'] == 0
    assert [r['category'] for r in result['rows']] == ['cash_in', 'buy', 'sell']
    assert result['rows'][1]['debit_rial'] == -2008
    broken = copy.deepcopy(rows)
    broken[3][6] = 7993
    with pytest.raises(ValueError, match='مانده'):
        parse_export(workbook(broken))
    broken = copy.deepcopy(rows)
    broken[3][4] = 2008
    with pytest.raises(ValueError, match='علامت'):
        parse_export(workbook(broken))


def test_malformed_json_shapes_produce_a_clear_validation_error(capture):
    for source in [[], None, {**capture, 'source_url': 1}, {**capture, 'summary': []},
                   {**capture, 'rows': [None]}, {**capture, 'rows': ['12345678']}]:
        with pytest.raises(ValueError):
            parse_portfolio(encoded(source))


def test_fills_preserve_identical_executions_actual_fractional_fees_and_missing_symbol():
    row = ['1400/01/01 10:00:00', 'خرید', 'فولاد', 2, 1000, '7.424']
    result = parse_export(workbook([FILLS_HEADERS, row, row, [*row[:2], '', *row[3:]]]))
    assert len(result['rows']) == 3  # Identical-looking executions can be distinct fills.
    assert result['rows'][0]['fee_rial'] == 7.424
    assert result['rows'][2]['raw_symbol'] is None
    assert not result['actual_history_complete']


def test_xlsx_rejects_formulas_entities_and_truncated_zip():
    with pytest.raises(ValueError):
        xlsx_rows(b'not a zip')
    for xml in ['<!DOCTYPE a [<!ENTITY b "bad">]><a/>',
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>']:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('xl/worksheets/sheet1.xml', xml)
        with pytest.raises(ValueError):
            xlsx_rows(buffer.getvalue())


def test_import_is_idempotent_append_only_and_separate_from_market(db, capture):
    repo = BrokerRepository(db)
    source = encoded(capture)
    first = import_report(repo, source, 'portfolio.json')
    assert import_report(repo, source, 'renamed.json') == first
    assert len(repo.latest('portfolio')['rows']) == 2
    assert db.conn.execute('SELECT COUNT(*) FROM journal_executions').fetchone()[0] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM daily_prices').fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.conn.execute('DELETE FROM broker_records')
    db.conn.rollback()
    capture['rows'].pop()
    with pytest.raises(ValueError):
        import_report(repo, encoded(capture), 'bad.json')
    assert db.conn.execute('SELECT COUNT(*) FROM broker_imports').fetchone()[0] == 1


def test_baseline_adoption_is_explicit_and_does_not_follow_later_snapshots(db, capture):
    repo = BrokerRepository(db)
    first = import_report(repo, encoded(capture), 'portfolio.json')
    assert repo.baseline() is None
    key = repo.adopt_baseline(first)
    assert repo.adopt_baseline(first) == key
    assert repo.baseline()['capital_rial'] == 3190 and repo.baseline()['max_loss_rial'] == 319
    capture['summary']['cash_rial'] += 100
    capture['summary']['buying_power_rial'] += 100
    capture['summary']['net_assets_rial'] += 100
    import_report(repo, encoded(capture), 'later.json')
    assert repo.baseline()['capital_rial'] == 3190
    profile = db.conn.execute('SELECT * FROM personal_profile').fetchone()
    assert profile['actual_history_complete'] == 0
    from core.risk import RiskProfile
    from database.workbench import WorkbenchRepository
    with pytest.raises(ValueError, match='مبنای مرحله'):
        WorkbenchRepository(db).save_profile(RiskProfile(5000, 500))
    with pytest.raises(ValueError, match='تطبیق'):
        WorkbenchRepository(db).save_profile(RiskProfile(3190, 319), actual_history_complete=True)


def test_reconciliation_preserves_scope_and_does_not_claim_verified_market_date(capture):
    portfolio = parse_portfolio(encoded(capture))
    result = reconcile_account(portfolio, market_rows=[dict(symbol_fa='فولاد', date='2024-01-01', close_raw=1000)])
    assert result['price_checks'] == {'price_equal_date_unverified': 1, 'missing_archive': 1}
    assert not result['trade_recommendations_enabled'] and result['total_account_return'] is None
    assert len(result['positions']) == 2


def test_old_capture_cannot_start_a_new_current_phase(db, capture):
    repo = BrokerRepository(db)
    capture['captured_at'] = '2020-01-01T00:00:00+03:30'
    source = import_report(repo, encoded(capture), 'old.json')
    with pytest.raises(ValueError, match='همان روز'):
        repo.adopt_baseline(source)
    assert repo.baseline() is None


def test_migration_nine_can_be_repeated(db):
    from database.migrator import MIGRATIONS_DIR, _run_sql_migration
    _run_sql_migration(db.conn, MIGRATIONS_DIR / '009_broker_imports.sql')
    db.conn.commit()
    assert db.schema_version() >= 9


def test_trade_cash_comparison_detects_missing_coverage_and_keeps_small_differences():
    ledger = [dict(date='2024-01-01', category='buy', debit_rial=-1010, credit_rial=0),
              dict(date='2024-01-02', category='sell', debit_rial=0, credit_rial=990),
              dict(date='2024-01-03', category='buy', debit_rial=-500, credit_rial=0),
              dict(date='2024-01-05', category='buy', debit_rial=-2001, credit_rial=0)]
    orders = [dict(date='2024-01-01', side='buy', quantity=1, price_rial=1000, fee_rial=10),
              dict(date='2024-01-02', side='sell', quantity=1, price_rial=1000, fee_rial=11),
              dict(date='2024-01-04', side='buy', quantity=1, price_rial=100, fee_rial=0),
              dict(date='2024-01-05', side='buy', quantity=2, price_rial=1000, fee_rial=10)]
    result = compare_trade_cash(ledger, orders)
    assert [r['status'] for r in result['groups']] == [
        'equal_cash_total', 'within_display_rounding', 'only_ledger', 'only_order_export', 'amount_difference']
    assert result['groups'][1]['difference_rial'] == -1
    assert result['groups'][2]['difference_rial'] == -500
    assert result['groups'][-1]['difference_rial'] == 9
    assert not result['complete_execution_history_verified']


def test_public_quote_observation_never_expands_research_universe(db, capture):
    repo = BrokerRepository(db)
    import_report(repo, encoded(capture), 'portfolio.json')
    repo.record_market_observation(dict(symbol_fa='خزامیا', ins_code='123', instrument_type_code=400),
                                   dict(date='2024-01-01', close_raw=1010))
    report = broker_report(db)
    row = next(p for p in report['positions'] if p['symbol_fa'] == 'خزامیا')
    assert row['instrument_type_code'] == 400 and row['archive_last_rial'] == 1010
    assert row['research_scope_eligible'] is False
    assert row['price_check'] == 'price_equal_date_unverified'
    assert db.conn.execute('SELECT COUNT(*) FROM symbols').fetchone()[0] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM daily_prices').fetchone()[0] == 0
    assert report['sources']['portfolio']['parser_version'] == 'farabi-v2'
    with pytest.raises(ValueError, match='آینده'):
        repo.record_market_observation(dict(symbol_fa='خزامیا', ins_code='123'), dict(date='2999-01-01', close_raw=1010))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.conn.execute('UPDATE account_market_observations SET symbol_fa=?', ('wrong',))
    db.conn.rollback()
    from database.migrator import MIGRATIONS_DIR, _run_sql_migration
    _run_sql_migration(db.conn, MIGRATIONS_DIR / '010_account_market_observations.sql')
    db.conn.commit()


def test_quote_refresh_keeps_research_separate_and_preserves_previous_quote_on_failure(db, capture):
    import pandas as pd

    from data_collector.account_quotes import refresh_account_quotes

    repo = BrokerRepository(db)
    import_report(repo, encoded(capture), 'portfolio.json')

    class Provider:
        fail = False

        def search_symbol(self, symbol):
            return dict(symbol_fa=symbol, ins_code='1' if symbol == 'فولاد' else '2', instrument_type_code=400)

        def get_symbol_history(self, ins_code, last_n):
            assert last_n == 10
            if self.fail:
                return None
            return pd.DataFrame([dict(date='2024-01-01', close_raw=1000 if ins_code == '1' else 1010)])

    provider = Provider()
    assert refresh_account_quotes(db, provider)['succeeded'] == 2
    previous = broker_report(db)
    provider.fail = True
    assert refresh_account_quotes(db, provider)['failed'] == 2
    assert broker_report(db)['price_checks'] == previous['price_checks']
    assert db.conn.execute('SELECT count(*) FROM account_market_observations').fetchone()[0] == 2
    assert db.conn.execute('SELECT count(*) FROM symbols').fetchone()[0] == 0
    assert db.conn.execute('SELECT count(*) FROM daily_prices').fetchone()[0] == 0


def test_same_date_quote_correction_uses_observation_time(db, capture, monkeypatch):
    from database import broker

    repo = BrokerRepository(db)
    import_report(repo, encoded(capture), 'portfolio.json')
    meta = dict(symbol_fa='فولاد', ins_code='1', tse_code='TEST', instrument_type_code=300)
    db.upsert_symbol(meta)
    symbol_id = db.get_symbol('فولاد')['id']
    db.conn.execute("INSERT INTO daily_prices(symbol_id,date,source,close_raw,captured_at) VALUES(?,?,'tsetmc',?,?)",
                    (symbol_id, '2024-01-01', 950, '2024-01-02T00:00:00+03:30'))
    db.conn.commit()
    monkeypatch.setattr(broker, 'now_tehran_iso', lambda: '2024-01-03T00:00:00+03:30')
    repo.record_market_observation(meta, dict(date='2024-01-01', close_raw=1000))
    assert next(p for p in broker_report(db)['positions'] if p['symbol_fa'] == 'فولاد')['archive_last_rial'] == 1000
    db.conn.execute("UPDATE daily_prices SET captured_at=?,close_raw=970 WHERE symbol_id=?",
                    ('2024-01-04T00:00:00+03:30', symbol_id))
    db.conn.commit()
    assert next(p for p in broker_report(db)['positions'] if p['symbol_fa'] == 'فولاد')['archive_last_rial'] == 970


def test_populated_broker_page_and_actual_cash_use_import_not_paper_capital(dashboard_db, capture):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD
    with DatabaseManager(db_path=dashboard_db) as db:
        repo = BrokerRepository(db)
        source = import_report(repo, encoded(capture), 'portfolio.json')
        import_report(repo, workbook(ledger_rows()), 'ledger.xlsx')
        import_report(repo, workbook([FILLS_HEADERS, ['1400/01/02 10:00:00', 'خرید', 'فولاد', 2, 1000, 8]]), 'orders.xlsx')
        repo.adopt_baseline(source)
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🏦 حساب و تطبیق فارابی').run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == 'قدرت خرید در فارابی').value == '20 تومان'
    assert len(app.dataframe[0].value) == 2
    assert any('تاریخ/سمت' in w.value for w in app.warning)
    app.sidebar.radio[0].set_value('🛡️ برنامه و دفتر معاملات').run()
    app.radio[0].set_value('واقعی').run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == 'قدرت خرید آخرین گزارش').value == '20 تومان'
    assert next(m for m in app.metric if m.label == 'سود/زیان تحقق‌یافته خالص').value == 'نامعلوم'
    assert app.checkbox[0].disabled
    assert next(b for b in app.button if b.label == 'ذخیره تنظیمات').disabled


def test_real_incomplete_journal_never_displays_initial_capital_as_cash(dashboard_db):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from core.risk import RiskProfile
    from database.db import DatabaseManager
    from database.workbench import WorkbenchRepository
    from tests.test_dashboard import DASHBOARD
    with DatabaseManager(db_path=dashboard_db) as db:
        WorkbenchRepository(db).save_profile(RiskProfile(100_000, 10_000))
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🛡️ برنامه و دفتر معاملات').run()
    app.radio[0].set_value('واقعی').run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == 'قدرت خرید آخرین گزارش').value == 'نامعلوم'


def test_public_quote_job_button_starts_once_and_recovers_after_completion(dashboard_db, capture, monkeypatch):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from ops import jobs
    from tests.test_dashboard import DASHBOARD

    with DatabaseManager(db_path=dashboard_db) as db:
        import_report(BrokerRepository(db), encoded(capture), 'portfolio.json')
    state = dict(running=False, started_at=None, log_tail=[])
    dispatched = []
    monkeypatch.setattr(jobs, 'job_status', lambda name: state)

    def start(name, args):
        dispatched.append((name, args))
        state.update(running=True, started_at=now_tehran_iso())
        return True

    monkeypatch.setattr(jobs, 'start_job', start)
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🏦 حساب و تطبیق فارابی').run()
    next(b for b in app.button if b.label == 'به‌روزرسانی قیمت عمومی دارایی‌ها').click().run()
    assert not app.exception and dispatched == [('broker-quotes', ['broker-refresh-quotes'])]
    assert next(b for b in app.button if b.label == 'به‌روزرسانی قیمت عمومی دارایی‌ها').disabled
    state.update(running=False, log_tail=['دریافت عمومی پایان یافت'])
    app.run()
    assert not app.exception
    assert not next(b for b in app.button if b.label == 'به‌روزرسانی قیمت عمومی دارایی‌ها').disabled
