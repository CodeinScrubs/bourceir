"""حسابداری سود نقدی و تقدم زمان؛ تمام ارقام آزمون فقط روی داده موقت‌اند."""

from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from backtest.cash_dividends import CashDividend, CashDividendBook, DividendPayment, replay_cash_dividend_portfolio
from backtest.portfolio import replay_portfolio
from tests.test_portfolio import config, history, picks


def cash_history(n=8):
    frame, days = history(n, ('a',))
    for name in ('open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw'):
        frame.loc[frame.date >= days[2], name] *= .9
    frame.loc[frame.date >= days[2], 'value'] *= .9
    event = CashDividend('E1', 'a', days[1], days[2], '10', 'a' * 64)
    return frame, days, event


def run(frame, days, event, *, payments=(), selected=None, **changes):
    return replay_cash_dividend_portfolio(frame, picks(days[:1], ('a',)) if selected is None else selected,
        days, config(max_positions=1, **changes), events=[event], payments=payments)


def test_dividend_does_not_create_extra_saleable_shares_or_erase_the_claim_after_sale():
    frame, days, event = cash_history()
    new = run(frame, days, event)
    buy, sell = new.executions.iloc[0], new.executions.iloc[1]
    old = replay_portfolio(frame, picks(days[:1], ('a',)), days, config(max_positions=1))
    assert buy['side'] == 'buy' and sell['side'] == 'sell' and buy.quantity == sell.quantity == 49
    assert old.executions.iloc[1].quantity > buy.quantity
    assert new.metrics['open_positions'] == 0 and new.metrics['cash_dividend_receivable_face_rial'] == 490
    assert new.metrics['cash_dividend_claims'][0]['entitled_quantity'] == 49
    assert new.metrics['cash_dividend_income_rial'] == 0 and new.metrics['fractional_exposures'] == []
    assert new.metrics['total_return'] is None and new.metrics['final_equity_rial'] is None
    assert new.metrics['max_drawdown'] is None and new.metrics['nav_unvalued_days'] == len(days) - 2
    assert new.equity.loc[new.equity.date >= days[2], 'equity_rial'].isna().all()
    expected_cash = config().initial_capital_rial - buy.quantity * buy.price_rial - buy.fee_rial
    expected_cash += sell.quantity * sell.price_rial - sell.fee_rial
    assert new.equity.iloc[-1].cash_rial == pytest.approx(expected_cash)
    assert new.metrics['liquidation_assets_final_rial'] == pytest.approx(expected_cash)
    assert new.assumptions['version'] == 'portfolio-cash-dividends-v1'
    assert new.assumptions['dividend_settlement_verified'] is False


def test_purchase_on_ex_day_is_not_entitled_and_sale_on_record_day_removes_entitlement():
    frame, days, event = cash_history()
    late = run(frame, days, event, selected=picks(days[1:2], ('a',)))
    assert late.executions.iloc[0].date == days[2]
    assert late.metrics['cash_dividend_claims'] == [] and late.metrics['total_return'] is not None
    # تاریخ مشمول این نمونه، پایان روز فروش است؛ سهمی در آن لحظه نداریم.
    frame, days = history(8, ('a',))
    for col in ('open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value'):
        frame.loc[frame.date >= days[3], col] *= .9
    event = CashDividend('E1', 'a', days[2], days[3], '10', 'a' * 64)
    early = run(frame, days, event)
    assert early.metrics['cash_dividend_claims'] == [] and early.metrics['total_return'] is not None


def test_partial_sales_do_not_reduce_previously_recorded_claim_quantity():
    frame, days, event = cash_history()
    frame.loc[frame.date == days[2], ['volume', 'value']] = [100, 9000]
    new = run(frame, days, event)
    sells = new.executions.loc[new.executions.side == 'sell']
    assert len(sells) == 2 and sells.quantity.sum() == 49 and sells.iloc[0].quantity == 5
    assert new.metrics['cash_dividend_receivable_face_rial'] == 490


def test_partial_payments_preserve_remaining_face_and_only_cash_received_is_realized():
    frame, days, event = cash_history()
    part = DividendPayment('P1', 'E1', days[4], '3', 'b' * 64)
    new = run(frame, days, event, payments=[part])
    assert new.metrics['cash_dividend_income_rial'] == 147
    assert new.metrics['cash_dividend_receivable_face_rial'] == 343 and new.metrics['total_return'] is None
    assert new.metrics['realized_net_rial'] == pytest.approx(new.metrics['stock_realized_net_rial'] + 147)
    assert new.metrics['cash_dividend_claims'][0]['paid_per_share_rial'] == '3'


