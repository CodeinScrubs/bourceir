"""Fictional prices and holder actions stay in tests; accounting is not profitability evidence."""

from dataclasses import replace
from fractions import Fraction

import pandas as pd
import pytest

from backtest.cash_dividends import CashDividend, CashDividendBook, DividendPayment
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.subscription_rights import (
    DividendCashReceipt,
    RightsCredit,
    RightsDisposal,
    RightsExercise,
    RightsIssue,
    RightsOffset,
    RightsPayment,
    SubscriptionRightsBook,
    replay_subscription_portfolio,
)
from config import settings
from tests.test_portfolio import history

PROOF = dict(evidence_sha256='a'*64, evidence_available_at='2026-10-22T10:00:00+03:30')


@pytest.fixture(autouse=True)
def no_trade_fees(monkeypatch):
    monkeypatch.setitem(settings.FEES, 'buy', 0.)
    monkeypatch.setitem(settings.FEES, 'sell', 0.)


def config(**changes):
    return replace(PortfolioConfig(10000, horizon=2, max_positions=1, max_loss_pct=.9, slippage_bps=0), **changes)


def picks(days, pairs=((0, 'a'),)):
    return pd.DataFrame([dict(date=days[i], symbol=symbol, rank=0) for i, symbol in pairs])


def market(n=12, *, ratio=(1, 1), price=20, ex=3, record=2):
    frame, days = history(n, ('a',))
    after = (100+Fraction(*ratio)*price)/(1+Fraction(*ratio))
    assert after.denominator == 1
    mask = frame.date.ge(days[ex])
    for name in ('open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw'):
        frame.loc[mask, name] = float(after)
    frame.loc[mask, 'low_raw'], frame.loc[mask, 'high_raw'] = float(after)-1, float(after)+1
    frame.loc[mask, 'value'] = frame.loc[mask, 'volume']*float(after)
    event = RightsIssue(event_id='R1', symbol='a', record_date=days[record], ex_date=days[ex],
        rights_numerator=ratio[0], rights_denominator=ratio[1], subscription_price_rial=str(price),
        subscription_start=days[record+1], subscription_end=days[-2], previous_price_session=days[record],
        previous_official_rial='100', ex_reference_rial=str(after.numerator), **PROOF)
    return frame, days, event


def exercise(event, day, quantity=50, *, cash=None, fee=0, offsets=(), identifier='X1', decision=None):
    amount = int(event.subscription_price_rial)*quantity+fee-sum(int(o.amount_rial) for o in offsets)
    return RightsExercise(exercise_id=identifier, event_id=event.event_id, date=day,
        decision_date=decision or day, quantity=quantity, cash_paid_rial=str(amount if cash is None else cash),
        fee_rial=str(fee), offsets=offsets, **PROOF)


def credit(day, count=50, *, identifier='C1', exercise_id='X1'):
    return RightsCredit(credit_id=identifier, exercise_id=exercise_id, date=day, cumulative_quantity=count, **PROOF)


def disposal(event, day, units, *, expected=None, identifier='D1', kind='unused_rights_distribution'):
    value = Fraction(units)
    return RightsDisposal(disposal_id=identifier, event_id=event.event_id, date=day,
        units_numerator=value.numerator, units_denominator=value.denominator, kind=kind,
        net_expected_rial=None if expected is None else str(expected), **PROOF)


def payment(day, amount, *, identifier='P1', disposal_id='D1', final=True):
    return RightsPayment(payment_id=identifier, disposal_id=disposal_id, date=day,
        net_cash_rial=str(amount), final=final, **PROOF)


def value(rational):
    return Fraction(rational['numerator'], rational['denominator'])


def test_unfunded_rights_are_not_free_saleable_shares_or_zero_assets():
    frame, days, event = market()
    result = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event])
    assert result.executions.query("side=='buy'").quantity.sum() == 50
    assert result.executions.query("side=='sell'").quantity.sum() == 50
    assert value(result.metrics['pending_subscription_units']) == 50
    assert result.metrics['total_return'] is result.metrics['final_equity_rial'] is None
    assert result.metrics['noncash_receivable_rial'] is None
    assert result.equity.loc[result.equity.date.ge(days[3]), 'equity_rial'].isna().all()
    assert not result.metrics['accepted_for_signals'] and not result.metrics['actual_corporate_entitlements_verified']


