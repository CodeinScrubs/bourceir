"""سناریوهای مصنوعی صرفاً در آزمون: مالکیت، کسور، اعتبار، نقد مشترک و ترتیب زمان."""

from dataclasses import asdict, replace
from fractions import Fraction

import pandas as pd
import pytest

from backtest.bonus_shares import (
    BonusCapitalization,
    BonusFractionSettlement,
    BonusRelease,
    BonusShareBook,
    replay_bonus_share_portfolio,
)
from backtest.cash_dividends import CashDividendBook
from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from research.dip import DipRule
from tests.test_portfolio import history

SHA = 'a'*64
AVAILABLE = '2026-10-19T10:00:00+03:30'


def market(n=12):
    prices, dates = history(n, ('a',))
    after = prices.date >= dates[3]
    prices.loc[after, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 80.
    prices.loc[after, 'low_raw'], prices.loc[after, 'high_raw'] = 79., 81.
    prices.loc[after, 'value'] = 80000.
    event = BonusCapitalization('event', 'a', dates[2], dates[3], 1, 4, dates[2], '100', '80', SHA, AVAILABLE)
    return prices, dates, event


def config(**kwargs):
    return replace(PortfolioConfig(10000, horizon=2, max_positions=1, max_loss_pct=.9), **kwargs)


def pick(dates, indices=(0,)):
    return pd.DataFrame([dict(date=dates[i], symbol='a', rank=0) for i in indices])


def release(event, date, numerator=1, denominator=1, identifier='release'):
    return BonusRelease(identifier, event.event_id, date, numerator, denominator, SHA, AVAILABLE)


def settle(event, date, amount='20'):
    return BonusFractionSettlement('settlement', event.event_id, date, amount, SHA, AVAILABLE)


def units(value):
    return Fraction(value['numerator'], value['denominator'])


def test_uncredited_shares_never_become_sellable_or_a_zero_valued_receivable():
    prices, dates, event = market()
    modeled = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event])
    legacy = replay_portfolio(prices, pick(dates), dates, config())
    bought = modeled.executions.query("side == 'buy'").quantity.sum()
    assert modeled.executions.query("side == 'sell'").quantity.sum() == bought
    assert legacy.executions.query("side == 'sell'").quantity.sum() > bought
    assert units(modeled.metrics['pending_bonus_units']) == Fraction(int(bought), 4)
    assert modeled.metrics['final_equity_rial'] is modeled.metrics['total_return'] is None
    assert modeled.metrics['noncash_receivable_rial'] is None and modeled.metrics['max_drawdown'] is None
    assert modeled.equity.loc[modeled.equity.date >= event.ex_date, 'equity_rial'].isna().all()
    assert modeled.equity.priced_assets_rial.notna().all()
    assert not modeled.metrics['profitability_proven'] and not modeled.metrics['accepted_for_signals']


def test_credit_after_sale_and_reentry_does_not_overwrite_new_position_and_shares_daily_capacity():
    prices, dates, event = market()
    result = replay_bonus_share_portfolio(prices, pick(dates, (0, 3)), dates, config(), events=[event],
                                         releases=[release(event, dates[5])])
    buys = result.executions.query("side == 'buy'")
    assert buys.date.tolist() == [dates[1], dates[4]] and buys.lot_id.nunique() == 2
    credit_sales = result.executions.loc[result.executions.get('credit_disposal').eq(True)]
    assert credit_sales.date.min() == dates[6] and dates[5] not in set(credit_sales.date)
    assert credit_sales.quantity.sum() == int(buys.iloc[0].quantity/4)
    # هر دو لات در روز۶ سررسیدند؛ ظرفیت۵۰سهم میان آنها تقسیم می‌شود، نه۵۰سهم برای هرکدام.
    assert result.executions.query("date == @dates[6] and side == 'sell'").quantity.sum() == 50
    assert dates[7] in set(credit_sales.date)
    for (day, symbol), executions in result.executions.groupby(['date', 'symbol']):
        row = prices.loc[(prices.date == day) & (prices.symbol == symbol)].iloc[0]
        assert executions.quantity.sum() <= row.volume*.05
        assert (executions.quantity*executions.price_rial).sum() <= row.value*.05 + 1e-9
    assert units(result.metrics['pending_bonus_units']) == Fraction(1, 4)
    assert result.metrics['open_positions'] == result.metrics['open_lots'] == 0
    assert result.metrics['issuer_exposures'] == 0 and result.metrics['unvalued_fractional_issuers'] == 1


