"""One accounting engine for fixed tapes and causal interactive policies."""

import pandas as pd
import pytest

from backtest.portfolio import replay_portfolio
from tests.test_portfolio import config, history, picks


def test_fixed_tape_and_callback_have_identical_cash_fees_fills_and_returns():
    prices, days = history(20, ('a', 'b'))
    tape = picks(days, ('a', 'b'))
    static = replay_portfolio(prices, tape, days, config())
    seen = []
    def policy(state):
        seen.append(state)
        assert set(state) == {'date','equity_rial','cash_rial','positions','stale_positions',
                              'stale_fractional_exposures','stopped_new_entries','holdings'}
        return ['a', 'b']
    live = replay_portfolio(prices, tape.iloc[:0], days, config(), decision_policy=policy)
    assert live.metrics == static.metrics
    pd.testing.assert_frame_equal(live.equity, static.equity)
    pd.testing.assert_frame_equal(live.executions, static.executions)
    assert [s['date'] for s in seen] == list(days[:-1])


@pytest.mark.parametrize('bad', [None, ['a','a'], ['a','b','c'], ['ك'], [True]])
def test_policy_contract_rejects_unbounded_or_ambiguous_actions(bad):
    prices, days = history()
    with pytest.raises(ValueError, match='Policy'):
        replay_portfolio(prices,picks(days).iloc[:0],days,config(),decision_policy=lambda _:bad)


def test_policy_cannot_mutate_the_live_cash_book_via_its_projection():
    prices, days = history()
    def policy(state):
        state['cash_rial'] = 1e99
        return []
    live = replay_portfolio(prices,picks(days).iloc[:0],days,config(),decision_policy=policy)
    assert live.metrics['total_return'] == 0
    assert live.equity.cash_rial.eq(10000).all()
