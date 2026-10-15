"""بازآفرینی ادعاها با داده ساختگی آزمون؛ هیچ داده واقعی حساب در مخزن نیست."""

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core.symbol_review import review_history, volume_comparability
from core.timeutils import TEHRAN_TZ
from data_collector.codal import CodalProvider
from data_collector.scheduler import DataCollector
from data_collector.validator import DataValidator
from database.symbol_review import load_symbol_review
from tests.conftest import FakeSession
from tests.test_codal_ai import RAW_LETTER
from tests.test_flow_filters import history


@pytest.mark.parametrize('factor', [.25, .9, 1.2])
def test_raw_volume_unknown_across_reference_gap_and_recovers_at_exact_window_boundary(factor):
    from core.flow_filters import flow_metrics
    p, c = history(100)
    raw_columns = [col for col in p if col.endswith('_raw')]
    p.loc[50:, raw_columns] *= factor
    p.loc[50:, 'volume'] /= factor
    metrics = flow_metrics(p, c)
    for n in (20, 30):
        assert metrics.loc[49, f'volume_ratio_{n}d'] == 1
        assert metrics.loc[50:50+n-1, f'volume_ratio_{n}d'].isna().all()
        assert not metrics.loc[50:50+n-1, f'volume_comparable_{n}d'].any()
        assert metrics.loc[50+n, f'volume_ratio_{n}d'] == pytest.approx(1)
    assert p.loc[50, 'volume'] == 100_000_000/factor  # داده خام دستکاری نشد.


def test_unknown_reference_never_becomes_comparable_and_future_cannot_repair_past():
    from core.flow_filters import flow_metrics
    p, c = history(100)
    p.loc[50, 'ref_price_raw'] = np.nan
    expected = flow_metrics(p.iloc[:61], c.iloc[:61])
    assert pd.isna(expected.iloc[-1].volume_ratio_20d)
    p.loc[61:, 'ref_price_raw'] = 999_999
    pd.testing.assert_frame_equal(flow_metrics(p, c).iloc[:61], expected)


def test_comparability_is_separate_per_symbol_and_full_window_is_required():
    p, _ = history(45)
    other = p.assign(symbol='دیگر')
    p.loc[40:, 'ref_price_raw'] = 500
    both = pd.concat([p, other], ignore_index=True)
    comparable = volume_comparability(both, 30)
    assert not comparable.iloc[44] and comparable.iloc[-1]
    with pytest.raises(ValueError):
        volume_comparability(both, True)