def test_cash_for_fraction_is_explicit_end_of_day_and_cannot_fund_that_days_order(monkeypatch):
    monkeypatch.setitem(settings.FEES, 'buy', 0.)
    monkeypatch.setitem(settings.FEES, 'sell', 0.)
    prices, dates = history(11, ('a', 'b'))
    a = prices.symbol.eq('a')
    for mask, price in ((a & prices.date.lt(dates[2]), 30.), (a & prices.date.ge(dates[2]), 20.), (~a, 100.)):
        prices.loc[mask, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = price
        prices.loc[mask, 'low_raw'], prices.loc[mask, 'high_raw'] = price-1, price+1
        prices.loc[mask, 'value'] = prices.loc[mask, 'volume']*price
    event = BonusCapitalization('cash-case', 'a', dates[1], dates[2], 1, 2, dates[1], '30', '20', SHA, AVAILABLE)
    selected = pd.DataFrame([dict(date=dates[0], symbol='a', rank=0), dict(date=dates[6], symbol='b', rank=0),
                             dict(date=dates[7], symbol='b', rank=0)])
    result = replay_bonus_share_portfolio(prices, selected, dates, config(initial_capital_rial=100,
        horizon=1, slippage_bps=0,max_loss_pct=.1), events=[event], releases=[release(event, dates[4])],
        settlements=[settle(event, dates[7], '15')])
    b = result.executions.query("symbol == 'b' and side == 'buy'")
    assert b.date.tolist() == [dates[8]]
    assert result.equity.loc[result.equity.date == dates[6], 'cash_rial'].iloc[0] == 90.
    assert result.equity.loc[result.equity.date == dates[7], 'cash_rial'].iloc[0] == 105.
    assert result.metrics['fraction_cash_rial'] == 15 and result.metrics['fraction_realized_rial'] == 5.
    assert units(result.metrics['pending_bonus_units']) == 0
    assert result.metrics['total_return'] == pytest.approx(.05)
    assert result.metrics['max_drawdown'] is None and result.metrics['status'] == 'unvalued_bonus_path'
    assert not result.metrics['actual_corporate_entitlements_verified']
    pause=result.equity.set_index('date').unvalued_rights_pause
    assert pause.loc[dates[2]] and not pause.loc[dates[7]] and not result.metrics['stopped_new_entries']


def test_two_partial_releases_do_not_credit_the_first_part_twice_and_do_not_value_the_fraction():
    prices, dates, event = market()
    result = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event], releases=[
        release(event, dates[5], 1, 2, 'first'), release(event, dates[7], identifier='second')])
    records = [r for r in result.metrics['bonus_ledger'] if r['kind'] == 'bonus_whole_shares_credited']
    assert [r['quantity'] for r in records] == [6, 6]
    assert result.executions.loc[result.executions.get('credit_disposal').eq(True), 'quantity'].sum() == 12
    assert units(result.metrics['pending_bonus_units']) == Fraction(1, 4)
    assert not any(r['kind'] == 'fraction_cash_received' for r in result.metrics['bonus_ledger'])


def test_release_after_window_does_not_move_into_the_last_day():
    prices, dates, event = market()
    result = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event],
        releases=[release(event, '2024-02-01')])
    assert not result.executions.get('credit_disposal', pd.Series(dtype=bool)).eq(True).any()
    assert units(result.metrics['pending_bonus_units']) == Fraction(49, 4)


