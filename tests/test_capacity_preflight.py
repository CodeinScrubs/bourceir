"""Regression tests for the outcome-free capacity diagnostic."""

import pandas as pd
import pytest

from research.capacity_preflight import THRESHOLD_RIAL, capacity_bounds, decision_preflight, liquidity_observations


def test_native_zero_does_not_prove_pre_listing_or_override_conflicting_activity():
    days = pd.date_range('2024-01-01', periods=6).strftime('%Y-%m-%d').tolist()
    rows = pd.DataFrame([dict(symbol='a', date=days[i], open_raw=100, high_raw=100, low_raw=100,
                              close_raw=100, close_final_raw=100, ref_price_raw=100, value=1000,
                              volume=10, trade_count=1) for i in (1, 3, 5)])
    # The zero on day 2 cannot make day 1's contradictory trade an earlier listing proof.
    zeros = pd.DataFrame({'symbol': ['a']*5+['b'], 'date': [days[i] for i in (0, 1, 2, 4, 5, 4)]})
    observations, audit = liquidity_observations(rows, zeros)
    assert observations.set_index('date').value.to_dict() == {days[3]: 1000, days[4]: 0}
    assert audit == dict(admitted_zero_keys=1, before_first_observed_trade_or_unknown_zeros=4, zero_conflicts=2)


def test_capacity_window_counts_zero_and_excludes_missing_and_future():
    days = pd.date_range('2024-01-01', periods=27).strftime('%Y-%m-%d').tolist()
    day = days[20]
    registry = ['a', 'b', 'c', 'd']
    rows = []
    # Day zero would be the fifteenth observation for d if the window were off by one.
    for symbol, idxs in [('a', range(1, 21)), ('b', range(6, 21)), ('c', range(7, 21)),
                         ('d', [0, *range(7, 21)])]:
        for i in idxs:
            value = 0 if symbol == 'a' and i <= 11 else THRESHOLD_RIAL
            rows.append(dict(date=days[i], symbol=symbol, value=value))
    obs = pd.DataFrame(rows)
    panel = pd.DataFrame(dict(date=[day]*4, symbol=registry, value=[THRESHOLD_RIAL]*4,
                              ret_20d=[.4, .3, .2, .1]))
    folds = [dict(test_start=day, test_end_exclusive=days[-1])]
    report = decision_preflight(panel, obs, days, registry, folds, 1)
    first = report[0]
    assert first['lookback_start'] == days[1] and first['planned_primary_exit'] == days[23]
    assert first['full_20'] == 1 and first['valid_15_to_19'] == 1 and first['insufficient'] == 2
    assert first['valid_zero_cells'] == 11 and first['missing_or_invalid_cells'] == 17
    assert first['control_top5'] == registry and first['filtered_top5'] == ['b']
    assert report[-1]['planned_primary_exit'] is None
    future = pd.DataFrame(dict(date=[days[21]], symbol=['c'], value=[THRESHOLD_RIAL]))
    assert decision_preflight(panel, pd.concat([obs, future]), days, registry, folds, 1)[0] == first


def test_filter_precedes_ranking_and_retains_baseline_tie_break():
    days = pd.date_range('2024-01-01', periods=25).strftime('%Y-%m-%d').tolist()
    symbols = ['f', 'e', 'd', 'c', 'b', 'a']
    obs = pd.DataFrame([dict(date=day, symbol=symbol, value=THRESHOLD_RIAL if symbol != 'a' else 0)
                        for day in days[:20] for symbol in symbols])
    panel = pd.DataFrame(dict(date=[days[19]]*6, symbol=symbols, value=[30, 20, 20, 20, 20, 20],
                              ret_20d=[.1, .1, .1, .1, .1, .2]))
    folds = [dict(test_start=days[19], test_end_exclusive=days[24])]
    first = decision_preflight(panel, obs, days, symbols, folds, 1)[0]
    assert first['control_top5'] == ['a', 'f', 'b', 'c', 'd']
    assert first['filtered_top5'] == ['f', 'b', 'c', 'd', 'e']
    with pytest.raises(ValueError, match='Unique decision'):
        decision_preflight(pd.concat([panel, panel]), obs, days, symbols, folds, 1)


def test_sharp_missingness_bounds_do_not_promote_observed_median():
    import numpy as np

    threshold = THRESHOLD_RIAL
    grid = pd.DataFrame({
        'uncertain': [threshold/2]*7+[threshold*2]*8+[np.nan]*5,
        'fail': [threshold/2]*11+[threshold*2]*4+[np.nan]*5,
        'pass': [threshold*2]*15+[np.nan]*5,
        'thin': [threshold*2]*14+[np.nan]*6,
        'complete': [threshold/2]*10+[threshold*2]*10,
    })
    original = grid.copy(deep=True)
    count, observed, lower, upper, status = capacity_bounds(grid)
    assert status.iloc[-1].tolist() == ['UNKNOWN', 'FAIL', 'PASS', 'UNKNOWN', 'PASS']
    assert observed.uncertain.iloc[-1] == threshold*2
    assert lower.uncertain.iloc[-1] == threshold/2 and upper.uncertain.iloc[-1] == threshold*2
    assert lower.complete.iloc[-1] == upper.complete.iloc[-1] == threshold*1.25
    assert count.thin.iloc[-1] == 14 and pd.isna(lower.thin.iloc[-1])
    # In the review's constant-value example, five unknown days cannot lower
    # the middle two observations: 15 equal known values pin the median.
    assert lower['pass'].iloc[-1] == upper['pass'].iloc[-1] == threshold*2
    pd.testing.assert_frame_equal(grid, original)
    for column in ('uncertain', 'fail', 'pass', 'complete'):
        for replacement in (0, threshold, threshold*10):
            actual = grid[column].fillna(replacement).median()
            assert lower[column].iloc[-1] <= actual <= upper[column].iloc[-1]
    grid.loc[0, 'pass'] = -1
    with pytest.raises(ValueError, match='Nonnegative'):
        capacity_bounds(grid)


def test_bounds_remove_unknown_candidate_before_top_five():
    days = pd.date_range('2024-01-01', periods=25).strftime('%Y-%m-%d').tolist()
    values = [THRESHOLD_RIAL/2]*7+[THRESHOLD_RIAL*2]*8
    obs = pd.DataFrame([dict(date=days[i], symbol='a', value=value) for i, value in enumerate(values)])
    panel = pd.DataFrame([dict(date=days[19], symbol='a', value=THRESHOLD_RIAL, ret_20d=.2)])
    rows, candidates = decision_preflight(panel, obs, days, ['a'],
        [dict(test_start=days[19], test_end_exclusive=days[-1])], 1, with_candidates=True)
    assert rows[0]['observed_median_top5'] == ['a'] and rows[0]['filtered_top5'] == []
    assert rows[0]['missingness_bounds_changed_tape'] is True
    assert candidates.capacity_status.tolist() == ['UNKNOWN']
