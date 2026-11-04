"""Decision-day cash reserves must not use future marks or hide stale exposure."""

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import ENTRY_ALLOCATION_VERSION, replay_portfolio
from config import settings
from tests.test_portfolio import config, history, picks


def test_joint_pending_budgets_keep_the_declared_decision_day_reserve():
    prices, days = history(6, ('a', 'b', 'c', 'd', 'e'))
    prices['volume'] = 1000000.
    prices['value'] = 100000000.
    cfg = config(initial_capital_rial=1000000, max_positions=5, horizon=3)
    out = replay_portfolio(prices, picks(days[:1], ('a','b','c','d','e')), days, cfg,
        entry_allocation_fraction=.30)
    buys = out.executions.loc[out.executions.side.eq('buy')]
    assert len(buys) == 5
    costs = buys.quantity*buys.price_rial*(1+settings.FEES['buy'])
    assert costs.sum() <= 300000 and costs.max() <= 60000
    assert out.equity.loc[out.equity.date.eq(days[1]), 'cash_rial'].iloc[0] >= 700000
    assert out.assumptions['entry_allocation_version'] == ENTRY_ALLOCATION_VERSION
    assert out.assumptions['allocation_is_guaranteed_ongoing_weight'] is False


def test_future_held_position_mark_cannot_resize_an_already_pending_buy():
    prices, days = history(5, ('a','b'))
    selected = pd.DataFrame([(days[0],'a',0),(days[1],'b',0)], columns=['date','symbol','rank'])
    cfg = config(horizon=10)
    ordinary = replay_portfolio(prices, selected, days, cfg, entry_allocation_fraction=.30)
    changed = prices.copy()
    at = changed.symbol.eq('a') & changed.date.eq(days[2])
    for field in ['low_raw','high_raw','open_raw','close_raw','close_final_raw']:
        changed.loc[at,field] *= 1.2
    changed.loc[at,'value'] *= 1.2
    changed.loc[changed.symbol.eq('a') & changed.date.eq(days[3]),'ref_price_raw'] = 120
    future = replay_portfolio(changed, selected, days, cfg, entry_allocation_fraction=.30)
    before = ordinary.executions.loc[ordinary.executions.symbol.eq('b') & ordinary.executions.side.eq('buy')]
    after = future.executions.loc[future.executions.symbol.eq('b') & future.executions.side.eq('buy')]
    assert len(before) == len(after) == 1
    pd.testing.assert_frame_equal(before.reset_index(drop=True), after.reset_index(drop=True))


def test_missing_held_mark_pauses_new_decisions_and_exposure_stays_unknown():
    prices, days = history(6, ('a','b'))
    prices = prices.loc[~(prices.symbol.eq('a') & prices.date.eq(days[2]))]
    selected = pd.DataFrame([(days[0],'a',0),(days[2],'b',0)], columns=['date','symbol','rank'])
    out = replay_portfolio(prices, selected, days, config(horizon=10), entry_allocation_fraction=.30)
    assert not out.executions.symbol.eq('b').any()
    assert 'allocation_unknown_stale_marks' in set(out.decisions.status)
    row = out.equity.loc[out.equity.date.eq(days[2])].iloc[0]
    assert row['stale_positions'] == 1 and pd.isna(row['known_liquidation_proxy_invested_fraction'])
    assert out.metrics['allocation_unknown_days'] >= 1


def test_market_price_drift_is_reported_instead_of_claiming_a_hard_weight_cap():
    prices, days = history(5, ('a',))
    mask = prices.date.ge(days[2])
    for field in ['low_raw','high_raw','open_raw','close_raw','close_final_raw']:
        prices.loc[mask,field] *= 1.5
    prices.loc[mask,'value'] *= 1.5
    prices.loc[prices.date.gt(days[2]),'ref_price_raw'] = 150
    out = replay_portfolio(prices,picks(days[:1],('a',)),days,
        config(max_positions=1,horizon=10),entry_allocation_fraction=.30)
    assert out.metrics['maximum_known_invested_fraction'] > .30
    assert out.metrics['known_allocation_drift_days'] > 0
    assert out.assumptions['allocation_price_drift_rebalanced'] is False
    assert not out.metrics['actual_economic_exposure_verified']


def test_omitting_the_optional_policy_retains_the_existing_result_schema_and_values():
    prices, days = history()
    ordinary = replay_portfolio(prices,picks(days),days,config())
    explicit = replay_portfolio(prices,picks(days),days,config(),entry_allocation_fraction=None)
    for name in ('equity','executions','decisions'):
        pd.testing.assert_frame_equal(getattr(ordinary,name),getattr(explicit,name))
    assert ordinary.metrics == explicit.metrics and ordinary.assumptions == explicit.assumptions
    assert 'entry_allocation_version' not in ordinary.assumptions
    assert 'known_liquidation_proxy_invested_fraction' not in ordinary.equity


@pytest.mark.parametrize('fraction', [True,False,0,-.1,1.1,np.nan,np.inf])
def test_invalid_allocation_is_rejected_without_truncation(fraction):
    prices, days = history()
    with pytest.raises(ValueError):
        replay_portfolio(prices,picks(days),days,config(),entry_allocation_fraction=fraction)
