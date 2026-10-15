"""Reproductions of the external review, using synthetic data and temporary databases."""

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from backtest.replay import MarketReplayEngine
from core.screening import current_daily_row, expected_completed_session
from core.timeutils import TEHRAN_TZ, is_market_open


@pytest.mark.parametrize('partial_exit', [False, True])
def test_fractional_action_cost_reconciles_closed_shares_and_non_cash_claim(partial_exit):
    engine = BacktestEngine(initial_capital=100_000, default_slippage_bps=0)
    engine.execute_buy('آزمون', 1000, 7 if partial_exit else 2, '2024-01-01')
    if partial_exit:
        engine.execute_sell('آزمون', 1000, 2, '2024-01-02')
    position = engine.positions['آزمون']
    original_cost, cash = position.total_cost, engine.cash
    engine.last_prices['آزمون'] = 1000
    engine.apply_corporate_action('آزمون', 600, '2024-01-03')
    exact = 5 / 3 * (5 if partial_exit else 2)
    fractional_cost = original_cost * (exact % 1) / exact
    assert position.total_cost == pytest.approx(original_cost - fractional_cost)
    assert engine.cash == cash  # A synthetic fractional claim is never spendable cash.
    engine.execute_sell('آزمون', 600, position.quantity, '2024-01-04')
    closed = engine.closed_trades[0]
    claim_pnl = engine.corporate_action_receivable - engine.corporate_action_receivable_cost
    assert engine.mark_to_market() - engine.initial_capital == pytest.approx(closed.net_pnl + claim_pnl, abs=.011)
    assert position.realized_pnl == pytest.approx(closed.net_pnl, abs=.011)
    assert closed.gross_pnl - closed.commissions == pytest.approx(closed.net_pnl, abs=.011)
    metrics = engine.calculate_metrics()
    assert metrics.corporate_action_receivable_cost == pytest.approx(fractional_cost, abs=.01)


def test_action_that_removes_all_whole_shares_does_not_reuse_old_cost_on_reentry():
    engine = BacktestEngine(initial_capital=100_000, default_slippage_bps=0)
    engine.execute_buy('آزمون', 500, 1, '2024-01-01')
    cost = engine.positions['آزمون'].total_cost
    engine.apply_corporate_action('آزمون', 1000, '2024-01-02')
    assert engine.positions['آزمون'].quantity == 0
    assert engine.positions['آزمون'].total_cost == 0
    assert engine.corporate_action_receivable_cost == cost
    assert not engine.closed_trades  # A receivable is not a cash sale or a winning trade.
    engine.execute_buy('آزمون', 1000, 1, '2024-01-03')
    engine.execute_sell('آزمون', 1100, 1, '2024-01-04')
    assert engine.closed_trades[0].entry_date == '2024-01-03'
    assert engine.closed_trades[0].net_pnl == pytest.approx(1100 * .9912 - 1000 * 1.003712, abs=.005)


def test_replay_is_read_only_and_closes_even_on_callback_failure(db):
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    db.insert_snapshot(sid, dict(captured_at='2024-01-01T09:00:00+03:30', last_price=1000))
    replay = MarketReplayEngine(str(db.db_path))
    with pytest.raises(RuntimeError, match='callback'):
        with replay:
            assert replay.db.read_only
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                replay.db.conn.execute('DELETE FROM snapshots')
            replay.run_replay(lambda *_: (_ for _ in ()).throw(RuntimeError('callback')))
    assert replay.db.conn is None
    replay.close()
    with pytest.raises(sqlite3.OperationalError):
        MarketReplayEngine(str(db.db_path.parent / 'missing.db'))


def test_idle_replay_reader_does_not_by_itself_block_wal_checkpoint(db):
    with MarketReplayEngine(str(db.db_path)) as replay:
        assert replay.get_available_replay_dates() == []
        busy, _, _ = db.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert busy == 0


def test_recorded_holiday_is_used_without_promoting_arbitrary_stale_data(db):
    at = datetime(2026, 10, 13, 10, tzinfo=TEHRAN_TZ)
    # Deliberately synthetic calendar exception; this is not an actual holiday assertion.
    db.insert_market_calendar('2026-10-12', False, 'synthetic test closure')
    calendar = db.get_market_calendar()
    assert expected_completed_session(at, calendar=calendar) == '2026-10-11'
    assert current_daily_row('2026-10-11', at, calendar=calendar)
    assert not current_daily_row('2026-10-10', at, calendar=calendar)
    assert not current_daily_row('2026-10-11', at)  # Unknown closure remains conservatively blocked.
    assert not is_market_open(at.replace(day=12), calendar=calendar)
    assert is_market_open(at, calendar=calendar)
    assert expected_completed_session(at.replace(day=12, hour=16), calendar=calendar) == '2026-10-11'


def test_session_watch_is_skipped_before_finalization_without_calling_provider(db, monkeypatch):
    from data_collector import scheduler
    clock = datetime(2026, 10, 11, 10, tzinfo=TEHRAN_TZ)
    monkeypatch.setattr(scheduler, 'now_tehran', lambda: clock)
    provider = SimpleNamespace(get_session_candles=lambda: pytest.fail('not required before finalization'))
    collector = scheduler.DataCollector(db=db, provider=provider, codal=object())
    assert collector.collect_session_candles()['status'] == 'skipped'
    db.insert_market_calendar(clock.date().isoformat(), False, 'synthetic closure')
    clock = clock.replace(hour=16)
    assert collector.collect_session_candles()['status'] == 'skipped'
    assert collector.run_collection_cycle()['status'] == 'skipped'