def test_reopening_anomaly_is_retained_as_review_flag_and_never_becomes_typed_action():
    p, c = history(45)
    p.loc[44, ['ref_price_raw', 'open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'volume']] = [500, 600, 600, 600, 600, 600, 2_000_000_000]
    report = review_history(p, c)
    assert report['price_anomalies'] == 1 and report['volume_anomalies'] == 1
    assert report['anomaly_rows'] == 1 and len(report['reference_gaps']) == 1
    assert not report['event_type_inferred'] and not report['signals_enabled']
    assert DataValidator.quant_anomaly_flags(p).iloc[-1].all()
    assert not report['volume_comparable_20d']


def test_exact_symbol_precedes_company_alias_and_duplicate_company_never_selects_arbitrarily(db):
    sid = db.upsert_symbol(dict(symbol_fa='کیان', name_fa='کیان ایران', ins_code='777', tse_code='IRO_TEST_1'))
    assert db.get_symbol('كيان ايران')['id'] == sid
    assert db.get_symbol('777')['id'] == sid
    assert db.get_symbol('IRO_TEST_1')['id'] == sid
    db.upsert_symbol(dict(symbol_fa='دیگر', name_fa='کیان ایران', ins_code='888'))
    assert db.get_symbol('کیان ایران') is None
    assert db.get_symbol('کیان')['id'] == sid
    assert db.get_symbol('') is None


def test_read_only_review_separates_market_archive_gap_missing_symbol_and_old_snapshot(db):
    p, c = history(3)
    for i in range(20):
        sid = db.upsert_symbol(dict(symbol_fa='آزمون'+chr(65+i), ins_code=str(i), instrument_type_code=300))
        db.upsert_daily_prices_df(sid, p.drop(columns='symbol'))
    sid = db.upsert_symbol(dict(symbol_fa='کیان', name_fa='کیان ایران', ins_code='900', instrument_type_code=303))
    db.upsert_daily_prices_df(sid, p.iloc[[0, 2]].drop(columns='symbol'))
    db.upsert_client_types_df(sid, c.drop(columns='symbol'))
    stamp = '2024-01-01T12:00:00+03:30'
    db.insert_snapshot(sid, dict(captured_at=stamp, provider_timestamp=stamp, sell_queue_volume=100_000))
    before = db.conn.total_changes
    report = load_symbol_review(db, 'کیان ایران', at=datetime(2024, 1, 4, 12, tzinfo=TEHRAN_TZ))
    assert db.conn.total_changes == before and not db.conn.in_transaction
    assert not report['snapshot_current'] and report['snapshot']['sell_queue_volume'] == 100_000
    assert report['extra_client_dates'] == [p.date.iloc[1]]
    assert report['observed_market_sessions_without_symbol'] == 1
    assert report['longest_calendar_gap']['observed_market_sessions_without_symbol'] == 1
    assert report['recorded_action_rows'] == 0 and not report['economic_actions_reconciled']


def test_block_boards_cannot_invent_an_observed_ordinary_equity_session(db):
    p, _ = history(3)
    for i in range(20):
        sid = db.upsert_symbol(dict(symbol_fa='بلوکی'+chr(65+i)+'2', ins_code=str(i), instrument_type_code=300))
        db.upsert_daily_prices_df(sid, p.drop(columns='symbol'))
    sid = db.upsert_symbol(dict(symbol_fa='کیان', ins_code='900', instrument_type_code=303))
    db.upsert_daily_prices_df(sid, p.iloc[[0, 2]].drop(columns='symbol'))
    report = load_symbol_review(db, 'کیان')
    assert report['observed_market_sessions_without_symbol'] == 0


@pytest.mark.parametrize('partial', [False, True])
def test_closed_trade_discloses_actual_buy_quantity_and_reconciles_adjusted_sale_basis(partial):
    engine = BacktestEngine(initial_capital=100_000, default_slippage_bps=0)
    engine.execute_buy('آزمون', 1000, 10, '2024-01-01')
    if partial:
        engine.execute_sell('آزمون', 1000, 3, '2024-01-02')
    engine.apply_corporate_action('آزمون', 500, '2024-01-03')
    engine.execute_sell('آزمون', 550, engine.positions['آزمون'].quantity, '2024-01-04')
    trade = engine.closed_trades[0]
    assert trade.entry_price == 1000 and trade.entry_quantity == 10
    assert trade.quantity == (17 if partial else 20)
    assert trade.allocated_entry_price_rial*trade.quantity == pytest.approx(trade.allocated_entry_notional_rial)
    assert trade.exit_notional_rial-trade.allocated_entry_notional_rial == pytest.approx(trade.gross_pnl)
    assert trade.exit_notional_rial/trade.quantity == pytest.approx(trade.exit_price, abs=.005)
    assert trade.gross_pnl-trade.commissions == pytest.approx(trade.net_pnl)
    assert trade.corporate_action_events == 1


def test_codal_partial_failure_retains_valid_letters_but_cannot_report_success(db):
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code='1', instrument_type_code=300))
    provider = CodalProvider()
    provider.get_letters = lambda page, **_: [CodalProvider.normalize_letter(RAW_LETTER)] if page == 1 else None
    result = DataCollector(db=db, codal=provider).collect_announcements(pages=2, symbol_fa='فولاد')
    assert result['status'] == 'partial' and result['new_announcements'] == 1
    assert result['pagination']['failed_page'] == 2 and not result['coverage_proven']
    assert not result['pagination']['reached_end']


def test_codal_empty_success_is_different_from_schema_error_or_all_malformed_records():
    for payload in ({}, {'Letters': {}}, {'Letters': [{'bad': 1}]}):
        provider = CodalProvider(session=FakeSession({'search.codal.ir': payload}))
        assert provider.get_latest_report(pages=1)['failed_page'] == 1
    provider = CodalProvider(session=FakeSession({'search.codal.ir': {'Letters': []}}))
    report = provider.get_latest_report(pages=2)
    assert report['reached_end'] and report['failed_page'] is None and report['pages_fetched'] == 1


def test_codal_wrong_symbol_is_rejected_and_page_limit_is_explicit():
    provider = CodalProvider(session=FakeSession({'search.codal.ir': {'Letters': [RAW_LETTER]}}))
    wrong = provider.get_latest_report(pages=1, symbol_fa='کیان')
    assert wrong['failed_page'] == 1 and not wrong['letters']
    bounded = provider.get_latest_report(pages=1, symbol_fa='فولاد')
    assert bounded['page_limit_reached'] and not bounded['reached_end']


@pytest.mark.parametrize('explicit', [True, False])
def test_selected_update_collects_codal_for_each_selected_symbol(explicit):
    requested = []
    success = dict(status='success', errors=[])
    collector = DataCollector.__new__(DataCollector)
    collector.symbols = None if explicit else ['کیان', 'دیگر']
    collector.collect_history = lambda *a, **k: success
    collector.collect_client_types = lambda *a, **k: success
    collector.collect_announcements = lambda pages, symbol_fa: requested.append(symbol_fa) or success
    collector.db = SimpleNamespace(log_collection=lambda **_: None)
    result = collector.update_all(symbols=['کیان', 'کیان', 'دیگر'] if explicit else None)
    assert requested == ['کیان', 'دیگر']
    assert result['codal']['status'] == 'success' and len(result['codal']['per_symbol']) == 2
