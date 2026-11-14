from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from config import settings
from core.price_levels import Swing, _make_zones, prepare_level_history, price_map_at
from research.price_level_audit import HORIZON, observed_reaction, run_audit


def level_prices(values):
    close = pd.Series(values, dtype=float)
    return pd.DataFrame({
        'date': pd.date_range('2024-01-01', periods=len(close)).strftime('%Y-%m-%d'),
        'open_raw': close,
        'high_raw': close+2,
        'low_raw': close-2,
        'close_raw': close,
        'close_final_raw': close,
        'ref_price_raw': close.shift(fill_value=close.iloc[0]),
        'volume': 1000,
        'value': close*1000,
        'trade_count': 42,
    })


def cyclical_prices():
    return level_prices([100, 96, 90, 96, 100, 104, 110, 104, 100] * 6 + [100])


def test_nearest_supported_zones_have_independent_reactions_and_exact_units():
    prices = cyclical_prices()
    result = price_map_at(prepare_level_history(prices), prices.date.iloc[-1])
    assert result.state == 'ready'
    assert result.anchor_rial == 100
    support, resistance = result.zones
    assert support.side == 'support' and support.lower_rial <= 88 <= support.upper_rial < 100
    assert resistance.side == 'resistance' and 100 < resistance.lower_rial <= 112 <= resistance.upper_rial
    for zone, expected in ((support, 5), (resistance, 6)):
        assert zone.episodes == zone.same_role_episodes == expected
        assert zone.last_confirmation <= result.date
    assert (result.recent_low_rial, result.recent_high_rial) == (88, 112)
    assert support.distance_pct == pytest.approx(100 - support.upper_rial)


def test_unconfirmed_peak_and_future_appends_do_not_change_prior_map():
    prices = level_prices([*range(100, 131), 127, 124, 121, 118, 115, 151])
    peak = prices.date.iloc[30]
    after_two = prices.date.iloc[32]
    history = prepare_level_history(prices)
    past = price_map_at(prepare_level_history(prices.iloc[:31]), peak)
    assert past == price_map_at(history, peak)
    assert all(zone.side != 'resistance' for zone in past.zones)
    new = next(zone for zone in price_map_at(history, after_two).zones if zone.side == 'resistance')
    assert new.lower_rial <= 132 <= new.upper_rial
    assert new.last_reaction == peak and new.last_confirmation == after_two


def test_unknown_sessions_cannot_confirm_a_swing_or_cross_a_long_gap():
    prices = level_prices([*range(100, 131), 127, 124, 121, 118, 115])
    market = prices.date.tolist()
    missing = prices.drop(index=31).copy()
    # Keep the price basis coherent so this exercises the missing-session guard.
    missing.loc[32, 'ref_price_raw'] = missing.loc[30, 'close_final_raw']
    result = price_map_at(prepare_level_history(missing, market), market[-1])
    assert result.skipped_sessions == 1
    assert all(zone.side != 'resistance' for zone in result.zones)

    gap = prices.drop(index=range(15, 22)).copy()
    gap.loc[22, 'ref_price_raw'] = gap.loc[14, 'close_final_raw']
    result = price_map_at(prepare_level_history(gap, market), market[-1])
    assert result.state == 'short_history'
    assert result.boundary == 'long_gap'
    assert result.window_start == market[22]


def test_reference_reset_discards_old_price_scale_and_ignores_adjusted_columns():
    prices = cyclical_prices()
    columns = [col for col in prices if col.endswith('_raw')] + ['value']
    prices.loc[27:, columns] *= 2
    history = prepare_level_history(prices)
    result = price_map_at(history, prices.date.iloc[-1])
    assert result.window_start == prices.date.iloc[27]
    assert result.boundary == 'reference_change'
    assert result.zones
    assert all(zone.lower_rial > 160 for zone in result.zones)
    prices['adj_close'], prices['adjustment_factor'] = 999999, .001
    assert price_map_at(prepare_level_history(prices), prices.date.iloc[-1]) == result


