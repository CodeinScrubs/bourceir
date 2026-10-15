"""بازآزمایی ادعاهای پیوست با داده ساختگی آزمون و دیتابیس موقت."""

import copy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import brs
from backtest.engine import BacktestEngine
from config import settings
from core.journal import summarize_executions
from core.risk import RiskProfile
from data_collector.farabi import parse_export, parse_portfolio, xlsx_rows
from data_collector.tsetmc import TsetmcProvider
from tests.test_broker import capture as capture
from tests.test_broker import encoded, ledger_rows, workbook
from tests.test_dashboard import DASHBOARD
from tests.test_dashboard import dashboard_db as dashboard_db
from tests.test_workbench import quote


@pytest.mark.parametrize('field', ['equity_rial', 'reserved_risk_rial'])
def test_unknown_account_budget_is_never_a_zero_equity_loss(field):
    result = quote(**{field: None})
    assert result['quantity'] == 0 and result['remaining_loss_budget_rial'] is None
    assert result['caps']['loss_budget'] is None
    assert any('نامعلوم' in message for message in result['blockers'])
    assert not any('مصرف شده' in message for message in result['blockers'])


def test_ledger_absorbs_only_the_already_allowed_cash_rounding_residue():
    event = dict(symbol_fa='آزمایش', side='buy', quantity=1, price_rial=999, fee_rial=1.0000005)
    state = summarize_executions([event], RiskProfile(1000, 100), {})
    assert state['cash_rial'] == 0
    assert quote(available_cash_rial=state['cash_rial'])['quantity'] == 0
    with pytest.raises(ValueError, match='نقدینگی'):
        summarize_executions([{**event, 'fee_rial': 1.01}], RiskProfile(1000, 100), {})


def test_invalid_intraday_band_cannot_produce_zero_loss_division():
    upper = 950 * (1 - settings.FEES['sell']) / (1 + settings.FEES['buy'])
    result = quote(intraday=True, slippage_bps=0, intraday_snapshot={'price_limit_up': upper})
    assert result['quantity'] == 0 and result['blockers']
    assert result['caps']['loss_budget'] is None and result['net_reward_risk'] is None


def test_unknown_liquidity_is_distinct_from_zero_observed_activity():
    result = quote(daily_value_rial=None)
    assert result['quantity'] == 0 and result['caps']['liquidity'] is None
    assert result['blockers'] and result['caps']['cash'] > 0


@pytest.mark.parametrize('project', [None, {}, {'version': ''}, {'version': True}])
def test_missing_or_malformed_project_version_never_uses_an_old_fallback(project):
    config = copy.deepcopy(settings.CONFIG)
    config['project'] = project
    with pytest.raises(ValueError, match='project.version'):
        settings.validate_config(config)


def test_sparse_xlsx_optional_tail_is_padded_but_missing_money_is_rejected():
    rows = ledger_rows()
    rows[-1].pop()
    assert len(xlsx_rows(workbook(rows))[-1]) == len(rows[0])
    assert parse_export(workbook(rows))['balance_chain_verified']
    rows[-1] = rows[-1][:6]
    with pytest.raises(ValueError):
        parse_export(workbook(rows))


def test_portfolio_display_digits_separators_and_bidi_do_not_change_money(capture):
    capture['rows'] = [[value.translate(str.maketrans('0123456789,%', '۰۱۲۳۴۵۶۷۸۹٬٪'))
                        for value in row] for row in capture['rows']]
    capture['rows'][0][7] = '\u2066' + capture['rows'][0][7] + '\u2069'
    report = parse_portfolio(encoded(capture))
    assert report['rows'][0]['broker_return_pct'] is None
    assert report['summary']['portfolio_value_rial'] == 2990


def test_portfolio_performance_accepts_unambiguous_amount_first_order(capture):
    capture['rows'][0][7] = '+1,990 (+∞%)'
    assert parse_portfolio(encoded(capture))['rows'][0]['broker_pnl_rial'] == 1990