def test_fractional_entitlements_pool_across_lots_and_total_basis_is_conserved():
    event = BonusCapitalization('pooled', 'a', '2024-01-01', '2024-01-02', 1, 2,
        '2024-01-01', '100', '67', SHA, AVAILABLE)
    book = BonusShareBook([event], [release(event, '2024-01-02')])
    book.start(['2024-01-01', '2024-01-02'])
    positions = dict(first=dict(symbol='a', quantity=1., cost=100.), second=dict(symbol='a', quantity=1., cost=110.))
    book.begin_day('2024-01-01', {}, positions)
    assert book.end_day('2024-01-01', positions) == ({}, 0., 0.)
    row = dict(valid=True, mark=67., previous_date='2024-01-01', previous_official=100., ref_price_raw=67.)
    book.begin_day('2024-01-02', {'a': row}, positions)
    credits, cash, realized = book.end_day('2024-01-02', positions)
    assert sum(p['quantity'] for p in credits.values()) == 1
    assert book.pending_units == 0 and cash == realized == 0
    assert sum(p['cost'] for p in positions.values()) + sum(p['cost'] for p in credits.values()) == pytest.approx(210.)
    assert len(book.records()[0]['origin_lots']) == 2


def test_source_terms_known_later_do_not_rewrite_decision_targets_budget_or_executed_quantities():
    prices, dates, event = market()
    selected = pick(dates, (0, 3, 6))
    one = replay_bonus_share_portfolio(prices, selected, dates, config(), events=[event])
    two = replay_bonus_share_portfolio(prices, selected, dates, config(), events=[replace(event,
        bonus_numerator=26, bonus_denominator=100, evidence_available_at='2026-10-19T11:00:00+03:30')])
    pd.testing.assert_frame_equal(one.executions.drop(columns='realized_net_rial'), two.executions.drop(columns='realized_net_rial'))
    pd.testing.assert_frame_equal(one.decisions, two.decisions)
    pd.testing.assert_series_equal(one.equity.priced_assets_rial, two.equity.priced_assets_rial)


def test_nominal_dip_target_survives_event_and_credit_lot_has_its_own_disposal_rule():
    prices, dates, event = market()
    prices.loc[1, ['low_raw', 'open_raw']] = [98., 98.5]
    prices.loc[3, 'high_raw'] = 120.
    rule = DipRule(dip=.01, take_profit=.5, hold=2)
    result = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event],
        releases=[release(event, dates[5])], dip_rule=rule)
    assert result.executions.side.eq('buy').any()
    credited = result.executions.loc[result.executions.get('credit_disposal').eq(True)]
    assert not credited.empty and credited.date.min() == dates[6]
    assert result.executions.loc[result.executions.get('credit_disposal').eq(False), 'exit_kind'].eq('time').all()
    assert result.assumptions['bonus_target_policy'].startswith('original nominal targets unchanged')


@pytest.mark.parametrize('field,bad', [('bonus_numerator', True), ('bonus_denominator', 0),
    ('bonus_numerator', 1.5), ('bonus_numerator', '1'), ('previous_official_rial', None),
    ('ex_reference_rial', '-1'), ('record_date', '2024-1-3'), ('evidence_available_at', '2024-01-01T10:00:00')])
def test_event_bad_dates_units_ratios_and_time_fail_closed(field, bad):
    _, _, event = market()
    with pytest.raises(ValueError):
        BonusCapitalization(**(asdict(event) | {field: bad}))


def test_unzoned_source_clock_is_rejected_before_financial_amounts(monkeypatch):
    _,_,event=market()
    monkeypatch.setattr('backtest.bonus_shares._money',lambda *_a,**_k:pytest.fail('money read before source clock'))
    with pytest.raises(ValueError,match='منطقه زمانی'):
        replace(event,evidence_available_at='2026-10-19T10:00:00')


def test_duplicate_decreasing_early_or_unknown_releases_are_invalid():
    _, dates, event = market()
    invalid = [[release(event, dates[2])], [replace(release(event, dates[5]), event_id='absent')],
        [release(event, dates[5], 1, 2), release(event, dates[6], 1, 3, 'later')],
        [release(event, dates[5]), release(event, dates[5], identifier='duplicate-day')]]
    for releases in invalid:
        with pytest.raises(ValueError):
            BonusShareBook([event], releases)


@pytest.mark.parametrize('amount', ['-1', None, True, 'NaN', '1.5', 2**53])
def test_fraction_cash_requires_explicit_integer_net_rial(amount):
    _, dates, event = market()
    with pytest.raises(ValueError):
        settle(event, dates[7], amount)