def test_funding_debits_cash_partial_credit_is_not_duplicate_and_basis_is_conserved():
    frame, days, event = market()
    result = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        exercises=[exercise(event, days[4], fee=2)], credits=[credit(days[6], 25), credit(days[8], identifier='C2')])
    assert result.metrics['subscription_cash_paid_rial'] == 1002
    assert result.metrics['exercise_fees_rial'] == result.metrics['fees_rial'] == 2
    sales = result.executions.loc[result.executions.get('credit_disposal').eq(True)]
    assert sales.quantity.sum() == 50 and sales.date.tolist() == [days[7], days[9]]
    assert result.metrics['final_equity_rial'] == pytest.approx(9998)
    assert result.metrics['realized_net_rial'] == pytest.approx(-2)
    assert result.metrics['total_return'] == pytest.approx(-.0002)
    assert result.metrics['max_drawdown'] is None
    funded = result.metrics['subscription_claims']['funded'][0]
    assert value(funded['basis_rial']) == 1002 and value(funded['pending_basis_rial']) == 0
    for day, trades in result.executions.groupby('date'):
        row = frame.loc[frame.date.eq(day)].iloc[0]
        assert trades.quantity.sum() <= row.volume*.05
        assert (trades.quantity*trades.price_rial).sum() <= row.value*.05


def test_same_day_future_sale_cannot_fund_beginning_of_day_subscription():
    frame, days, event = market()
    book = SubscriptionRightsBook([event], [exercise(event, days[4])])
    result = replay_portfolio(frame, picks(days), days, config(initial_capital_rial=5000, horizon=3), subscription_rights=book)
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.executions.side.tolist() == ['buy']
    assert book.funded == {} and book.next_exercise == 0
    assert 'نقد مشترک' in result.decisions.iloc[-1].reason


