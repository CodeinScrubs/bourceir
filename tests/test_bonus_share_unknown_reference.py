"""A tiny unexplained reference gap is still unknown; fictional prices only in tests."""

from dataclasses import replace

from backtest.bonus_shares import replay_bonus_share_portfolio
from tests.test_portfolio import config, history, picks


def test_small_unexplained_reference_gap_cannot_make_a_known_bonus_path_return():
    prices, days = history(10, ('a',))
    prices[['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 500000.
    prices['low_raw'], prices['high_raw'] = 499999., 500001.
    prices['value'] = prices.volume*500000.
    prices.loc[prices.date.eq(days[3]), 'ref_price_raw'] = 499999.
    result = replay_bonus_share_portfolio(prices, picks(days[:1], ('a',)), days,
        replace(config(), initial_capital_rial=1_000_000_000, horizon=5, max_positions=1), events=[])
    assert result.metrics['status'] == 'invalid_data'
    assert result.metrics['total_return'] is None
    assert 'unknown_corporate_entitlement' in set(result.decisions.status)
