"""EOD decisions, delayed real-model disposal and unvalued-rights guards; fixtures stay temporary."""

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import RISK_EXIT_VERSION, replay_portfolio
from tests.test_portfolio import config, history, picks


def shock(n=9):
    prices, days = history(n, ('a',))
    prices.loc[prices.date.ge(days[2]), ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'value']] = [60, 59, 61, 60, 60, 60000]
    prices.loc[prices.date.ge(days[3]), ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'value']] = [62, 61, 63, 62, 62, 62000]
    prices['ref_price_raw'] = prices.close_final_raw.shift().fillna(100.)
    return prices, days


@pytest.mark.parametrize('stress', [False, True])
def test_loss_floor_requests_next_session_exit_before_original_due_date(stress):
    prices, days = shock()
    cfg = config(max_positions=1, horizon=6, max_loss_pct=.15, stress=stress)
    result = replay_portfolio(prices, picks(days, ('a',)), days, cfg, loss_floor_exit=True)
    old = replay_portfolio(prices, picks(days, ('a',)), days, cfg)
    assert result.metrics['risk_exit_request_date'] == days[2]
    sold = result.executions.loc[result.executions.side.eq('sell')]
    assert list(sold.date) == [days[3]] and sold.exit_kind.eq('loss_floor').all()
    assert old.executions.loc[old.executions.side.eq('sell'), 'date'].iloc[0] == days[7]
    assert result.executions.loc[result.executions.side.eq('buy'), 'date'].lt(days[2]).all()
    assert result.metrics['risk_exit_fully_liquidated']
    assert result.assumptions['version'] == RISK_EXIT_VERSION and result.assumptions['base_accounting_version'] == 'portfolio-v4'
    assert not result.assumptions['risk_exit_loss_cap_guaranteed']
    assert result.metrics['total_return'] < -.15  # A request is not a guaranteed loss cap.
    daily = prices.loc[prices.date.eq(days[3])].iloc[0]
    assert daily.low_raw <= sold.iloc[0].price_rial <= daily.high_raw
    assert result.equity.iloc[-1].cash_rial == pytest.approx(result.metrics['final_equity_rial'])


def test_unfinished_session_low_does_not_trigger_retrospective_same_day_stop():
    prices, days = history(7, ('a',))
    prices.loc[prices.date.eq(days[2]), ['low_raw', 'high_raw']] = [50., 110.]
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
                              config(max_positions=1, horizon=5, max_loss_pct=.15), loss_floor_exit=True)
    assert result.metrics['risk_exit_request_date'] is None
    assert result.metrics['risk_exit_fully_liquidated'] is None
    assert 'loss_floor' not in set(result.executions.get('exit_kind', []))


def test_locked_and_missing_sessions_keep_requested_exit_pending():
    prices, days = shock()
    prices = prices.loc[prices.date.ne(days[3])].copy()
    prices.loc[prices.date.eq(days[4]), ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] = [55, 55, 55, 55, 55, 60, 55000]
    prices.loc[prices.date.eq(days[5]), 'ref_price_raw'] = 55
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
                              config(max_positions=1, horizon=7, max_loss_pct=.15), loss_floor_exit=True)
    sells = result.executions.loc[result.executions.side.eq('sell')]
    assert sells.date.iloc[0] == days[5]
    delayed = result.decisions.loc[result.decisions.status.eq('exit_delayed')]
    assert set(delayed.date) == {days[3], days[4]}
    assert result.equity.loc[result.equity.date.eq(days[3]), 'stale_positions'].iloc[0] == 1


def test_capacity_constrained_liquidation_uses_multiple_sessions_and_charges_fees_once():
    prices, days = shock()
    prices.loc[prices.date.eq(days[3]), ['volume', 'value']] = [100., 6200.]
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
                              config(max_positions=1, horizon=7, max_loss_pct=.15), loss_floor_exit=True)
    buys, sells = (result.executions.loc[result.executions.side.eq(side)] for side in ('buy', 'sell'))
    assert sells.iloc[0].date == days[3] and sells.iloc[0].quantity == 5
    assert len(sells) == 2 and sells.quantity.sum() == buys.quantity.sum()
    assert result.metrics['risk_exit_fully_liquidated']
    assert result.metrics['fees_rial'] == pytest.approx(result.executions.fee_rial.sum())
    expected = result.metrics['initial_capital_rial'] - (buys.quantity*buys.price_rial).sum() + (sells.quantity*sells.price_rial).sum() - result.executions.fee_rial.sum()
    assert result.equity.iloc[-1].cash_rial == pytest.approx(expected)