def test_optional_watch_failure_does_not_hide_or_prevent_valid_history_features(monkeypatch):
    import brs
    from data_collector import scheduler
    reports = {key: dict(status='success') for key in ('universe', 'history', 'client_types', 'codal')}
    reports['session_candles'] = dict(status='failed')
    calls = []
    collector = SimpleNamespace(db=SimpleNamespace(close=lambda: calls.append('close')),
                                update_all=lambda **_: reports)
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **_: collector)
    monkeypatch.setattr(brs, '_build_features', lambda _: calls.append('features'))
    args = SimpleNamespace(symbols='', full=False, no_features=False)
    assert brs.cmd_update(args) == 1  # Failure is still reported to the caller.
    assert calls == ['features', 'close']
    calls.clear()
    reports['client_types']['status'] = 'partial'
    assert brs.cmd_update(args) == 1
    assert calls == ['close']


def test_flow_all_history_and_tail_keep_the_same_joined_values(db):
    from database.flow_data import load_flow_inputs
    for symbol, kind in [('آزمون', 300), ('نمونه', 303), ('صندوق', 305)]:
        sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=symbol, instrument_type_code=kind))
        for day in ('2024-01-01', '2024-01-02', '2024-01-03'):
            db.insert_daily_price(sid, day, 1000, 1010, 990, 1000, volume=10,
                                  value=10_000, close_final_raw=1000, ref_price_raw=1000)
        db.upsert_client_types_df(sid, pd.DataFrame([dict(date='2024-01-01', buy_i_value=1234)]))
    queries = []
    db.conn.set_trace_callback(queries.append)
    prices, clients = load_flow_inputs(db, before='2024-01-03')
    assert not any('ROW_NUMBER' in q.upper() for q in queries)
    short_prices, short_clients = load_flow_inputs(db, before='2024-01-03', tail_per_symbol=1)
    for full, short in [(prices, short_prices), (clients, short_clients)]:
        # SQLite infers object dtype for an all-NULL column in the smaller result.
        expected = full.groupby('symbol', sort=False).tail(1).reset_index(drop=True)
        for column in full.columns.difference(['symbol', 'date']):
            expected[column] = pd.to_numeric(expected[column])
            short[column] = pd.to_numeric(short[column])
        pd.testing.assert_frame_equal(expected, short, check_dtype=False)
    assert len(prices) == 4
    assert clients.buy_i_value.isna().sum() == 2


def test_cached_filter_ranking_is_identical_and_rejects_misaligned_keys(monkeypatch):
    from core.flow_filters import filter_diagnostics, flow_metrics
    from research import flow_run
    from tests.test_flow_filters import history
    prices, clients = history(80)
    metrics = flow_metrics(prices, clients)
    diagnostics = filter_diagnostics(metrics, 'retail_acceleration')
    expected = flow_run.ranked_matches(metrics, 'retail_acceleration', 5)
    monkeypatch.setattr(flow_run, 'filter_diagnostics', lambda *_: pytest.fail('cached diagnosis must be reused'))
    pd.testing.assert_frame_equal(expected, flow_run._rank_diagnostics(metrics, diagnostics, 5))
    with pytest.raises(ValueError, match='ترتیب'):
        flow_run._rank_diagnostics(metrics, diagnostics.iloc[::-1], 5)


def test_recorded_closure_blocks_live_account_and_entry_even_with_fresh_quotes():
    from core.account_risk import intraday_account_state
    from core.risk import RiskProfile
    from tests.test_audit_followup import live_book, quote
    clock, book = live_book()
    calendar = {clock.date().isoformat(): False}
    calculation = quote(intraday=True, intraday_snapshot=book, at=clock, calendar=calendar,
                        stop_rial=980, target_rial=1020)
    assert calculation['quantity'] == 0
    assert any('ساعات معاملات' in value for value in calculation['blockers'])
    account = intraday_account_state([], RiskProfile(100_000, 10_000), [], {}, at=clock, calendar=calendar)
    assert not account['current']
    assert account['evidence']['calendar_exceptions'] == calendar


def test_fractional_actions_after_partial_sales_and_additional_buys_keep_all_costs():
    engine = BacktestEngine(initial_capital=100_000, default_slippage_bps=0)
    engine.execute_buy('آزمون', 1000, 7, '2024-01-01')
    engine.execute_sell('آزمون', 1000, 2, '2024-01-02')
    engine.apply_corporate_action('آزمون', 600, '2024-01-03')
    engine.execute_buy('آزمون', 600, 1, '2024-01-04')
    engine.apply_corporate_action('آزمون', 700, '2024-01-05')
    quantity = engine.positions['آزمون'].quantity
    engine.execute_sell('آزمون', 750, quantity, '2024-01-06')
    claim_pnl = engine.corporate_action_receivable - engine.corporate_action_receivable_cost
    assert len(engine.closed_trades) == 1
    assert engine.mark_to_market() - engine.initial_capital == pytest.approx(engine.closed_trades[0].net_pnl + claim_pnl, abs=.011)


def test_update_closes_database_when_feature_build_raises(monkeypatch):
    import brs
    from data_collector import scheduler
    calls = []
    collector = SimpleNamespace(db=SimpleNamespace(close=lambda: calls.append('close')),
                                update_all=lambda **_: dict(history=dict(status='success')))
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **_: collector)
    monkeypatch.setattr(brs, '_build_features', lambda _: (_ for _ in ()).throw(RuntimeError('feature failure')))
    with pytest.raises(RuntimeError, match='feature failure'):
        brs.cmd_update(SimpleNamespace(symbols='', full=False, no_features=False))
    assert calls == ['close']