def test_rights_fraction_becomes_a_receivable_and_only_later_cash_can_fund_an_order():
    a, days, event = market(ratio=(1, 2), price=10)
    b, _ = history(len(days), ('b',))
    b[['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 300.
    b['low_raw'], b['high_raw'], b['value'] = 299., 301., b.volume*300.
    frame = pd.concat([a, b], ignore_index=True)
    result = replay_subscription_portfolio(frame, picks(days, ((0, 'a'), (6, 'b'), (7, 'b'))), days,
        config(initial_capital_rial=300), events=[event], disposals=[disposal(event, days[4], Fraction(3, 2))],
        payments=[payment(days[7], 90)])
    assert result.executions.query("symbol=='b' and side=='buy'").date.tolist() == [days[8]]
    rows = result.equity.set_index('date')
    assert rows.loc[days[6], 'cash_rial'] == 210 and rows.loc[days[7], 'cash_rial'] == 300
    assert rows.loc[days[6], 'equity_rial'] != rows.loc[days[6], 'equity_rial']
    assert value(result.metrics['pending_subscription_units']) == 0
    assert result.metrics['rights_disposal_cash_received_rial'] == 90
    assert result.metrics['total_return'] == pytest.approx(0)


def test_no_receipt_and_explicit_final_zero_are_different():
    frame, days, event = market(ratio=(1, 2), price=10)
    sale = disposal(event, days[4], Fraction(3, 2))
    args = dict(events=[event], disposals=[sale])
    pending = replay_subscription_portfolio(frame, picks(days), days, config(initial_capital_rial=300), **args)
    closed = replay_subscription_portfolio(frame, picks(days), days, config(initial_capital_rial=300),
        payments=[payment(days[7], 0)], **args)
    assert pending.metrics['total_return'] is None
    assert closed.metrics['total_return'] == pytest.approx(-.3)
    assert closed.metrics['subscription_claims']['disposals'][0]['closed'] is True


def test_new_shares_paid_and_credited_during_halt_wait_for_ex_price_and_next_session():
    frame, days, event = market(ex=6)
    frame = frame.loc[~frame.date.isin(days[3:6])].copy()
    result = replay_subscription_portfolio(frame, picks(days), days, config(horizon=8), events=[event],
        exercises=[exercise(event, days[3])], credits=[credit(days[4])])
    sales = result.executions.loc[result.executions.get('credit_disposal').eq(True)]
    assert sales.date.tolist() == [days[6]]
    rows = result.equity.set_index('date')
    assert rows.loc[days[4], 'unvalued_credited_positions'] == 1
    assert pd.isna(rows.loc[days[4], 'equity_rial'])
    assert rows.loc[days[6], 'equity_rial'] == pytest.approx(10000)
    assert result.metrics['total_return'] == pytest.approx(0)


def dividend_market(*, n=14, initial_price=100, dps=10, subscription=10):
    frame, days = history(n, ('a',))
    before_right = initial_price-dps
    after_right = Fraction(before_right+subscription, 2)
    assert after_right.denominator == 1
    for mask, price in ((frame.date.ge(days[2]) & frame.date.lt(days[4]), before_right),
                         (frame.date.ge(days[4]), after_right.numerator)):
        frame.loc[mask, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = price
        frame.loc[mask, 'low_raw'], frame.loc[mask, 'high_raw'] = price-1, price+1
        frame.loc[mask, 'value'] = frame.loc[mask, 'volume']*price
    dividend = CashDividend('DIV1', 'a', days[1], days[2], str(dps), 'b'*64)
    event = RightsIssue(event_id='R1', symbol='a', record_date=days[3], ex_date=days[4],
        rights_numerator=1, rights_denominator=1, subscription_price_rial=str(subscription),
        subscription_start=days[4], subscription_end=days[-2], previous_price_session=days[3],
        previous_official_rial=str(before_right), ex_reference_rial=str(after_right.numerator), **PROOF)
    return frame, days, event, dividend


def test_existing_dividend_offsets_funding_without_creating_cash_or_double_income():
    frame, days, event, dividend = dividend_market()
    source = CashDividendBook([dividend])
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='500')
    result = replay_subscription_portfolio(frame, picks(days), days, config(horizon=4), events=[event],
        exercises=[exercise(event, days[5], offsets=(offset,))], credits=[credit(days[7])], dividends=source)
    assert result.metrics['subscription_cash_paid_rial'] == 0
    assert result.metrics['dividend_cash_received_rial'] == 0
    assert result.metrics['dividend_offset_income_rial'] == 500
    assert value(result.metrics['outstanding_dividend_face_rial']) == 0
    assert result.metrics['total_return'] == pytest.approx(0)
    assert not source.started and source.claims == {}


def test_offset_and_a_full_later_cash_payment_cannot_spend_the_same_claim_twice():
    frame, days, event, dividend = dividend_market()
    source = CashDividendBook([dividend], [DividendPayment('cash', 'DIV1', days[5], '10', 'c'*64)])
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='500')
    book = SubscriptionRightsBook([event], [exercise(event, days[5], offsets=(offset,))], dividends=source)
    result = replay_portfolio(frame, picks(days), days, config(horizon=4), subscription_rights=book)
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert book.dividends.total_paid_rial == 0
    assert book.total_dividend_cash == 0 and book.outstanding_dividend_face == 0
    assert 'جمع وصول و تهاتر' in result.decisions.iloc[-1].reason


def test_partial_offset_and_total_receipt_do_not_require_a_rounded_per_share_number():
    frame, days, event, dividend = dividend_market(dps=1, subscription=1)
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='1')
    result = replay_subscription_portfolio(frame, picks(days), days, config(initial_capital_rial=300, horizon=4),
        events=[event], exercises=[exercise(event, days[5], 1, offsets=(offset,))], credits=[credit(days[7], 1)],
        dividends=CashDividendBook([dividend]),
        dividend_receipts=[DividendCashReceipt(receipt_id='cash', dividend_event_id='DIV1', date=days[6], amount_rial='2', **PROOF)],
        disposals=[disposal(event, days[6], 2)], payments=[payment(days[9], 98)])
    assert result.metrics['dividend_offset_income_rial'] == 1
    assert result.metrics['dividend_cash_received_rial'] == 2
    assert value(result.metrics['outstanding_dividend_face_rial']) == 0
    assert result.metrics['total_return'] == pytest.approx(0)


