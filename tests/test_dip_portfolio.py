"""The fixed DIP rule uses actual shared cash; synthetic input stays in temporary tests."""

from dataclasses import replace

import numpy as np
import pytest

from backtest.portfolio import replay_portfolio
from research.dip import DipRule
from tests.test_portfolio import config, history, picks


def replay(prices, calendar, rule=None, **options):
    return replay_portfolio(prices, picks(calendar[:1], tuple(prices.symbol.unique())), calendar,
                            config(horizon=1, **options), dip_rule=rule or DipRule())


def dip_history():
    prices, calendar = history(5, ('a',))
    prices['volume'] = 100000
    prices['value'] = prices['volume'] * 100.
    prices.loc[1, ['low_raw', 'high_raw', 'open_raw']] = [97.5, 102., 100.]
    return prices, calendar


def test_conservative_dip_cannot_use_the_unknown_order_of_high_and_low():
    prices, calendar = dip_history()
    conservative = replay(prices, calendar, max_positions=1)
    optimistic = replay(prices, calendar, rule=replace(DipRule(), conservative=False), max_positions=1, allow_entry_day_exit=True)
    assert conservative.executions.iloc[0].price_rial == 98
    assert conservative.executions.loc[conservative.executions.side == 'sell', 'date'].tolist() == [calendar[2]]
    assert optimistic.executions.loc[optimistic.executions.side == 'sell', 'date'].tolist() == [calendar[1]]
    assert conservative.equity.cash_rial.ge(-1e-8).all()


def test_gap_entry_can_reach_a_target_after_the_open():
    prices, calendar = dip_history()
    prices.loc[1, 'open_raw'] = 97.5
    result = replay(prices, calendar, max_positions=1, allow_entry_day_exit=True)
    assert result.executions.side.tolist() == ['buy', 'sell']
    assert result.executions.date.tolist() == [calendar[1], calendar[1]]
    assert result.executions.iloc[0].price_rial == 97.5


def test_default_research_does_not_assume_historical_entry_day_sale_permission():
    prices, calendar = dip_history()
    prices.loc[1, 'open_raw'] = 97.5
    result = replay(prices, calendar, max_positions=1)
    assert result.executions.loc[result.executions.side == 'sell', 'date'].tolist() == [calendar[2]]
    assert not result.assumptions['historical_same_day_resale_permissions_verified']


def test_not_filled_limit_keeps_cash_and_does_not_retry_a_stale_order():
    prices, calendar = history(5, ('a',))
    prices.loc[2, 'low_raw'] = 97.5
    result = replay(prices, calendar, max_positions=1)
    assert result.executions.empty and result.equity.cash_rial.eq(10000).all()
    assert 'limit_not_filled' in set(result.decisions.status)


def test_touching_the_limit_is_insufficient_in_the_conservative_model():
    prices, calendar = dip_history()
    prices.loc[1, 'low_raw'] = 98
    assert replay(prices, calendar).executions.empty


def test_dip_shared_cash_and_cumulative_capacity_cover_both_sides():
    prices, calendar = history(5, ('a', 'b', 'c'))
    prices.loc[prices.date == calendar[1], ['low_raw', 'high_raw']] = [97.5, 102.]
    result = replay(prices, calendar, rule=replace(DipRule(), conservative=False))
    assert result.equity.positions.le(2).all() and result.equity.cash_rial.ge(-1e-6).all()
    for (_, symbol), events in result.executions.groupby(['date', 'symbol']):
        assert events.quantity.sum() <= 50
        assert (events.quantity * events.price_rial).sum() <= 5000 + 1e-6


def test_requested_quantity_does_not_grow_when_future_fill_improves():
    prices, calendar = dip_history()
    prices.loc[1, ['low_raw', 'open_raw']] = [90., 90.]
    result = replay(prices, calendar, max_positions=1)
    buy = result.executions.loc[result.executions.side == 'buy'].iloc[0]
    # The current reference buy limit is 98. A favorable open does not use the future low
    # to turn the previous day's cash reservation into a larger order.
    assert buy.quantity == int(10000 / (98 * (1 + result.assumptions['fees']['buy'])))


def test_dip_exit_queue_keeps_the_cash_tied_up():
    prices, calendar = dip_history()
    prices.loc[1, 'high_raw'] = 100.5
    prices.loc[2, ['low_raw', 'high_raw', 'open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] = [97., 97., 97., 97., 97., 100., 9700000.]
    prices.loc[3, 'ref_price_raw'] = 97
    result = replay(prices, calendar, max_positions=1)
    assert calendar[2] not in set(result.executions.loc[result.executions.side == 'sell', 'date'])
    assert 'exit_delayed' in set(result.decisions.status)
    assert result.equity.loc[result.equity.date == calendar[2], 'positions'].iloc[0] == 1


def test_dip_stress_drops_targets_without_buying_above_the_limit():
    prices, calendar = dip_history()
    prices.loc[1, 'open_raw'] = 97.5
    result = replay(prices, calendar, max_positions=1, stress=True)
    assert result.executions.iloc[0].price_rial == 98
    sale = result.executions.loc[result.executions.side == 'sell'].iloc[0]
    assert sale.date == calendar[2] and sale.price_rial == prices.loc[2, 'low_raw']


def test_dip_unknown_held_reference_invalidates_the_portfolio_return():
    prices, calendar = dip_history()
    prices.loc[2, 'ref_price_raw'] = np.nan
    result = replay(prices, calendar, max_positions=1)
    assert result.metrics['total_return'] is None and result.metrics['status'] == 'invalid_data'


def test_fractional_action_entitlement_is_never_sold_as_a_share_or_reused_as_cash():
    prices, calendar = history(5, ('a',))
    prices.loc[2:, ['low_raw', 'high_raw', 'open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] = [79., 81., 80., 80., 80., 80., 80000.]
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config(max_positions=1))
    assert result.executions.quantity.map(lambda value: float(value).is_integer()).all()
    assert result.metrics['noncash_receivable_rial'] > 0
    assert not result.metrics['actual_corporate_entitlements_verified']
    final = result.equity.iloc[-1]
    assert final.equity_rial == pytest.approx(final.cash_rial + result.metrics['noncash_receivable_rial'])


def test_unsupported_dip_stop_or_horizon_is_rejected_explicitly():
    prices, calendar = dip_history()
    with pytest.raises(ValueError):
        replay(prices, calendar, rule=DipRule(hold=2))
    with pytest.raises(ValueError):
        replay(prices, calendar, rule=DipRule(stop_loss=.02))