def test_fraction_settlement_without_full_credit_and_compound_or_mixed_actions_are_not_synthesized():
    prices, dates, event = market()
    with pytest.raises(ValueError):
        BonusShareBook([event], settlements=[settle(event, dates[7])])
    with pytest.raises(ValueError):
        replay_portfolio(prices, pick(dates), dates, config(), cash_dividends=CashDividendBook([]),
                         bonus_shares=BonusShareBook([event]))
    later = replace(event, event_id='later', record_date=dates[5], ex_date=dates[6],
        previous_price_session=dates[5], previous_official_rial='80', ex_reference_rial='80',
        bonus_numerator=1, bonus_denominator=100)
    result = replay_bonus_share_portfolio(prices, pick(dates, (0, 3)), dates, config(), events=[event, later])
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert 'unknown_corporate_entitlement' in set(result.decisions.status)


def test_undeclared_following_gap_while_only_uncredited_rights_remain_cannot_be_ignored():
    prices, dates, event = market()
    prices.loc[prices.date >= dates[6], ['low_raw', 'high_raw', 'open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] = [63,65,64,64,64,64,64000]
    result = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event])
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None


def test_credit_on_second_actions_record_day_requires_an_explicit_intraday_eligibility_contract():
    prices, dates, event = market()
    # اعتبار در پایان همان روز، مبنای خودساخته برای مشمول‌شدن یا نشدنِ افزایش بعدی نیست.
    later = replace(event, event_id='later', record_date=dates[5], ex_date=dates[6],
        previous_price_session=dates[5], previous_official_rial='80', ex_reference_rial='80',
        bonus_numerator=1, bonus_denominator=100)
    result = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event, later],
                                         releases=[release(event, dates[5])])
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert 'unknown_corporate_entitlement' in set(result.decisions.status)


def test_missing_or_conflicting_price_anchor_is_not_replaced_with_a_ratio_based_quantity():
    prices, dates, event = market()
    prices.loc[prices.date.eq(event.ex_date),'ref_price_raw']=79.
    result = replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event])
    assert result.metrics['status'] == 'invalid_data' and result.metrics['final_equity_rial'] is None


def test_a_mixed_action_price_gap_is_not_silently_accepted_as_pure_bonus():
    _, _, event = market()
    with pytest.raises(ValueError,match='ترکیبی'):
        replace(event,ex_reference_rial='70')


def test_credited_shares_stay_non_cash_through_missing_sessions_until_an_observed_exit():
    prices, dates, event = market()
    prices=prices.loc[~prices.date.isin(dates[5:8])]
    result=replay_bonus_share_portfolio(prices,pick(dates),dates,config(),events=[event],
        releases=[release(event,dates[5])])
    credit_sales=result.executions.loc[result.executions.get('credit_disposal').eq(True)]
    assert credit_sales.date.min()==dates[8]
    assert 'exit_delayed' in set(result.decisions.status)
    cash=result.equity.set_index('date').cash_rial
    assert cash.loc[dates[4]]==cash.loc[dates[5]]==cash.loc[dates[6]]==cash.loc[dates[7]]


def test_uncredited_issuer_does_not_free_a_position_slot_for_another_company():
    prices,dates,event=market()
    peer=prices.copy();peer['symbol']='b'
    all_prices=pd.concat([prices,peer],ignore_index=True)
    selected=pd.DataFrame([dict(date=dates[0],symbol='a',rank=0),dict(date=dates[3],symbol='b',rank=0)])
    result=replay_bonus_share_portfolio(all_prices,selected,dates,config(),events=[event])
    assert set(result.executions.symbol)=={'a'}
    assert 'slots_full' in set(result.decisions.status) and result.metrics['issuer_exposures']==1


def test_each_replay_requires_a_fresh_book_and_an_explicit_in_range_record_session():
    prices, dates, event = market()
    book = BonusShareBook([event])
    replay_portfolio(prices, pick(dates), dates, config(), bonus_shares=book)
    with pytest.raises(ValueError):
        replay_portfolio(prices, pick(dates), dates, config(), bonus_shares=book)
    with pytest.raises(ValueError):
        replay_bonus_share_portfolio(prices, pick(dates), dates, config(), events=[event], start=dates[4])