def test_priced_assets_floor_only_pauses_entries_until_unknown_rights_are_settled():
    a, days, event = market()
    b, _ = history(len(days), ('b',))
    result = replay_subscription_portfolio(pd.concat([a, b], ignore_index=True),
        picks(days, ((0, 'a'), (7, 'b'))), days, config(max_loss_pct=.1), events=[event],
        exercises=[exercise(event, days[4])], credits=[credit(days[6])])
    rows = result.equity.set_index('date')
    assert rows.loc[days[3:6], 'unvalued_rights_pause'].all()
    assert not rows.loc[days[6], 'unvalued_rights_pause']
    assert not result.metrics['stopped_new_entries']
    assert result.executions.query("symbol=='b' and side=='buy'").date.tolist() == [days[8]]


def test_same_issuer_reentry_and_credited_shares_use_one_daily_capacity():
    frame, days, event = market()
    result = replay_subscription_portfolio(frame, picks(days, ((0, 'a'), (4, 'a'))), days,
        config(), events=[event], exercises=[exercise(event, days[4])], credits=[credit(days[6])])
    buys = result.executions.query("side=='buy'")
    sales = result.executions.loc[result.executions.get('credit_disposal').eq(True)]
    assert buys.date.tolist() == [days[1], days[5]] and buys.lot_id.nunique() == 2
    assert sales.date.tolist() == [days[8]]
    assert result.executions.query("date==@days[7] and side=='sell'").quantity.sum() == 50
    assert result.executions.query("date==@days[8] and side=='sell'").quantity.sum() == 50
    assert result.metrics['total_return'] == pytest.approx(0)


def test_future_credit_does_not_rewrite_prior_cash_fills_or_nav():
    frame, days, event = market(n=16)
    args = dict(events=[event], exercises=[exercise(event, days[4])])
    earlier = replay_subscription_portfolio(frame, picks(days), days, config(), credits=[credit(days[8])], **args)
    later = replay_subscription_portfolio(frame, picks(days), days, config(), credits=[credit(days[11])], **args)
    pd.testing.assert_frame_equal(earlier.equity.loc[earlier.equity.date.lt(days[8])].reset_index(drop=True),
                                  later.equity.loc[later.equity.date.lt(days[8])].reset_index(drop=True))
    pd.testing.assert_frame_equal(earlier.executions.loc[earlier.executions.date.lt(days[8])].reset_index(drop=True),
                                  later.executions.loc[later.executions.date.lt(days[8])].reset_index(drop=True))


def test_future_cash_receipt_does_not_fund_prior_decisions():
    frame, days, event = market(n=16)
    args = dict(events=[event], disposals=[disposal(event, days[4], 50)])
    earlier = replay_subscription_portfolio(frame, picks(days), days, config(), payments=[payment(days[8], 2000)], **args)
    later = replay_subscription_portfolio(frame, picks(days), days, config(), payments=[payment(days[11], 2000)], **args)
    pd.testing.assert_frame_equal(earlier.equity.loc[earlier.equity.date.lt(days[8])].reset_index(drop=True),
                                  later.equity.loc[later.equity.date.lt(days[8])].reset_index(drop=True))


def test_future_subscription_does_not_reserve_cash_before_its_stated_day():
    frame, days, event = market(n=16)
    earlier = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        exercises=[exercise(event, days[7])], credits=[credit(days[10])])
    later = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        exercises=[exercise(event, days[9])], credits=[credit(days[10])])
    pd.testing.assert_frame_equal(earlier.equity.loc[earlier.equity.date.lt(days[7])].reset_index(drop=True),
                                  later.equity.loc[later.equity.date.lt(days[7])].reset_index(drop=True))


def test_out_of_window_credit_or_cash_is_not_moved_to_the_last_session():
    frame, days, event = market()
    funded = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        exercises=[exercise(event, days[4])], credits=[credit('2024-03-01')])
    disposed = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        disposals=[disposal(event, days[4], 50)], payments=[payment('2024-03-01', 2000)])
    assert funded.metrics['total_return'] is disposed.metrics['total_return'] is None
    assert value(funded.metrics['pending_subscription_units']) == 50
    assert disposed.metrics['rights_disposal_cash_received_rial'] == 0