def test_trigger_on_final_session_does_not_fabricate_an_exit():
    prices, days = shock()
    result = replay_portfolio(prices.loc[prices.date.le(days[2])], picks(days[:1], ('a',)), days[:3],
                              config(max_positions=1, horizon=7, max_loss_pct=.15), loss_floor_exit=True)
    assert result.metrics['risk_exit_request_date'] == days[2]
    assert result.metrics['risk_exit_first_sale_date'] is None
    assert result.metrics['risk_exit_sell_executions'] == 0
    assert result.metrics['risk_exit_remaining_positions'] == 1
    assert result.metrics['risk_exit_fully_liquidated'] is False


def test_unvalued_cash_dividend_cannot_trigger_from_a_priced_asset_lower_bound():
    from backtest.cash_dividends import CashDividendBook
    from tests.test_cash_dividend_portfolio import cash_history

    prices, days, event = cash_history()
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
        config(max_positions=1, horizon=12, max_loss_pct=.02),
        cash_dividends=CashDividendBook([event], []), loss_floor_exit=True)
    assert result.equity.equity_rial.isna().any()
    assert result.metrics['risk_exit_request_date'] is None
    assert result.metrics['risk_exit_sell_executions'] == 0
    assert result.metrics['total_return'] is None


def test_uncredited_bonus_rights_cannot_trigger_as_a_known_nav_loss():
    from backtest.bonus_shares import BonusShareBook
    from tests.test_bonus_share_portfolio import market

    prices, days, event = market()
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
        config(max_positions=1, horizon=20, max_loss_pct=.02), bonus_shares=BonusShareBook([event], [], []),
        loss_floor_exit=True)
    assert result.equity.equity_rial.isna().any()
    assert result.metrics['risk_exit_request_date'] is None
    assert result.metrics['total_return'] is None


def test_subscription_book_positions_can_be_disposed_with_shared_capacity():
    from backtest.subscription_rights import SubscriptionRightsBook

    prices, days = shock()
    book = SubscriptionRightsBook([], [], [], [], [])
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
        config(max_positions=1, horizon=7, max_loss_pct=.15), subscription_rights=book, loss_floor_exit=True)
    assert result.metrics['risk_exit_first_sale_date'] == days[3]
    assert result.metrics['risk_exit_fully_liquidated']
    assert result.assumptions['base_accounting_version'] == 'portfolio-subscription-rights-v1'


def test_dip_target_policy_yields_to_a_prior_session_risk_exit_request():
    from research.dip import DipRule

    prices, days = shock()
    prices.loc[prices.date.eq(days[1]), 'open_raw'] = 98.
    prices.loc[prices.date.eq(days[1]), 'low_raw'] = 97.
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
        config(max_positions=1, horizon=6, max_loss_pct=.15),
        dip_rule=DipRule(dip=.02, take_profit=.03, hold=6), loss_floor_exit=True)
    sells = result.executions.loc[result.executions.side.eq('sell')]
    assert sells.iloc[0].date == days[3] and sells.iloc[0].exit_kind == 'loss_floor'


def test_future_prices_and_decisions_cannot_change_prior_risk_action():
    prices, days = shock()
    selected = picks(days, ('a',))
    cfg = config(max_positions=1, horizon=7, max_loss_pct=.15)
    full = replay_portfolio(prices, selected, days, cfg, loss_floor_exit=True)
    cut = days[3]
    short = replay_portfolio(prices.loc[prices.date.le(cut)], selected.loc[selected.date.le(cut)], days[:4], cfg, loss_floor_exit=True)
    pd.testing.assert_frame_equal(full.equity.loc[full.equity.date.le(cut)].reset_index(drop=True), short.equity)
    pd.testing.assert_frame_equal(full.executions.loc[full.executions.date.le(cut)].reset_index(drop=True), short.executions)


@pytest.mark.parametrize('value', [None, 1, 'false', np.bool_(False)])
def test_risk_exit_flag_requires_an_explicit_boolean(value):
    prices, days = history()
    with pytest.raises(ValueError, match='بولی'):
        replay_portfolio(prices, picks(days), days, config(), loss_floor_exit=value)
