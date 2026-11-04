"""Fictional market inputs stay in tests; no return is evidence of profitability."""

from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from backtest.bonus_shares import BonusCapitalization, BonusRelease
from backtest.cash_dividends import CashDividend, CashDividendBook, DividendPayment
from backtest.corporate_actions import (
    BonusDividendBook,
    DatedCashDividendBook,
    replay_corporate_action_portfolio,
)
from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from research.anniversary import replay_sleeve
from tests.test_portfolio import history

SHA = 'a'*64
AVAILABLE = '2026-10-23T10:00:00+03:30'


@pytest.fixture(autouse=True)
def zero_fees(monkeypatch):
    monkeypatch.setitem(settings.FEES, 'buy', 0.)
    monkeypatch.setitem(settings.FEES, 'sell', 0.)


def segment(frame, start, price):
    mask = frame.date.ge(start)
    frame.loc[mask, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = float(price)
    frame.loc[mask, 'low_raw'], frame.loc[mask, 'high_raw'] = price-1., price+1.
    frame.loc[mask, 'value'] = frame.loc[mask, 'volume']*price


def example(*, cash_first=False):
    frame, days = history(12, ('a',))
    frame['volume'] = 10000.
    frame['value'] = frame.volume*100.
    bonus_day, cash_day = (7, 3) if cash_first else (3, 8)
    if cash_first:
        segment(frame, days[cash_day], 90)
        segment(frame, days[bonus_day], 72)
        before, after = '90', '72'
    else:
        segment(frame, days[bonus_day], 80)
        segment(frame, days[cash_day], 70)
        before, after = '100', '80'
    bonus = BonusCapitalization('BONUS', 'a', days[bonus_day-1], days[bonus_day],
        1, 4, days[bonus_day-1], before, after, SHA, AVAILABLE)
    dividend = CashDividend('CASH', 'a', days[cash_day-1], days[cash_day], '10', SHA)
    release = BonusRelease('CREDIT', bonus.event_id, days[bonus_day+1], 1, 1, SHA, AVAILABLE)
    payment = DividendPayment('PAID', dividend.event_id, days[cash_day+1], '10', SHA)
    return frame, days, bonus, dividend, release, payment


def picks(days):
    return pd.DataFrame([dict(symbol='a', date=days[0], rank=1)])


def cfg(days, **changes):
    return replace(PortfolioConfig(10000., horizon=len(days)-2, max_positions=1, slippage_bps=0., max_loss_pct=.9), **changes)


def combined(bonus, dividend, release=None, payment=None, *, credit_exit_session=None):
    return BonusDividendBook([bonus], [] if release is None else [release],
        dividends=CashDividendBook([dividend], [] if payment is None else [payment]),
        credit_exit_session=credit_exit_session)


@pytest.mark.parametrize('cash_first', [False, True])
def test_cash_and_bonus_are_separate_and_do_not_invent_profit(cash_first):
    frame, days, bonus, dividend, release, payment = example(cash_first=cash_first)
    book = combined(bonus, dividend, release, payment, credit_exit_session=days[-1])
    result = replay_portfolio(frame, picks(days), days, cfg(days), bonus_shares=book)
    assert result.metrics['status'] == 'unvalued_corporate_action_path'
    assert result.metrics['total_return'] == pytest.approx(0.)
    assert result.metrics['realized_net_rial'] == pytest.approx(0.)
    assert result.metrics['open_positions'] == 0
    assert result.metrics['cash_dividend_income_rial'] == (1000 if cash_first else 1250)
    assert result.metrics['cash_dividend_receivable_face_rial'] == 0
    assert result.metrics['fractional_exposures'] == []
    assert result.metrics['max_drawdown'] is None  # Unknown fair values during settlement.
    sales = result.executions.query("side == 'sell'")
    assert set(sales.date) == {days[-1]} and sales.quantity.sum() == 125
    assert not result.metrics['accepted_for_signals'] and not result.metrics['profitability_proven']


def test_unpaid_cash_stays_unknown_after_bonus_is_fully_credited():
    frame, days, bonus, dividend, release, _ = example()
    result = replay_portfolio(frame, picks(days), days, cfg(days),
        bonus_shares=combined(bonus, dividend, release, credit_exit_session=days[-1]))
    assert result.metrics['pending_bonus_units']['numerator'] == 0
    assert result.metrics['cash_dividend_receivable_face_rial'] == 1250
    assert result.metrics['cash_dividend_income_rial'] == 0
    assert result.metrics['total_return'] is result.metrics['final_equity_rial'] is None
    assert result.metrics['noncash_receivable_rial'] is None
    assert result.equity.loc[result.equity.date.ge(dividend.ex_date), 'equity_rial'].isna().all()


def test_fees_are_charged_on_trades_only_and_cash_reconciles_after_both_events(monkeypatch):
    frame, days, bonus, dividend, release, payment = example()
    monkeypatch.setitem(settings.FEES, 'buy', .003712)
    monkeypatch.setitem(settings.FEES, 'sell', .0088)
    capital = 10037.12
    result = replay_portfolio(frame, picks(days), days, cfg(days, initial_capital_rial=capital),
        bonus_shares=combined(bonus, dividend, release, payment, credit_exit_session=days[-1]))
    assert result.executions.query("side == 'buy'").quantity.sum() == 100
    assert result.executions.query("side == 'sell'").quantity.sum() == 125
    assert result.metrics['fees_rial'] == pytest.approx(114.12)
    assert result.metrics['final_equity_rial'] == pytest.approx(capital-114.12)
    assert result.metrics['realized_net_rial'] == pytest.approx(-114.12)
    assert result.metrics['stock_realized_net_rial']+result.metrics['cash_dividend_income_rial'] == pytest.approx(-114.12)


def test_unvalued_claims_pause_risk_without_creating_a_permanent_loss_or_forced_sale():
    frame, days, bonus, dividend, release, payment = example()
    result = replay_portfolio(frame, picks(days), days, cfg(days, max_loss_pct=.1), loss_floor_exit=True,
        bonus_shares=combined(bonus, dividend, release, payment, credit_exit_session=days[-1]))
    assert result.equity.unvalued_rights_pause.any()
    assert not result.equity.risk_stopped.any() and not result.equity.risk_exit_requested.any()
    assert result.decisions.empty or 'risk_exit_trigger' not in set(result.decisions.status)
    assert set(result.executions.query("side == 'sell'").date) == {days[-1]}
    assert result.metrics['final_equity_rial'] == pytest.approx(10000)


def test_all_lots_of_one_issuer_share_a_single_fixed_dividend_entitlement():
    _, days = history(6, ('a',))
    dividend = CashDividend('CASH', 'a', days[1], days[2], '10', SHA)
    payment = DividendPayment('P', 'CASH', days[4], '10', SHA)
    book = BonusDividendBook([], dividends=CashDividendBook([dividend], [payment]))
    book.start(days)
    inventory = {'L1': dict(symbol='a', quantity=30., cost=3000.),
                 'L2': dict(symbol='a', quantity=20., cost=2000.)}
    paid = 0.
    for i, day in enumerate(days):
        row = dict(valid=True, mark=90. if i >= 2 else 100.,
                   previous_date=days[max(0, i-1)], previous_official=100. if i <= 2 else 90.,
                   ref_price_raw=90. if i >= 2 else 100.)
        book.begin_day(day, {'a': row}, inventory)
        if i == 3:
            inventory.clear()  # Selling after the snapshot does not erase the claim.
        _, cash, _ = book.end_day(day, inventory)
        paid += cash
    assert book.dividends.claims['CASH']['quantity'] == 50
    assert paid == 500 and book.dividends.outstanding_face_rial == 0


def test_dividend_cash_cannot_fund_a_buy_before_it_is_received_at_eod():
    frame, days = history(8, ('a', 'b'))
    is_b = frame.symbol.eq('b')
    frame.loc[is_b, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 5000.
    frame.loc[is_b, 'low_raw'], frame.loc[is_b, 'high_raw'], frame.loc[is_b, 'value'] = 4999., 5001., 5000000.
    is_a_after = frame.symbol.eq('a') & frame.date.ge(days[3])
    frame.loc[is_a_after, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 90.
    frame.loc[is_a_after, 'low_raw'], frame.loc[is_a_after, 'high_raw'], frame.loc[is_a_after, 'value'] = 89., 91., 90000.
    dividend = CashDividend('CASH', 'a', days[2], days[3], '10', SHA)
    cash = CashDividendBook([dividend], [DividendPayment('P', 'CASH', days[4], '10', SHA)])
    selected = pd.DataFrame([dict(symbol=s, date=days[i], rank=0) for i, s in [(0, 'a'), (3, 'b'), (4, 'b')]])
    result = replay_corporate_action_portfolio(frame, selected, days,
        cfg(days, initial_capital_rial=5000., horizon=2), events=[], dividends=cash)
    assert result.executions.query("side == 'buy' and symbol == 'b'").date.tolist() == [days[5]]
    assert ((result.decisions.date == days[4]) & (result.decisions.status == 'insufficient_capacity')).any()
    assert result.metrics['cash_dividend_income_rial'] == 500


def test_dividend_on_pending_bonus_shares_is_rejected_instead_of_guessing_quantity():
    frame, days, bonus, dividend, _, payment = example()
    result = replay_portfolio(frame, picks(days), days, cfg(days),
        bonus_shares=combined(bonus, dividend, payment=payment))
    assert result.metrics['status'] == 'invalid_data'
    assert result.metrics['total_return'] is None
    assert result.metrics['cash_dividend_income_rial'] == 0
    assert 'unknown_corporate_entitlement' in set(result.decisions.status)


def test_unknown_reference_gap_has_no_synthetic_fallback():
    frame, days, bonus, dividend, release, payment = example()
    frame.loc[frame.date.eq(dividend.ex_date), 'ref_price_raw'] = 69.
    result = replay_portfolio(frame, picks(days), days, cfg(days),
        bonus_shares=combined(bonus, dividend, release, payment))
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.metrics['cash_dividend_income_rial'] == 0
    assert result.metrics['fractional_exposures'] == []


def test_dividend_meeting_on_a_closed_day_uses_last_eod_inventory_and_explicit_previous_price():
    frame, _ = history(4, ('a',))
    days = ['2024-04-16', '2024-04-17', '2024-04-21', '2024-04-22']
    frame['date'] = days
    segment(frame, days[2], 90)
    dividend = CashDividend('CASH', 'a', '2024-04-18', days[2], '10', SHA)
    cash = DatedCashDividendBook([dividend], [DividendPayment('P', 'CASH', days[-1], '10', SHA)],
        previous_price_sessions={'CASH': days[1]})
    result = replay_corporate_action_portfolio(frame, picks(days), days, cfg(days), events=[], dividends=cash)
    assert result.assumptions['dividend_record_snapshot_sessions'] == {'CASH': days[1]}
    assert result.assumptions['source_dividends'][0]['record_date'] == '2024-04-18'
    assert result.metrics['cash_dividend_income_rial'] == 500
    assert result.executions.query("side == 'sell'").quantity.sum() == 50
    assert not cash.started  # The caller's source book was not consumed.


def test_halted_symbol_previous_price_is_not_confused_with_meeting_date():
    frame, days = history(8, ('a',))
    frame = frame.loc[~frame.date.isin(days[2:5])].copy()
    segment(frame, days[5], 90)
    event = CashDividend('CASH', 'a', days[4], days[5], '10', SHA)
    cash = DatedCashDividendBook([event], previous_price_sessions={'CASH': days[1]})
    result = replay_corporate_action_portfolio(frame, picks(days), days, cfg(days), events=[], dividends=cash)
    assert result.metrics['cash_dividend_receivable_face_rial'] == 500
    assert result.metrics['cash_dividend_income_rial'] == 0
    assert result.metrics['total_return'] is None
    assert result.metrics['status'] == 'unvalued_corporate_action_path'
    assert result.assumptions['source_previous_price_sessions'] == {'CASH': days[1]}


@pytest.mark.parametrize('mapping', [{}, {'other': '2024-01-01'}, {'CASH': True}, {'CASH': '2024-01-04'}])
def test_missing_or_invalid_explicit_previous_price_dates_are_not_guessed(mapping):
    event = CashDividend('CASH', 'a', '2024-01-02', '2024-01-05', '10', SHA)
    with pytest.raises(ValueError):
        DatedCashDividendBook([event], previous_price_sessions=mapping)


@pytest.mark.parametrize('days', [[], ['2024-01-02', '2024-01-01'], ['2024-01-01']*2, ['2024-01-01', 'bad']])
def test_invalid_calendar_does_not_start_a_partial_combined_book(days):
    _, _, bonus, dividend, _, _ = example()
    book = combined(bonus, dividend)
    with pytest.raises(ValueError):
        book.start(days)
    assert not book.started and not book.dividends.started


def test_same_day_actions_duplicate_ids_and_used_cash_book_are_rejected():
    _, _, bonus, dividend, _, _ = example()
    for event in [replace(dividend, ex_date=bonus.ex_date, record_date=bonus.record_date),
                  replace(dividend, event_id=bonus.event_id)]:
        with pytest.raises(ValueError):
            combined(bonus, event)
    cash = CashDividendBook([])
    cash.start(['2024-01-01'])
    with pytest.raises(ValueError):
        BonusDividendBook([], dividends=cash)


def test_begin_failure_rolls_back_claims_and_allocated_position_basis():
    _, days, bonus, dividend, _, _ = example()
    book = combined(bonus, dividend)
    book.start(days)
    inventory = {'L': dict(symbol='a', quantity=100., cost=10000.)}
    for day in days[:3]:
        book.begin_day(day, {}, inventory)
        book.end_day(day, inventory)
    before = deepcopy(book.__dict__)
    old_inventory = deepcopy(inventory)
    bad = dict(valid=True, mark=80., previous_date=days[2], previous_official=100., ref_price_raw=79.)
    with pytest.raises(ValueError):
        book.begin_day(days[3], {'a': bad}, inventory)
    assert inventory == old_inventory
    assert book.eligible == before['eligible'] and book.claims == before['claims']
    assert book.last_begin == before['last_begin']
    assert book.dividends.last_begin_day == before['dividends'].last_begin_day
    assert book.ledger == before['ledger']


def test_eod_failure_rolls_back_an_already_due_dividend_payment():
    _, days, bonus, dividend, _, payment = example(cash_first=True)
    book = combined(bonus, dividend, payment=payment)
    book.start(days)
    inventory = {'L': dict(symbol='a', quantity=100., cost=10000.)}
    for i, day in enumerate(days[:4]):
        row = dict(valid=True, mark=90. if i >= 3 else 100.,
                   previous_date=days[max(0, i-1)], previous_official=100., ref_price_raw=90. if i == 3 else 100.)
        book.begin_day(day, {'a': row}, inventory)
        book.end_day(day, inventory)
    book.begin_day(days[4], {}, inventory)
    before = deepcopy(book.__dict__)
    original = book.dividends.end_day

    def reject_after_cash(*args):
        original(*args)
        raise ValueError('rejected after payment')

    book.dividends.end_day = reject_after_cash
    with pytest.raises(ValueError):
        book.end_day(days[4], inventory)
    assert book.dividends.total_paid_rial == before['dividends'].total_paid_rial == 0
    assert book.dividends.claims == before['dividends'].claims
    assert book.dividends.ledger == before['dividends'].ledger


def test_annual_accounting_holds_credited_shares_until_the_same_terminal_and_preserves_inputs():
    frame, days, bonus, dividend, release, payment = example()
    source = combined(bonus, dividend, release, payment)
    selected, curve, events, _ = replay_sleeve(frame, symbol='a', decision=days[0], calendar=days,
        initial_capital_rial=10000., fees=dict(settings.FEES), slippage_bps=0., corporate_actions=source)
    assert set(events.query("side == 'sell'").date) == {days[-1]}
    assert selected['net_model_return'] == pytest.approx(0.)
    assert selected['corporate_accounting']['cash_dividend_income_rial'] == 1250
    assert selected['engine_assumptions']['credited_share_exit_session'] == days[-1]
    assert curve.equity_rial.isna().any()
    assert not source.started and source.credit_exit_session is None


def test_annual_accounting_never_reports_unknown_unpaid_dividend_as_zero_return():
    frame, days, bonus, dividend, release, _ = example()
    source = combined(bonus, dividend, release)
    selected, _, _, _ = replay_sleeve(frame, symbol='a', decision=days[0], calendar=days,
        initial_capital_rial=10000., fees=dict(settings.FEES), slippage_bps=0., corporate_actions=source)
    assert selected['net_model_return'] is selected['model_final_value_rial'] is None
    assert not selected['economic_profit_verified'] and not selected['fully_liquidated']


def test_annual_accounting_does_not_accept_a_different_credit_exit_session():
    frame, days, bonus, dividend, release, payment = example()
    source = combined(bonus, dividend, release, payment, credit_exit_session=days[6])
    with pytest.raises(ValueError, match='پایان'):
        replay_sleeve(frame, symbol='a', decision=days[0], calendar=days, initial_capital_rial=10000.,
                      fees=dict(settings.FEES), slippage_bps=0., corporate_actions=source)