def test_partial_receipts_keep_the_unsettled_remainder_unknown():
    frame, days, event = market()
    result = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        disposals=[disposal(event, days[4], 50, expected=2000)],
        payments=[payment(days[5], 500, final=False), payment(days[8], 1500, identifier='P2')])
    rows = result.equity.set_index('date')
    assert rows.loc[days[5], 'cash_rial'] == 8500 and pd.isna(rows.loc[days[5], 'equity_rial'])
    assert rows.loc[days[8], 'cash_rial'] == rows.loc[days[8], 'equity_rial'] == 10000
    assert result.metrics['total_return'] == pytest.approx(0)


def test_no_entitlement_for_entry_on_ex_day_or_sale_at_record_day():
    frame, days, event = market()
    late = replay_subscription_portfolio(frame, picks(days, ((2, 'a'),)), days, config(), events=[event])
    early = replay_subscription_portfolio(frame, picks(days), days, config(horizon=1), events=[event])
    assert value(late.metrics['pending_subscription_units']) == value(early.metrics['pending_subscription_units']) == 0
    assert late.metrics['total_return'] is not None and early.metrics['total_return'] is not None


def test_insufficient_rights_roll_back_the_entire_funding_phase():
    frame, days, event = market()
    book = SubscriptionRightsBook([event], [exercise(event, days[4], 25), exercise(event, days[4], 26, identifier='X2')])
    result = replay_portfolio(frame, picks(days), days, config(), subscription_rights=book)
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert book.funded == {} and book.next_exercise == 0 and book.total_cash_paid == 0
    assert book.claims['R1']['remaining'] == 50
    assert not any(row['kind']=='subscription_funded' for row in book.ledger)


def test_exercise_and_disposal_cannot_consume_the_same_rights():
    frame, days, event = market()
    result = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        exercises=[exercise(event, days[4])], disposals=[disposal(event, days[4], 1)])
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.metrics['rights_disposal_cash_received_rial'] == 0
    assert 'حق کافی' in result.decisions.iloc[-1].reason


def test_applied_offset_cannot_exceed_owned_dividend_face():
    frame, days, event, dividend = dividend_market(dps=2, subscription=10)
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='400')
    book = SubscriptionRightsBook([event], [exercise(event, days[5], offsets=(offset,))], dividends=CashDividendBook([dividend]))
    result = replay_portfolio(frame, picks(days), days, config(horizon=4), subscription_rights=book)
    assert result.metrics['status'] == 'invalid_data'
    assert book.dividend_offsets == {} and book.funded == {} and book.total_cash_paid == 0
    assert book.outstanding_dividend_face == 100


def test_offset_of_other_issuer_is_rejected_without_spending_its_claim():
    frame, days, event = market(n=14, record=3, ex=4)
    other = CashDividend('DIV1', 'b', days[1], days[2], '10', 'b'*64)
    b, _ = history(len(days), ('b',))
    b.loc[b.date.ge(days[2]), ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 90.
    b.loc[b.date.ge(days[2]), 'low_raw'], b.loc[b.date.ge(days[2]), 'high_raw'] = 89., 91.
    b.loc[b.date.ge(days[2]), 'value'] = 90000.
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='500')
    book = SubscriptionRightsBook([event], [exercise(event, days[5], offsets=(offset,))], dividends=CashDividendBook([other]))
    result = replay_portfolio(pd.concat([frame, b], ignore_index=True), picks(days, ((0, 'a'), (0, 'b'))), days,
        config(initial_capital_rial=20000, horizon=4, max_positions=2), subscription_rights=book)
    assert result.metrics['status'] == 'invalid_data' and book.dividend_offsets == {}
    assert book.outstanding_dividend_face == 500
    assert 'همان ناشر' in result.decisions.iloc[-1].reason