@pytest.mark.parametrize('field,value', [
    ('ref_price_raw', None), ('close_raw', 999), ('trade_count', 0), ('volume', 0), ('value', 9),
    ('price_unit', 'Toman'), ('source', 'legacy'),
])
def test_latest_invalid_bar_cannot_silently_fall_back_to_old_prices(field, value):
    prices = cyclical_prices()
    if field == 'price_unit':
        prices[field] = 'Rial'
    if field == 'source':
        prices[field] = 'tsetmc'
    prices.at[prices.index[-1], field] = value
    result = price_map_at(prepare_level_history(prices), prices.date.iloc[-1])
    assert result.state == 'invalid_latest'
    assert result.date == prices.date.iloc[-1] and result.anchor_rial is None and not result.zones


def test_invalid_previous_row_breaks_history_but_official_close_may_be_outside_the_range():
    prices = cyclical_prices()
    prices.at[39, 'volume'] = 0
    prices.at[54, 'close_final_raw'] = 94
    result = price_map_at(prepare_level_history(prices), prices.date.iloc[-1])
    assert result.state == 'short_history'
    assert result.boundary == 'invalid_previous'
    assert result.bars == 15


def test_price_range_disjoint_even_for_chained_nearby_pivots():
    prices = [
        Swing(3, 5, 100, 'resistance', False),
        Swing(11, 16, 106, 'resistance', True),
        Swing(4, 6, 112, 'support', False),
    ]
    dates = pd.date_range('2024-01-01', periods=21).strftime('%Y-%m-%d')
    one, two = _make_zones(prices, dates, 119, 10)
    assert one.upper_rial < two.lower_rial
    assert one.side == two.side == 'support'
    assert one.episodes == 2 and one.same_role_episodes == 0 and one.broad_episodes == 1
    assert two.episodes == two.same_role_episodes == 1
    assert _make_zones(prices, dates, 105, 10)[0].same_role_episodes is None


def test_flat_window_and_duplicate_dates_fail_closed():
    prices = level_prices([100] * 28)
    prices['high_raw'] = prices['low_raw'] = 100
    assert price_map_at(prepare_level_history(prices), prices.date.iloc[-1]).state == 'flat_history'
    prices.loc[1, 'date'] = prices.loc[0, 'date']
    with pytest.raises(ValueError, match='تاریخچه'):
        prepare_level_history(prices)


def bar(opening, low, high, close, boundary=''):
    return SimpleNamespace(open_raw=opening, low_raw=low, high_raw=high, close_raw=close,
                           valid=True, boundary=boundary)


@pytest.mark.parametrize('bars,outcome', [
    ([bar(109, 96, 115, 114)], 'bounce'),
    ([bar(109, 94, 110, 94)], 'break'),
    ([bar(94, 90, 110, 109)], 'gap_through'),
    ([bar(110, 108, 112, 111)] * HORIZON, 'unreached'),
    ([bar(109, 96, 110, 101)] * 4, 'unresolved'),
    ([bar(109, 96, 110, 101), None], 'unknown'),
    ([bar(109, 96, 115, 114, 'reference_change')], 'unknown'),
    ([bar(110, 108, 112, 111)] * 4 + [bar(109, 96, 110, 101)] * 3 +
     [bar(109, 98, 115, 113)], 'bounce'),
])
def test_walk_forward_labels_respect_contact_clock_and_unknowns(bars, outcome):
    assert observed_reaction(bars, 'support', 96, 102, 10) == outcome


def test_resistance_reaction_is_symmetric_and_resolves_only_after_contact():
    assert observed_reaction([bar(90, 86, 103, 88)], 'resistance', 100, 104, 10) == 'bounce'
    assert observed_reaction([bar(90, 90, 108, 106)], 'resistance', 100, 104, 10) == 'break'
    assert observed_reaction([bar(109, 98, 115, 99)], 'resistance', 100, 104, 10) == 'gap_through'


def test_audit_rejects_dates_crossing_holdout_before_opening_database():
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    later = (date.fromisoformat(cutoff) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match='مهر و موم'):
        run_audit(None, '2023-03-21', later)