def search_provider(monkeypatch, records, failed=()):
    provider = TsetmcProvider()
    monkeypatch.setattr(provider, '_get_json', lambda *a, **k: {'instrumentSearch': records})
    calls = []
    def info(code):
        calls.append(code)
        return None if code in failed else dict(symbol_fa='آزمایش', ins_code=code)
    monkeypatch.setattr(provider, 'get_instrument_info', info)
    return provider, calls


def test_search_uses_latest_unique_identity_and_does_not_fall_back_on_failure(monkeypatch):
    records = [dict(lVal18AFC='آزمایش', insCode='1', lastDate=20200101),
               dict(lVal18AFC='آزمایش', insCode='2', lastDate='20241001')]
    provider, calls = search_provider(monkeypatch, records)
    assert provider.search_symbol('آزمایش')['ins_code'] == '2' and calls == ['2']
    provider, calls = search_provider(monkeypatch, records, failed=('2',))
    assert provider.search_symbol('آزمایش') is None and calls == ['2']


@pytest.mark.parametrize('date', [20241001, None, 1, 'invalid', 20990101])
def test_search_ambiguous_or_unrankable_current_identity_is_unknown(monkeypatch, date):
    records = [dict(lVal18AFC='آزمایش', insCode='1', lastDate=20241001),
               dict(lVal18AFC='آزمایش', insCode='2', lastDate=date)]
    provider, calls = search_provider(monkeypatch, records)
    assert provider.search_symbol('آزمایش') is None and not calls


@pytest.mark.parametrize('indicator', [1, '1', None])
def test_unique_search_accepts_opaque_provider_indicator_without_inventing_a_date(monkeypatch, indicator):
    provider, calls = search_provider(monkeypatch, [dict(lVal18AFC='آزمایش',insCode='2',lastDate=indicator)])
    assert provider.search_symbol('آزمایش')['ins_code'] == '2' and calls == ['2']


def test_account_refresh_rechecks_cached_identity_without_changing_research(db, capture):
    from data_collector.account_quotes import refresh_account_quotes
    from data_collector.farabi import import_report
    from database.broker import BrokerRepository
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code='old', instrument_type_code=300))
    import_report(BrokerRepository(db), encoded(capture), 'synthetic.json')
    searches, prices = [], []
    class Provider:
        def search_symbol(self, symbol):
            searches.append(symbol)
            return dict(symbol_fa=symbol, ins_code='new-' + symbol, instrument_type_code=300)
        def get_symbol_history(self, ins_code, last_n):
            prices.append(ins_code)
            return pd.DataFrame([dict(date='2024-01-01', close_raw=1000)])
    assert refresh_account_quotes(db, Provider())['succeeded'] == 2
    assert 'فولاد' in searches and 'new-فولاد' in prices and 'old' not in prices
    assert db.get_symbol('فولاد')['ins_code'] == 'old'
    assert db.conn.execute('SELECT count(*) FROM daily_prices').fetchone()[0] == 0


@pytest.mark.parametrize('command', ['universe', 'backup'])
def test_cli_closes_database_after_operation_failure(monkeypatch, command):
    from data_collector import scheduler
    from database import db as db_module
    closed = []
    def fail(**kwargs):
        raise RuntimeError('synthetic failure')
    database = SimpleNamespace(close=lambda: closed.append(True), backup=fail)
    monkeypatch.setattr(db_module, 'DatabaseManager', lambda: database)
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **kw: SimpleNamespace(db=database, sync_universe=fail))
    with pytest.raises(RuntimeError, match='synthetic failure'):
        getattr(brs, 'cmd_' + command)(SimpleNamespace(keep=0))
    assert closed == [True]