def test_total_dividend_receipt_cannot_exceed_face_after_offset():
    frame, days, event, dividend = dividend_market(dps=1, subscription=1)
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='1')
    result = replay_subscription_portfolio(frame, picks(days), days, config(initial_capital_rial=300, horizon=4),
        events=[event], exercises=[exercise(event, days[5], 1, offsets=(offset,))], dividends=CashDividendBook([dividend]),
        dividend_receipts=[DividendCashReceipt(receipt_id='cash', dividend_event_id='DIV1', date=days[6], amount_rial='3', **PROOF)])
    assert result.metrics['status'] == 'invalid_data'
    assert result.metrics['dividend_cash_received_rial'] == 0


def test_record_report_does_not_expose_mutable_entitlement_lots():
    frame, days, event = market()
    book = SubscriptionRightsBook([event])
    replay_portfolio(frame, picks(days), days, config(), subscription_rights=book)
    report = book.records()
    report['rights'][0]['original_lots'][0]['quantity'] = 999
    assert book.records()['rights'][0]['original_lots'][0]['quantity'] == 50


def test_zero_allocated_rights_basis_does_not_round_the_basis_of_partial_share_credits():
    frame, days, event = market()
    result = replay_subscription_portfolio(frame, picks(days), days, config(), events=[event],
        exercises=[exercise(event, days[4], 2, fee=1)],
        credits=[credit(days[6], 1), credit(days[8], 2, identifier='C2')],
        disposals=[disposal(event, days[5], 48)], payments=[payment(days[10], 1920)])
    bases = [value(row['basis_rial']) for row in result.metrics['subscription_ledger'] if row['kind']=='funded_shares_credited']
    assert bases == [Fraction(41, 2), Fraction(41, 2)] and sum(bases) == 41
    assert result.metrics['final_equity_rial'] == 9999
    assert result.metrics['realized_net_rial'] == -1


def test_received_dividend_cannot_later_be_used_as_an_offset_again():
    frame, days, event, dividend = dividend_market()
    offset = RightsOffset(dividend_event_id='DIV1', amount_rial='500')
    book = SubscriptionRightsBook([event], [exercise(event, days[5], offsets=(offset,))],
        dividends=CashDividendBook([dividend]), dividend_receipts=[DividendCashReceipt(receipt_id='paid',
            dividend_event_id='DIV1', date=days[3], amount_rial='500', **PROOF)])
    result = replay_portfolio(frame, picks(days), days, config(horizon=4), subscription_rights=book)
    assert result.metrics['status'] == 'invalid_data'
    assert book.total_dividend_cash == 500 and book.total_offset_income == 0 and book.funded == {}
    assert book.outstanding_dividend_face == 0


def test_no_event_path_preserves_the_existing_shared_cash_financial_result():
    frame, days = history(12, ('a', 'b'))
    selected = picks(days, ((0, 'a'), (0, 'b'), (6, 'b')))
    cfg = config(max_positions=2)
    before = replay_portfolio(frame, selected, days, cfg)
    after = replay_subscription_portfolio(frame, selected, days, cfg, events=[])
    columns = list(before.equity.columns)
    pd.testing.assert_frame_equal(before.equity, after.equity[columns])
    for name in ('total_return', 'max_drawdown', 'final_equity_rial', 'realized_net_rial', 'fees_rial'):
        assert before.metrics[name] == after.metrics[name]


def test_cash_dividend_only_financial_result_is_preserved_in_the_composite_book():
    from backtest.cash_dividends import replay_cash_dividend_portfolio
    from tests.test_cash_dividend_portfolio import cash_history

    frame, days, event = cash_history()
    paid = DividendPayment('paid', event.event_id, days[5], '10', 'b'*64)
    before = replay_cash_dividend_portfolio(frame, picks(days), days, config(), events=[event], payments=[paid])
    after = replay_subscription_portfolio(frame, picks(days), days, config(), events=[], dividends=CashDividendBook([event], [paid]))
    for name in ('total_return', 'max_drawdown', 'final_equity_rial', 'realized_net_rial', 'fees_rial'):
        assert before.metrics[name] == after.metrics[name]
    assert after.metrics['dividend_cash_received_rial'] == 500
    assert value(after.metrics['outstanding_dividend_face_rial']) == 0


