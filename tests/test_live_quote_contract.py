"""قرارداد مظنه: داده آزمون فقط در حافظه و SQLite موقت استفاده می‌شود."""

from datetime import timedelta

import numpy as np
import pytest

from core.account_risk import intraday_account_state
from core.paper import quote_fill
from core.risk import RiskProfile, finite_number
from data_collector.validator import DataValidator
from tests.test_audit_followup import live_book
from tests.test_dashboard import dashboard_db as dashboard_db
from tests.test_workbench import quote


def valid_book(**changes):
    at, book = live_book()
    book.update(source='tsetmc', price_unit='Rial')
    book.update(changes)
    return at, book


BAD_METADATA = [dict(source=None), dict(source='other'), dict(price_unit=None), dict(price_unit='Toman')]


@pytest.mark.parametrize('changes', BAD_METADATA)
def test_unverified_source_or_unit_blocks_entry_and_account_marks(changes):
    at, book = valid_book(**changes)
    calculation = quote(intraday=True, intraday_snapshot=book, at=at, stop_rial=980, target_rial=1020)
    assert calculation['quantity'] == 0 and calculation['blockers']
    result = account(book, at)
    assert not result['current'] and result['state']['equity_rial'] is None
    assert result['state']['positions']['آزمون'].get('liquidation_value_rial') is None


def account(book, at):
    return intraday_account_state(
        [dict(symbol_fa='آزمون', quantity=10, price_rial=1000, fee_rial=0, side='buy')],
        RiskProfile(100000, 10000), [], {'آزمون': book}, at=at)


@pytest.mark.parametrize('field', ['captured_at', 'provider_timestamp'])
def test_future_quote_within_clock_skew_tolerance_cannot_value_or_size_a_decision(field):
    at, book = valid_book()
    book[field] = (at + timedelta(seconds=1)).isoformat()
    calculation = quote(intraday=True, intraday_snapshot=book, at=at, stop_rial=980, target_rial=1020)
    assert calculation['quantity'] == 0 and calculation['blockers']
    result = account(book, at)
    assert not result['current'] and result['state']['equity_rial'] is None


@pytest.mark.parametrize('field', ['volume', 'ask_vol_1', 'ask_count_1', 'bid_vol_1', 'bid_count_1'])
def test_fractional_shares_and_order_counts_cannot_supply_entry_capacity(field):
    at, book = valid_book(**{field: 100.5})
    calculation = quote(intraday=True, intraday_snapshot=book, at=at, stop_rial=980, target_rial=1020)
    assert calculation['quantity'] == 0 and calculation['blockers']


@pytest.mark.parametrize('field', ['bid_vol_1', 'bid_count_1'])
def test_fractional_exit_depth_or_count_cannot_mark_open_holdings(field):
    at, book = valid_book(**{field: .5})
    result = account(book, at)
    assert not result['current'] and result['state']['equity_rial'] is None


@pytest.mark.parametrize('field', ['volume', 'trade_count', 'real_buy_count', 'real_buy_volume',
                                  'ask_vol_1', 'ask_count_1', 'bid_vol_1', 'bid_count_1'])
def test_ingestion_rejects_fractional_quantity_fields(field):
    at, book = valid_book(**{field: .5})
    ok, reasons = DataValidator.validate_snapshot(book)
    assert not ok and reasons


@pytest.mark.parametrize('value', [np.bool_(True), np.bool_(False)])
def test_numpy_boolean_is_never_a_price_volume_or_cash_amount(value):
    with pytest.raises(ValueError):
        finite_number(value, positive=False)
    at, book = valid_book(volume=value)
    assert not DataValidator.validate_snapshot(book)[0]


def test_valid_rial_quote_retains_existing_capacity_and_account_arithmetic():
    at, book = valid_book()
    calculation = quote(intraday=True, intraday_snapshot=book, at=at, stop_rial=980, target_rial=1020)
    assert calculation['quantity'] == 7 and not calculation['blockers']
    result = account(book, at)
    assert result['current'] and result['state']['equity_rial'] == pytest.approx(90000 + 10 * 999 * .9912)


def test_observed_paper_fill_rejects_fractional_daily_volume_from_older_snapshot():
    at, book = valid_book(volume=1000.5, low_price=970, high_price=1030)
    with pytest.raises(ValueError, match='حجم'):
        quote_fill(book, side='buy', quantity=1, at=at, decision_at=(at-timedelta(seconds=1)).isoformat(),
                   protocol=dict(slippage_bps=0, participation=.05))


@pytest.mark.parametrize('unit', ['Rial', 'Toman'])
def test_real_workbench_ui_does_not_offer_plan_for_unverified_market_unit(dashboard_db, monkeypatch, unit):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from database.workbench import WorkbenchRepository
    from tests.test_dashboard import DASHBOARD

    at, book = valid_book(price_unit=unit)
    for module in ('core.screening', 'core.risk', 'dashboard.workbench'):
        monkeypatch.setattr(f'{module}.now_tehran', lambda: at)
    with DatabaseManager(db_path=dashboard_db) as db:
        WorkbenchRepository(db).save_profile(RiskProfile(1_000_000, 100_000))
        sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
        db.insert_daily_price(sid, '2026-10-13', 1000, 1050, 950, 1000, volume=1000, value=1_000_000,
                              close_final_raw=1000, ref_price_raw=1000)
        assert db.insert_snapshot(sid, book)
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🛡️ برنامه و دفتر معاملات').run()
    for label, value in {'قیمت سناریوی خروج با زیان (تومان)': 98., 'قیمت هدف خودت (تومان)': 102.,
                         'بودجه زیان همین معامله (تومان)': 1000}.items():
        next(widget for widget in app.number_input if widget.label == label).set_value(value)
    app.run()
    assert not app.exception
    assert any(button.label == 'ثبت برنامه دستی' for button in app.button) is (unit == 'Rial')
    if unit != 'Rial':
        assert any('واحد ریال' in warning.value for warning in app.warning)
