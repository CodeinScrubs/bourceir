"""ارزش‌گذاری کسر مصنوعی سهم؛ داده‌ها فقط در حافظه آزمون هستند."""

import numpy as np
import pytest

from backtest.engine import BacktestEngine
from backtest.portfolio import replay_portfolio
from config import settings
from tests.test_portfolio import config, history, picks


def fractional_history():
    prices, calendar = history(6, ('a',))
    prices['volume'] = 40.
    prices['value'] = prices.volume * 100
    for index, (ref, mark) in enumerate([(60, 60), (60, 120), (30, 30), (30, 60)], start=2):
        prices.loc[index, 'volume'] = 100
        prices.loc[index, ['ref_price_raw', 'close_final_raw', 'close_raw', 'open_raw',
                           'low_raw', 'high_raw', 'value']] = [ref, mark, mark, mark, mark - 1, mark + 1, mark * 100]
    return prices, calendar


def replay(prices, calendar):
    return replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar,
                            config(max_positions=1, slippage_bps=0))


def test_fraction_follows_price_and_later_action_after_whole_shares_are_sold():
    prices, calendar = fractional_history()
    result = replay(prices, calendar)
    net = 1 - settings.FEES['sell']
    assert result.executions.query("side == 'buy'").quantity.tolist() == [2]
    assert result.executions.query("side == 'sell'").quantity.sum() == 3
    # 2 / 0.6 = 3 + 1/3. Later 120 -> 30 multiplies the remaining exposure by four.
    assert result.equity.noncash_receivable_rial.tolist() == pytest.approx([0, 0, 20 * net, 40 * net, 40 * net, 80 * net])
    assert result.equity.cash_rial.iloc[2:].nunique() == 1
    assert result.metrics['noncash_receivable_cost_rial'] == pytest.approx(20 * (1 + settings.FEES['buy']))
    assert result.metrics['corporate_action_events'] == 2
    assert not result.metrics['actual_corporate_entitlements_verified']
    assert result.assumptions['version'] == 'portfolio-v4'
    assert result.metrics['fractional_exposures'][0]['quantity'] == pytest.approx(4 / 3)
    assert result.metrics['final_equity_rial'] - result.metrics['initial_capital_rial'] == pytest.approx(
        result.metrics['realized_net_rial'] + result.metrics['noncash_receivable_rial']
        - result.metrics['noncash_receivable_cost_rial'])


@pytest.mark.parametrize('column', ['ref_price_raw', 'close_final_raw'])
def test_fraction_only_unknown_reference_or_mark_invalidates_return(column):
    prices, calendar = fractional_history()
    prices.loc[3, column] = np.nan
    result = replay(prices, calendar)
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None


def test_missing_session_retains_fraction_mark_and_reports_it_as_stale():
    prices, calendar = fractional_history()
    prices = prices.drop(index=[3, 4, 5])
    result = replay(prices, calendar)
    assert result.equity.stale_fractional_exposures.tolist() == [0, 0, 0, 1, 1, 1]
    assert result.metrics['stale_mark_days'] == 3
    assert result.equity.noncash_receivable_rial.iloc[2:].nunique() == 1


def test_fraction_value_uses_only_observed_prefix():
    prices, calendar = fractional_history()
    full, short = replay(prices, calendar), replay(prices.head(4), calendar[:4])
    assert full.equity.iloc[:4].to_dict('records') == short.equity.to_dict('records')


@pytest.mark.parametrize('final_mark', [300, 1200])
def test_legacy_fraction_tracks_new_price_and_second_action_without_spendable_cash(final_mark):
    engine = BacktestEngine(initial_capital=100_000, default_slippage_bps=0)
    engine.execute_buy('آزمون', 1000, 2, '2024-01-01')
    engine.apply_corporate_action('آزمون', 600, '2024-01-02')
    engine.execute_sell('آزمون', 600, 3, '2024-01-02')
    cash, cost = engine.cash, engine.corporate_action_receivable_cost
    engine.last_prices['آزمون'] = final_mark
    assert engine.corporate_action_receivable == pytest.approx(final_mark / 3)
    engine.apply_corporate_action('آزمون', final_mark / 4, '2024-01-03')
    assert engine.corporate_action_receivable == pytest.approx(final_mark / 3)
    assert engine.cash == cash and engine.corporate_action_receivable_cost == cost
    assert engine.positions['آزمون'].quantity == 0


@pytest.mark.parametrize('column', ['ref_price_raw', 'close_final_raw'])
def test_legacy_fraction_only_unknown_data_stops_replay(column):
    prices, _ = fractional_history()
    prices = prices.drop(columns='symbol')
    prices.loc[2, ['ref_price_raw', 'close_raw', 'close_final_raw', 'low_raw', 'high_raw', 'value']] = [200, 200, 200, 199, 201, 8000]
    prices.loc[3, column] = np.nan
    engine = BacktestEngine(initial_capital=100_000, default_slippage_bps=0)
    engine.execute_buy('آزمون', 100, 1, '2023-12-31')
    with pytest.raises(ValueError, match='نامعلوم'):
        engine.run_replay_simulation(prices, 'آزمون', lambda *_: None)
