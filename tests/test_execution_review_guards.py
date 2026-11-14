"""Keep external-review execution failures out of BRS's shared accounting engine."""

import numpy as np
import pandas as pd
import pytest

from backtest.evidence import audit_daily_replay
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from tests.test_portfolio import config, history, picks


@pytest.mark.parametrize('prepared', [False, True])
def test_locked_terminal_position_is_not_a_realized_sale_or_a_cash_reward(prepared):
    prices, days = history(6, ('a',))
    for index in range(2, len(days)):
        price = 100. * .95**(index-1)
        mask = prices.date.eq(days[index])
        prices.loc[mask, ['open_raw','high_raw','low_raw','close_raw','close_final_raw']] = price
        prices.loc[mask, 'ref_price_raw'] = 100. * .95**(index-2)
        prices.loc[mask, 'value'] = prices.loc[mask, 'volume']*price
    tape = picks(days[:1], ('a',))
    source = prepare_portfolio_prices(prices) if prepared else prices
    result = replay_portfolio(source, tape, days, config(max_positions=1),
                              no_trade_valuation_observations=[], require_explicit_corporate_actions=True)
    assert result.executions.side.tolist() == ['buy']
    assert result.metrics['open_positions'] == 1 and result.equity.iloc[-1].positions == 1
    assert result.equity.cash_rial.iloc[1:].nunique() == 1
    assert result.equity.iloc[-1].equity_rial > result.equity.iloc[-1].cash_rial
    audit = audit_daily_replay(result, prices, tape, days, no_trade_valuation_observations=[])
    assert audit['modeled_cash_return'] is None and audit['blockers']


@pytest.mark.parametrize('prepared', [False, True])
def test_entry_day_mark_includes_price_move_and_separate_buy_sell_fees(prepared):
    prices, days = history(6, ('a',))
    prices['volume'], prices['value'] = 1_000_000., 100_000_000.
    row = prices.date.eq(days[1])
    prices.loc[row, ['low_raw','close_raw','close_final_raw']] = [49.,50.,50.]
    prices.loc[prices.date.eq(days[2]), 'ref_price_raw'] = 50.
    source = prepare_portfolio_prices(prices) if prepared else prices
    result = replay_portfolio(source, picks(days[:1], ('a',)), days, config(max_positions=1,horizon=3))
    bought, = result.executions.loc[result.executions.side.eq('buy')].to_dict('records')
    at = result.equity.loc[result.equity.date.eq(days[1])].iloc[0]
    cost = bought['quantity']*bought['price_rial']*(1+settings.FEES['buy'])
    expected_cash = 10000.-cost
    assert at.cash_rial == pytest.approx(expected_cash)
    assert at.equity_rial == pytest.approx(expected_cash+bought['quantity']*50.*(1-settings.FEES['sell']))
    assert result.metrics['max_drawdown'] > .4


@pytest.mark.parametrize('allocation', [None, .30])
def test_execution_day_close_cannot_change_an_earlier_callback_or_pending_entry(allocation):
    prices, days = history(6, ('a','b'))
    prices.loc[prices.symbol.eq('a') & prices.date.eq(days[2]), 'low_raw'] = 49.
    altered = prices.copy(deep=True)
    altered.loc[altered.symbol.eq('a') & altered.date.eq(days[2]), ['close_raw','close_final_raw']] = 50.
    altered.loc[altered.symbol.eq('a') & altered.date.eq(days[3]), 'ref_price_raw'] = 50.
    runs, states = [], []
    for frame in (prices, altered):
        seen = []
        def policy(state):
            seen.append(state)
            return ['a'] if state['date']==days[0] else ['b'] if state['date']==days[1] else []
        result = replay_portfolio(frame, picks(days[:0], ('a','b')), days, config(horizon=4),
                                  entry_allocation_fraction=allocation, decision_policy=policy)
        runs.append(result.executions.loc[result.executions.side.eq('buy')].reset_index(drop=True))
        states.append([state for state in seen if state['date']<days[2]])
    assert states[0] == states[1]
    assert runs[0].symbol.tolist() == runs[1].symbol.tolist() == ['a','b']
    pd.testing.assert_frame_equal(runs[0],runs[1])


def test_missing_mark_after_a_loss_cannot_restore_original_principal():
    prices, days = history(7, ('a',))
    row = prices.date.eq(days[2])
    prices.loc[row, ['low_raw','close_raw','close_final_raw']] = [49.,50.,50.]
    prices.loc[prices.date.eq(days[3]), 'ref_price_raw'] = 50.
    prices = prices.loc[~prices.date.eq(days[3])]
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days, config(max_positions=1,horizon=4),
                              no_trade_valuation_observations=[], require_explicit_corporate_actions=True)
    assert result.metrics['total_return'] is None and result.metrics['max_drawdown'] is None
    assert np.isnan(result.equity.iloc[-1].equity_rial)
    assert result.equity.iloc[-1].date == days[3]
    assert result.executions.side.tolist() == ['buy']


@pytest.mark.parametrize('allocation', [None, .30])
def test_same_session_sale_proceeds_cannot_resize_a_previously_reserved_buy(allocation):
    prices, days = history(6, ('a','b'))
    prices['volume'], prices['value'] = 1_000_000., 100_000_000.
    selections = pd.DataFrame([(days[0],'a',0),(days[1],'b',0)],columns=['date','symbol','rank'])
    changed = prices.copy(deep=True)
    at = changed.symbol.eq('a') & changed.date.eq(days[2])
    changed.loc[at,['high_raw','close_raw','close_final_raw','value']] = [151.,150.,150.,150_000_000.]
    changed.loc[changed.symbol.eq('a') & changed.date.eq(days[3]),'ref_price_raw'] = 150.
    results=[]
    for frame in (prices,changed):
        results.append(replay_portfolio(frame,selections,days,config(horizon=1),
                                        entry_allocation_fraction=allocation))
    before,after=(result.executions.loc[result.executions.symbol.eq('b') & result.executions.side.eq('buy')]
                  .reset_index(drop=True) for result in results)
    assert len(before)==len(after)==1
    pd.testing.assert_frame_equal(before,after)
    event_day=results[1].executions.loc[results[1].executions.date.eq(days[2])]
    # Safe daily replay convention: already reserved buys precede same-day sale
    # proceeds. It does not claim a real intraday execution sequence was observed.
    assert list(zip(event_day.side,event_day.symbol))==[('buy','b'),('sell','a')]