@pytest.mark.parametrize('field,bad', [
    ('quantity', True), ('quantity', 0), ('quantity', 1.5), ('quantity', 2**53),
    ('cash_paid_rial', '-1'), ('cash_paid_rial', '0.1'), ('cash_paid_rial', 'NaN'),
    ('fee_rial', True), ('fee_rial', 'Infinity'), ('evidence_sha256', ''),
    ('evidence_available_at', '2026-10-22T10:00:00'), ('decision_date', '2099-01-01'),
    ('offsets', None),
])
def test_bad_funding_inputs_are_not_normalized_into_valid_money(field, bad):
    _, days, event = market()
    with pytest.raises(ValueError):
        replace(exercise(event, days[4]), **{field: bad})


@pytest.mark.parametrize('field,bad', [
    ('rights_numerator', 0), ('rights_denominator', True), ('ex_reference_rial', '61'),
    ('subscription_price_rial', '0'), ('subscription_price_rial', '20.5'),
    ('record_date', '2024-99-01'), ('ex_date', '2024-01-01'),
    ('previous_price_session', '2099-01-01'), ('subscription_end', '2000-01-01'),
])
def test_bad_rights_issue_cannot_create_an_entitlement(field, bad):
    _, _, event = market()
    with pytest.raises(ValueError):
        replace(event, **{field: bad})


def test_missing_funding_duplicate_credits_and_duplicate_or_excess_receipts_are_rejected():
    _, days, event = market()
    x = exercise(event, days[4])
    with pytest.raises(ValueError):
        SubscriptionRightsBook([event], [replace(x, cash_paid_rial='999')])
    with pytest.raises(ValueError):
        SubscriptionRightsBook([event], [x], [credit(days[6], 51)])
    with pytest.raises(ValueError):
        SubscriptionRightsBook([event], [x], [credit(days[6], 25), credit(days[6], 50, identifier='C2')])
    with pytest.raises(ValueError):
        SubscriptionRightsBook([event], disposals=[disposal(event, days[4], 50, expected=2000)],
            payments=[payment(days[6], 2001)])
    with pytest.raises(ValueError):
        SubscriptionRightsBook([event], disposals=[disposal(event, days[4], 50)],
            payments=[payment(days[6], 1), payment(days[8], 1, identifier='P2')])


def test_same_day_mixed_action_and_opening_unknown_rights_are_rejected():
    _, days, event = market()
    dividend = CashDividend('DIV1', 'a', event.record_date, event.ex_date, '10', 'b'*64)
    with pytest.raises(ValueError):
        SubscriptionRightsBook([event], dividends=CashDividendBook([dividend]))
    book = SubscriptionRightsBook([event])
    with pytest.raises(ValueError):
        book.start(days[3:])
    assert not book.started


def test_book_must_visit_each_calendar_session_in_order_and_start_only_once():
    _, days, _event = market()
    book = SubscriptionRightsBook([])
    book.start(days)
    with pytest.raises(ValueError):
        book.begin_day(days[1], {}, {}, 100)
    book.begin_day(days[0], {}, {}, 100)
    with pytest.raises(ValueError):
        book.begin_day(days[1], {}, {}, 100)
    book.end_day(days[0], {})
    with pytest.raises(ValueError):
        book.end_day(days[0], {})
    with pytest.raises(ValueError):
        book.start(days)


def test_non_session_payment_is_applied_on_the_next_observed_session_not_the_prior_one():
    frame, original, event = market(n=14, record=4, ex=5)
    days = pd.bdate_range('2024-01-01', periods=14).strftime('%Y-%m-%d').tolist()
    frame['date'] = frame.date.map(dict(zip(original, days, strict=True)))
    assert days[4]=='2024-01-05' and days[5]=='2024-01-08'
    event = replace(event, record_date='2024-01-06', ex_date=days[5], subscription_start='2024-01-07',
        subscription_end=days[-2], previous_price_session=days[4])
    result = replay_subscription_portfolio(frame, picks(days), days, config(horizon=6), events=[event],
        exercises=[exercise(event, '2024-01-07')], credits=[credit(days[7])])
    funded = [r for r in result.metrics['subscription_ledger'] if r['kind']=='subscription_funded']
    assert funded[0]['date'] == days[5] and funded[0]['stated_payment_date'] == '2024-01-07'
    assert result.equity.loc[result.equity.date.eq(days[4]), 'cash_rial'].iloc[0] == 5000
    assert result.metrics['total_return'] == pytest.approx(0)


