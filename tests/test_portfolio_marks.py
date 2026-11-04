"""A holding mark and an actual execution have distinct raw-data dependencies."""

import numpy as np
import pytest

from backtest.bonus_shares import BonusShareBook
from backtest.cash_dividends import CashDividendBook
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from backtest.subscription_rights import SubscriptionRightsBook
from tests.test_portfolio import config, history, picks


def book(kind):
    if kind=='cash':
        return {'cash_dividends':CashDividendBook([])}
    if kind=='bonus':
        return {'bonus_shares':BonusShareBook([])}
    if kind=='rights':
        return {'subscription_rights':SubscriptionRightsBook([])}
    return {}


@pytest.mark.parametrize('kind',['synthetic','cash','bonus','rights'])
@pytest.mark.parametrize('prepared',[False,True])
def test_bad_vwap_on_a_holding_day_does_not_erase_a_known_official_mark(kind,prepared):
    prices,days = history(8,('a',))
    prices.loc[prices.date.eq(days[3]),'value'] = 101001.
    source = prepare_portfolio_prices(prices) if prepared else prices
    out = replay_portfolio(source,picks(days[:1],('a',)),days,
        config(max_positions=1,horizon=4),**book(kind))
    assert out.metrics['status']=='complete' and out.metrics['total_return'] is not None
    assert out.equity.date.tolist()==days.tolist()
    at = out.equity.loc[out.equity.date.eq(days[3])].iloc[0]
    assert at.positions==1 and at.stale_positions==0 and np.isfinite(at.equity_rial)
    assert not out.executions.date.eq(days[3]).any()
    assert out.executions.loc[out.executions.side.eq('sell'),'date'].tolist()==[days[5]]


@pytest.mark.parametrize('kind',['synthetic','cash','bonus','rights'])
def test_unknown_vwap_on_a_required_exit_remains_unknown_instead_of_a_delayed_fill(kind):
    prices,days = history(8,('a',))
    prices.loc[prices.date.eq(days[5]),'value'] = 101001.
    out = replay_portfolio(prices,picks(days[:1],('a',)),days,
        config(max_positions=1,horizon=4),**book(kind))
    assert out.metrics['status']=='invalid_data' and out.metrics['total_return'] is None
    assert out.executions.side.tolist()==['buy']
    assert 'unknown_exit_execution' in set(out.decisions.status)
    assert out.equity.date.iloc[-1]==days[5] and np.isnan(out.equity.equity_rial.iloc[-1])


@pytest.mark.parametrize('field',['close_final_raw','ref_price_raw'])
@pytest.mark.parametrize('value',[None,0,True])
def test_unknown_mark_or_reference_still_invalidates_a_nonexecution_day(field,value):
    prices,days = history(8,('a',))
    prices[field] = prices[field].astype(object)
    prices.loc[prices.date.eq(days[3]),field] = value
    out = replay_portfolio(prices,picks(days[:1],('a',)),days,config(max_positions=1,horizon=4))
    assert out.metrics['status']=='invalid_data' and out.metrics['total_return'] is None
    assert len(out.executions)==1 and out.equity.date.iloc[-1]==days[3]


def test_missing_entire_bar_retains_a_stale_mark_rather_than_inventing_a_fresh_one():
    prices,days = history(8,('a',))
    prices = prices.loc[~prices.date.eq(days[3])]
    out = replay_portfolio(prices,picks(days[:1],('a',)),days,config(max_positions=1,horizon=4))
    assert out.metrics['stale_mark_days']==1
    assert out.equity.loc[out.equity.date.eq(days[3]),'stale_positions'].iloc[0]==1
    assert not out.executions.date.eq(days[3]).any()


def test_explicit_positive_official_mark_on_zero_activity_day_does_not_imply_a_fill():
    prices,days = history(8,('a',))
    prices.loc[prices.date.eq(days[3]),['volume','value']] = 0.
    out = replay_portfolio(prices,picks(days[:1],('a',)),days,config(max_positions=1,horizon=4))
    assert out.metrics['status']=='complete'
    assert out.equity.loc[out.equity.date.eq(days[3]),'stale_positions'].iloc[0]==0
    assert not out.executions.date.eq(days[3]).any()


def test_bad_entry_vwap_cannot_be_replaced_by_the_official_mark():
    prices,days = history(8,('a',))
    prices.loc[prices.date.eq(days[1]),'value'] = np.nan
    out = replay_portfolio(prices,picks(days[:1],('a',)),days,config(max_positions=1,horizon=4))
    assert out.metrics['status']=='invalid_data' and out.metrics['total_return'] is None
    assert out.executions.empty and 'unknown_entry_execution' in set(out.decisions.status)
