"""Integer-lot targets must agree with independent shared-engine cash accounting."""

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from research.sized_dip_outcomes import RULE, sized_dip_attempt_outcomes
from tests.test_dip_outcomes import sample

BUDGET = 50_000


def labels(data, decisions, days, **kwargs):
    return sized_dip_attempt_outcomes(data, decisions, days, budget_rial=BUDGET, **kwargs)


@pytest.mark.parametrize('stress', [False, True])
@pytest.mark.parametrize('entry_kind', ['open', 'limit'])
@pytest.mark.parametrize('partial', [False, True])
def test_integer_cash_and_exit_dates_agree_with_portfolio_oracle(stress, entry_kind, partial):
    data, decisions, days = sample()
    if entry_kind == 'limit':
        data.loc[1, 'open_raw'] = 99.
    if partial:
        data.loc[2, ['volume', 'value']] = [200., 20000.]
        data.loc[3, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'value']] = [90., 89., 92., 90., 900000.]
    row = labels(data, decisions, days).iloc[0]
    replay = replay_portfolio(data, decisions, days, PortfolioConfig(initial_capital_rial=BUDGET,
        horizon=RULE.hold, max_positions=1, stress=stress, max_loss_pct=.9999),
        dip_rule=RULE, entry_allocation_fraction=1.)
    buy = replay.executions.loc[replay.executions.side.eq('buy')].iloc[0]
    sold = replay.executions.loc[replay.executions.side.eq('sell')]
    prefix = 'stress' if stress else 'normal'
    assert row.state == 'complete'
    assert row[prefix+'_entry_quantity'] == buy.quantity
    assert row[prefix+'_entry_quantity'] > 1
    assert row[prefix+'_exit_rows'] == len(sold)
    assert row[prefix+'_entry_date'] == buy.date
    assert row[prefix+'_exit_date'] == sold.date.iloc[-1] > buy.date
    assert row[prefix+'_entry_price'] == buy.price_rial
    cost = buy.quantity*buy.price_rial+buy.fee_rial
    proceeds = (sold.quantity*sold.price_rial-sold.fee_rial).sum()
    assert row[prefix+'_entry_cost_rial'] == pytest.approx(cost, abs=1e-9)
    assert row[prefix+'_proceeds_rial'] == pytest.approx(proceeds, abs=1e-9)
    assert row[prefix+'_observed_net'] == pytest.approx((proceeds-cost)/BUDGET, abs=1e-14)
    assert replay.metrics['total_return'] == pytest.approx(row[prefix+'_observed_net'], abs=1e-14)
    assert row.floor_net == min(row.normal_observed_net, row.stress_observed_net)


def test_reserved_unused_cash_is_not_silently_removed_from_return_denominator():
    data, decisions, days = sample()
    row = sized_dip_attempt_outcomes(data, decisions, days, budget_rial=1_000_000).iloc[0]
    stake_return = row.normal_proceeds_rial/row.normal_entry_cost_rial-1
    assert row.normal_entry_cost_rial < 1_000_000/10
    assert row.normal_net == pytest.approx(stake_return*row.normal_entry_cost_rial/1_000_000)
    assert row.normal_net != pytest.approx(stake_return)


@pytest.mark.parametrize('cause', ['missing_entry', 'missing_exit', 'invalid_open', 'corporate', 'residual'])
def test_unresolved_cash_and_partial_disposals_never_become_a_zero(cause):
    data, decisions, days = sample()
    if cause == 'missing_entry':
        data = data.drop(index=1)
    elif cause == 'missing_exit':
        data = data.drop(index=2)
    elif cause == 'invalid_open':
        data.loc[2, 'open_raw'] = 0.
    elif cause == 'corporate':
        data.loc[2, 'ref_price_raw'] = 50.
    else:
        data.loc[2:, ['volume', 'value']] = [200., 20000.]
    row = labels(data, decisions, days).iloc[0]
    assert row.state == 'unknown' and row.resolved_at is None
    assert np.isnan(row.normal_net) and np.isnan(row.floor_net)
    if cause == 'residual':
        assert row.normal_proceeds_rial > 0
        assert row.normal_remaining_quantity > 0 and row.normal_exit_rows == 3
        assert row.normal_reason == 'censored_remaining_quantity'


@pytest.mark.parametrize('cause', ['limit', 'capacity', 'budget', 'locked'])
def test_only_known_coherent_unfilled_attempts_have_zero_return(cause):
    data, decisions, days = sample()
    budget = BUDGET
    if cause == 'limit':
        data.loc[1, ['open_raw', 'low_raw', 'value']] = [100., 99., 1_000_000.]
    elif cause == 'capacity':
        data.loc[0, ['volume', 'value']] = [10., 1000.]
    elif cause == 'budget':
        budget = 1
    else:
        data.loc[1, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'value']] = [103., 103., 103., 103., 103., 1_030_000.]
    row = sized_dip_attempt_outcomes(data, decisions, days, budget_rial=budget).iloc[0]
    assert row.state == 'entry_blocked' and row.normal_net == row.floor_net == 0
    assert row.normal_entry_quantity == 0 and row.resolved_at == days[1]


def test_current_entry_reference_change_is_allowed_but_later_unneeded_data_does_not_rewrite_result():
    data, decisions, days = sample()
    data.loc[1, ['open_raw','low_raw','high_raw','close_raw','close_final_raw','ref_price_raw','value']] = [48.,47.,51.,50.,50.,50.,490000.]
    data.loc[2:, ['open_raw','low_raw','high_raw','close_raw','close_final_raw','ref_price_raw','value']] = [50.,49.,52.,50.,50.,50.,500000.]
    original = data.copy(deep=True)
    before = labels(data, decisions, days)
    assert before.state.eq('complete').all()
    pd.testing.assert_frame_equal(data, original)
    data.loc[4, 'ref_price_raw'] = 1.
    pd.testing.assert_frame_equal(before, labels(data, decisions, days), check_exact=True)
    pd.testing.assert_frame_equal(before, labels(prepare_portfolio_prices(original), decisions, days), check_exact=True)


@pytest.mark.parametrize('budget', [True, 0, -1, 1.5, 2**53, None])
def test_budget_requires_exact_whole_rial_amount(budget):
    data, decisions, days = sample()
    with pytest.raises(ValueError):
        sized_dip_attempt_outcomes(data, decisions, days, budget_rial=budget)


@pytest.mark.parametrize('change', ['sealed', 'duplicate', 'future_column', 'bad_volume', 'bad_fee', 'bad_participation'])
def test_scope_quality_and_execution_contract_fail_closed(change):
    data, decisions, days = sample()
    kwargs = {}
    if change == 'sealed':
        days = np.append(days, '2025-03-21')
    elif change == 'duplicate':
        decisions = pd.concat([decisions, decisions])
    elif change == 'future_column':
        decisions = decisions.assign(net_ret_1d=1.)
    elif change == 'bad_volume':
        data.loc[0, 'volume'] = 10000.5
        assert labels(data, decisions, days).state.eq('unknown').all()
        return
    elif change == 'bad_fee':
        kwargs['fees'] = {'buy': True, 'sell': .01}
    else:
        kwargs['participation'] = .06
    with pytest.raises(ValueError):
        labels(data, decisions, days, **kwargs)