@pytest.mark.parametrize('raises', [False, True])
def test_live_empty_universe_bootstrap_never_enters_capture_loop(monkeypatch, raises):
    from data_collector import scheduler
    closed, captures = [], []
    def sync():
        if raises:
            raise RuntimeError('synthetic bootstrap failure')
        return dict(status='failed')
    def collect(**kwargs):
        captures.append(True)
        return dict(status='failed', symbols_succeeded=0, symbols_failed=0)
    database = SimpleNamespace(close=lambda: closed.append(True), get_universe_symbols=lambda: [])
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **kw: SimpleNamespace(db=database, sync_universe=sync,
                                                                              collect_market_snapshot=collect))
    args = SimpleNamespace(symbols=None, interval=1, cycles=1, force=True)
    if raises:
        with pytest.raises(RuntimeError):
            brs.cmd_live(args)
    else:
        assert brs.cmd_live(args) == 1
    assert captures == [] and closed == [True]


def test_zero_value_broker_portfolio_renders_unknown_concentration(dashboard_db, capture):
    from data_collector.farabi import import_report
    from database.broker import BrokerRepository
    from database.db import DatabaseManager
    for row in capture['rows']:
        row[3], row[6], row[7] = '0', '0', '(+∞%) 0'
    capture['summary'].update(portfolio_value_rial=0, net_assets_rial=200, buy_total_rial=0, broker_pnl_rial=0,
                              broker_return_pct=None)
    with DatabaseManager(db_path=dashboard_db) as db:
        import_report(BrokerRepository(db), encoded(capture), 'synthetic.json')
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🏦 حساب و تطبیق فارابی').run()
    assert not app.exception
    assert any('وزن دو دارایی بزرگ: نامعلوم' in item.value for item in app.markdown)


def test_nonpositive_round_trip_cost_is_invalid_not_zero_return():
    engine = BacktestEngine(initial_capital=1000)
    engine._round_trips['آزمایش'] = dict(sold=1, bought=1, cost=10., receivable_cost=10., buy_gross=10.,
                                        proceeds=11., sell_gross=11.)
    with pytest.raises(ValueError, match='بهای'):
        engine._close_round_trip('آزمایش', '2024-01-01', '2024-01-02')
    assert not engine.closed_trades and 'آزمایش' in engine._round_trips


def test_random_summary_preserves_unknown_draws_and_reports_coverage():
    from research.dip_run import summarize_random_draws
    draw = dict(mean_trade=.01, mean_daily_cohort=.001, fill_rate=.5, win_rate=.6, target_rate=.5)
    result = summarize_random_draws([draw, {**draw, 'mean_trade': np.nan, 'mean_daily_cohort': np.nan}])
    assert np.isnan(result['mean_trade']) and np.isnan(result['mean_daily_cohort'])
    assert result['coverage']['mean_trade'] == dict(known=1, unknown=1, total=2)
    assert result['known_draw_diagnostics']['mean_trade'] == .01
    assert result['fill_rate'] == .5


def test_dip_conditional_entry_and_exit_statistics_disclose_their_denominators():
    from research.dip_run import evaluate
    picks = pd.DataFrame({'date':['2024-01-01']*3,'symbol':['A','B','C']})
    trades = picks.assign(dip_status=['complete','not_filled','invalid'],entry_filled=[1.,0.,np.nan],
                          net_ret=[.01,np.nan,np.nan],exit_kind=['target',None,None],days_held=[1.,np.nan,np.nan])
    result = evaluate(trades,picks,pd.Index(['2024-01-01']),3,1,pd.Series({'2024-01-01':.6}))
    assert result['picks']==3 and result['entry_known_picks']==2 and result['entry_unknown_picks']==1
    assert result['trades']==1 and result['fill_rate']==.5 and result['mean_trade']==.01
    assert np.isnan(result['mean_daily_cohort']) and result['cohort_unknown_days']==1


def test_initial_reference_scale_does_not_create_a_flow_breakout():
    from core.flow_filters import flow_metrics
    from tests.test_flow_filters import history
    prices, clients = history(65)
    original = flow_metrics(prices, clients)['breakout_60d']
    prices.loc[0, 'ref_price_raw'] /= 100
    pd.testing.assert_series_equal(flow_metrics(prices, clients)['breakout_60d'], original)
