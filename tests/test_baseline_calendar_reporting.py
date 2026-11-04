"""Empty panel sessions must not silently disappear from baseline reports."""

import math

import pandas as pd
import pytest

from research.baselines import Strategy, random_benchmark, run_baselines


def panel():
    return pd.DataFrame(dict(date=['2024-01-01', '2024-01-03'], symbol=['a', 'a'],
        value=[1000., 1000.], score=[1., 1.], net_ret_1d=[.1, .2], label_status_1d=['complete', 'complete']))


def test_missing_panel_day_is_unknown_for_market_strategy_and_random():
    days = ['2024-01-01', '2024-01-02', '2024-01-03']
    result = run_baselines(panel(), [1], 1, 3, strategies=[Strategy('fixed', 'fixed', lambda p: p.score)],
                           calendar=days)['1']
    market, strategy, random = result['market'], result['strategies']['fixed'], result['random']
    assert market['calendar_days'] == 3 and market['no_panel_days'] == market['unknown_days'] == 1
    assert market['calendar_basis'] == 'provided_calendar_not_verified_here'
    assert market['mean_cohort'] == pytest.approx(.15)  # conditional on the two known days
    assert math.isnan(market['t_cohort'])
    assert strategy['days'] == 3 and strategy['unknown_cohorts'] == 1
    assert strategy['missing_panel_days'] == 1 and strategy['days_without_selection'] == 0
    assert strategy['mean_cohort'] == pytest.approx(.15)
    assert random['minimum_unknown_days'] == random['maximum_unknown_days'] == 1


def test_random_benchmark_direct_call_cannot_turn_missing_panel_into_cash():
    result = random_benchmark(panel(), 1, 1, 3, calendar=['2024-01-01', '2024-01-02', '2024-01-03'])
    assert result['minimum_unknown_days'] == result['maximum_unknown_days'] == 1
    assert result['conditional_on_known_days'] and result['mean'] == pytest.approx(.15)


def test_observed_day_without_scorable_pick_is_idle_cash_not_missing_panel():
    data = pd.concat([panel(), pd.DataFrame(dict(date=['2024-01-02'], symbol=['a'], value=[1000.],
        score=[float('nan')], net_ret_1d=[.3], label_status_1d=['complete']))], ignore_index=True)
    result = run_baselines(data, [1], 1, 1, strategies=[Strategy('fixed', 'fixed', lambda p: p.score)],
                           calendar=['2024-01-01', '2024-01-02', '2024-01-03'])['1']
    strategy = result['strategies']['fixed']
    assert strategy['unknown_cohorts'] == strategy['missing_panel_days'] == 0
    assert strategy['days_without_selection'] == 1 and strategy['mean_cohort'] == pytest.approx(.1)
    assert strategy['score_coverage']['unknown_score_rows'] == 1


def test_observed_only_calendar_is_labelled_as_incomplete_scope():
    result = run_baselines(panel(), [1], 1, 1, strategies=[Strategy('fixed', 'fixed', lambda p: p.score)])['1']
    assert result['market']['calendar_basis'] == 'observed_panel_only'
    assert result['market']['calendar_days'] == 2


@pytest.mark.parametrize('days', [
    ['2024-01-03', '2024-01-01'],
    ['2024-01-01', '2024-01-01', '2024-01-03'],
    ['2024-1-1', '2024-01-03'],
    ['2024-01-01'],
    [],
])
def test_invalid_or_conflicting_calendar_is_rejected(days):
    with pytest.raises(ValueError, match='تقویم'):
        run_baselines(panel(), [1], 1, 1, strategies=[Strategy('fixed', 'fixed', lambda p: p.score)], calendar=days)