def test_full_payment_after_sale_restores_final_nav_but_does_not_invent_the_missing_path_drawdown():
    frame, days, event = cash_history()
    payments = [DividendPayment('P1', 'E1', days[4], '3', 'b' * 64),
                DividendPayment('P2', 'E1', days[5], '7', 'c' * 64)]
    new = run(frame, days, event, payments=payments)
    assert new.metrics['cash_dividend_income_rial'] == 490 and new.metrics['cash_dividend_receivable_face_rial'] == 0
    assert new.metrics['final_equity_rial'] == pytest.approx(new.equity.iloc[-1].cash_rial)
    assert new.metrics['total_return'] == pytest.approx(new.metrics['final_equity_rial'] / 10000 - 1)
    assert new.metrics['max_drawdown'] is None and new.metrics['status'] == 'unvalued_receivable_path'
    assert new.equity.loc[new.equity.date < days[5], 'equity_rial'].isna().sum() == 3
    assert new.equity.loc[new.equity.date >= days[5], 'equity_rial'].notna().all()
    assert new.assumptions['same_day_dividend_cash_reused'] is False
    assert not new.metrics['accepted_for_signals'] and not new.metrics['actual_corporate_entitlements_verified']


def test_future_payment_and_prices_cannot_change_past_equity_cash_claims_or_fills():
    frame, days, event = cash_history()
    changed = frame.copy()
    changed.loc[changed.date >= days[6], ['low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'open_raw']] = [70, 72, 71, 71, 71]
    payment = DividendPayment('P1', 'E1', days[6], '10', 'b' * 64)
    baseline, future = run(frame, days, event), run(changed, days, event, payments=[payment])
    pd.testing.assert_frame_equal(baseline.equity.loc[baseline.equity.date < days[6]],
                                  future.equity.loc[future.equity.date < days[6]])
    pd.testing.assert_frame_equal(baseline.executions, future.executions)


@pytest.mark.parametrize('mutation', ['absent', 'mismatch', 'missing_ex_row', 'wrong_record', 'duplicate_action'])
def test_unknown_or_concurrent_corporate_actions_cannot_fall_back_to_synthetic_shares(mutation):
    frame, days, event = cash_history()
    events = [event]
    if mutation == 'absent':
        events = []
    elif mutation == 'mismatch':
        events = [replace(event, dps_rial='9')]
    elif mutation == 'missing_ex_row':
        frame = frame.loc[frame.date != days[2]]
    elif mutation == 'wrong_record':
        events = [replace(event, record_date=days[0])]
    else:
        with pytest.raises(ValueError, match='همزمان|هم‌زمان'):
            CashDividendBook([event, replace(event, event_id='E2')])
        return
    new = replay_cash_dividend_portfolio(frame, picks(days[:1], ('a',)), days,
        config(max_positions=1, horizon=4), events=events)
    assert new.metrics['status'] == 'invalid_data' and new.metrics['total_return'] is None
    assert 'unknown_corporate_entitlement' in set(new.decisions.status)
    assert new.metrics['cash_dividend_income_rial'] == 0


@pytest.mark.parametrize('value', [True, '-1', 'NaN', float('inf'), 2**63, '1e-1000'])
def test_bad_amounts_are_rejected_before_any_replay(value):
    _, _, event = cash_history()
    with pytest.raises(ValueError):
        replace(event, dps_rial=value)


@pytest.mark.parametrize('case', ['duplicate_payment', 'too_much', 'before_ex', 'unknown_event', 'zero_payment',
                                 'duplicate_event', 'bad_hash', 'bad_date', 'same_record_ex'])
def test_bad_payment_or_event_contract_is_not_financial_data(case):
    _, days, event = cash_history()
    payment = DividendPayment('P1', 'E1', days[4], '10', 'b' * 64)
    with pytest.raises(ValueError):
        if case == 'duplicate_payment':
            CashDividendBook([event], [payment, payment])
        elif case == 'too_much':
            CashDividendBook([event], [payment, replace(payment, payment_id='P2', per_share_rial='1')])
        elif case == 'before_ex':
            CashDividendBook([event], [replace(payment, date=days[1])])
        elif case == 'unknown_event':
            CashDividendBook([event], [replace(payment, event_id='OTHER')])
        elif case == 'zero_payment':
            replace(payment, per_share_rial='0')
        elif case == 'duplicate_event':
            CashDividendBook([event, event])
        elif case == 'bad_hash':
            replace(event, evidence_sha256='unknown')
        elif case == 'bad_date':
            replace(event, record_date='2024-2-30')
        else:
            replace(event, record_date=event.ex_date)


def test_book_is_single_run_and_explicit_calendar_is_required():
    frame, days, event = cash_history()
    book = CashDividendBook([event])
    book.start(days)
    with pytest.raises(ValueError, match='تازه'):
        replay_portfolio(frame, picks(days[:1], ('a',)), days, config(), cash_dividends=book)
    with pytest.raises(ValueError, match='تقویم'):
        CashDividendBook([replace(event, ex_date='2030-01-01')]).start(days)


def test_exact_decimal_installments_and_non_session_payment_are_not_double_counted():
    _, days, event = cash_history()
    event = replace(event, dps_rial='1')
    book = CashDividendBook([event], [DividendPayment('P1', 'E1', days[3], '.3', 'b' * 64),
                                     DividendPayment('P2', 'E1', days[4], '.7', 'c' * 64)])
    book.start(days)
    book.end_day(days[1], {'a': {'quantity': 10}})
    book.begin_day(days[2], {'a': dict(previous_official=100, ref_price_raw=99, previous_date=days[1])})
    assert book.end_day(days[4], {}) == 10  # هر دو شاهد در نخستین جلسه پس از تاریخشان ثبت می‌شوند.
    assert book.outstanding_face_rial == Decimal(0) and book.total_paid_rial == 10
    assert book.end_day(days[5], {}) == 0 and len(book.ledger) == 3


def test_cash_book_without_actions_does_not_change_default_execution_or_sizing():
    frame, days = history(8, ('a', 'b'))
    selected = picks(days, ('a', 'b'))
    old = replay_portfolio(frame, selected, days, config())
    new = replay_cash_dividend_portfolio(frame, selected, days, config(), events=[])
    pd.testing.assert_frame_equal(old.executions, new.executions)
    pd.testing.assert_frame_equal(old.equity, new.equity[old.equity.columns])
    for name in ('total_return', 'final_equity_rial', 'max_drawdown', 'fees_rial', 'realized_net_rial'):
        assert old.metrics[name] == new.metrics[name]


def test_same_event_cannot_reset_a_paid_claim_by_repeating_the_ex_day():
    _, days, event = cash_history()
    book = CashDividendBook([event])
    book.start(days)
    book.end_day(days[1], {'a': {'quantity': 10}})
    row = {'a': dict(previous_official=100, ref_price_raw=90, previous_date=days[1])}
    book.begin_day(days[2], row)
    with pytest.raises(ValueError, match='یکتا'):
        book.begin_day(days[2], row)
    assert book.outstanding_face_rial == 100


def test_dip_target_remains_the_recorded_nominal_price_after_the_cash_event():
    from research.dip import DipRule

    frame, days, event = cash_history()
    frame.loc[frame.date == days[1], ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'value']] = [98, 97, 100, 98, 100, 98000]
    # هدف از ورود ۹۸: ۱۰۰٫۹۴ است؛ سقف ۹۱ بعد از حق به آن نمی‌رسد.
    new = replay_cash_dividend_portfolio(frame, picks(days[:1], ('a',)), days,
        config(max_positions=1, horizon=1), events=[event], dip_rule=DipRule(hold=1))
    sells = new.executions.loc[new.executions.side == 'sell']
    assert set(sells.exit_kind) == {'time'}
    assert new.assumptions['dividend_target_policy'] == 'nominal_entry_price_target_unchanged'


def test_decimal_rights_remain_exact_with_large_quantity_and_low_ambient_precision():
    from decimal import localcontext

    _, days, event = cash_history()
    event = replace(event, dps_rial='0.12345678901234567890123456789')
    with localcontext() as context:
        context.prec = 4
        book = CashDividendBook([event])
        book.start(days)
        book.end_day(days[1], {'a': {'quantity': 10**12}})
        # این آزمون دفتر است؛ کنترل قیمت جدا در آزمون‌های موتور پوشش داده شده است.
        book.claims[event.event_id] = dict(event=event, quantity=10**12, paid_per_share=Decimal(0))
        assert book.outstanding_face_rial == Decimal('123456789012.34567890123456789')


def test_one_rial_unexplained_reference_gap_is_not_hidden_by_the_legacy_ratio_tolerance():
    frame, days = history(6, ('a',))
    columns = ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']
    frame[columns] *= 20000
    frame['value'] *= 20000
    frame.loc[frame.date >= days[2], columns] -= 1
    frame.loc[frame.date >= days[2], 'value'] -= frame.loc[frame.date >= days[2], 'volume']
    result = replay_cash_dividend_portfolio(frame, picks(days[:1], ('a',)), days,
        config(initial_capital_rial=100000000, max_positions=1), events=[])
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert 'unknown_corporate_entitlement' in set(result.decisions.status)