@pytest.mark.parametrize('quantity', [1, 3, 10])
@pytest.mark.parametrize('ratio,subscription', [((1, 1), 20), ((1, 2), 10), ((1, 4), 20), ((2, 3), 20)])
@pytest.mark.parametrize('fee', [0, 3])
def test_flat_economic_value_is_conserved_across_funding_fractions_and_fees(quantity, ratio, subscription, fee):
    frame, days, event = market(ratio=ratio, price=subscription)
    units = quantity*Fraction(*ratio)
    whole = units.numerator//units.denominator
    fraction = units-whole
    exercises = [exercise(event, days[4], whole, fee=fee)] if whole else []
    credits = [credit(days[6], whole)] if whole else []
    disposals, payments = [], []
    if fraction:
        cash = fraction*(Fraction(event.ex_reference_rial)-subscription)
        assert cash.denominator == 1
        disposals = [disposal(event, days[5], fraction)]
        payments = [payment(days[8], cash.numerator)]
    result = replay_subscription_portfolio(frame, picks(days), days, config(initial_capital_rial=100*quantity),
        events=[event], exercises=exercises, credits=credits, disposals=disposals, payments=payments)
    expected = 100*quantity-(fee if whole else 0)
    assert result.metrics['final_equity_rial'] == pytest.approx(expected)
    assert result.metrics['realized_net_rial'] == pytest.approx(expected-100*quantity)
    assert value(result.metrics['pending_subscription_units']) == 0
    assert not result.metrics['accepted_for_signals'] and not result.metrics['profitability_proven']


@pytest.mark.parametrize('quantity', [1, 3, 10])
@pytest.mark.parametrize('mode', ['cash', 'one_rial_offset', 'full_offset'])
def test_conservation_with_partial_offsets_does_not_require_per_share_division(quantity, mode):
    frame, days, event, dividend = dividend_market(dps=20, subscription=20)
    total_face = 20*quantity
    offset_amount = 0 if mode=='cash' else 1 if mode=='one_rial_offset' else total_face
    offsets = (RightsOffset(dividend_event_id='DIV1', amount_rial=str(offset_amount)),) if offset_amount else ()
    receipts = [DividendCashReceipt(receipt_id='cash', dividend_event_id='DIV1', date=days[7],
        amount_rial=str(total_face-offset_amount), **PROOF)] if total_face > offset_amount else []
    result = replay_subscription_portfolio(frame, picks(days), days,
        config(initial_capital_rial=100*quantity, horizon=4), events=[event],
        exercises=[exercise(event, days[6], quantity, fee=3, offsets=offsets)], credits=[credit(days[8], quantity)],
        dividends=CashDividendBook([dividend]), dividend_receipts=receipts)
    assert result.metrics['final_equity_rial'] == pytest.approx(100*quantity-3)
    assert result.metrics['realized_net_rial'] == pytest.approx(-3)
    assert result.metrics['dividend_income_rial'] == total_face
    assert result.metrics['subscription_cash_paid_rial'] == total_face-offset_amount+3
    assert value(result.metrics['outstanding_dividend_face_rial']) == 0


def test_symbol_normalization_preserves_input_frames_and_rejects_collapsed_duplicates():
    frame, days, event = market()
    frame['symbol'] = 'ك'
    selected = picks(days)
    selected['symbol'] = 'ك'
    event = replace(event, symbol='ك')
    before_frame, before_picks = frame.copy(deep=True), selected.copy(deep=True)
    result = replay_subscription_portfolio(frame, selected, days, config(), events=[event])
    assert set(result.executions.symbol) == {'ک'}
    pd.testing.assert_frame_equal(frame, before_frame)
    pd.testing.assert_frame_equal(selected, before_picks)
    duplicate = frame.copy()
    duplicate['symbol'] = 'ک'
    with pytest.raises(ValueError):
        replay_subscription_portfolio(pd.concat([frame, duplicate], ignore_index=True), selected, days, config(), events=[event])
