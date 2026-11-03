"""Unknown outcomes and future entries must not become known idle cash or fills."""

import math

import numpy as np
import pandas as pd
import pytest

from research.baselines import Strategy, entry_counts, evaluate_picks, market_benchmark, run_baselines
from research.run import research_panel


def test_legacy_panel_without_status_does_not_invent_fills_or_cash():
    panel = pd.DataFrame(dict(date=['2024-01-01', '2024-01-02'], symbol=['a', 'a'],
                              value=[1000., 1000.], net_ret_1d=[.1, np.nan]))
    market = market_benchmark(panel, 1)
    assert market.isna().all()
    stats = evaluate_picks(panel, market, 1, 1)
    assert stats['unknown_picks'] == stats['unknown_cohorts'] == 2
    assert stats['unknown_entries'] == 2 and stats['known_entries'] == 0
    assert math.isnan(stats['mean_cohort']) and math.isnan(stats['fill_rate'])
    result = run_baselines(panel, [1], 1, 1, strategies=[Strategy('value', 'value', lambda p: p.value)])
    assert math.isnan(result['1']['market']['mean_trade'])
    assert result['1']['strategies']['value']['unknown_picks'] == 2


def test_explicit_known_block_is_cash_but_censored_row_is_not():
    panel = pd.DataFrame(dict(date=['2024-01-01'] * 3, symbol=['a', 'b', 'c'], value=[1000.] * 3,
                              net_ret_1d=[.1, np.nan, np.nan],
                              label_status_1d=['complete', 'entry_blocked', 'censored']))
    assert math.isnan(market_benchmark(panel, 1).iloc[0])
    assert market_benchmark(panel.iloc[:2], 1).iloc[0] == pytest.approx(.05)
    stats = evaluate_picks(panel, pd.Series([0.], index=['2024-01-01']), 1, 3)
    assert stats['unknown_picks'] == 1 and stats['unknown_cohorts'] == 1


def test_entry_counts_require_valid_date_consistent_flag_and_status():
    panel = pd.DataFrame(dict(
        entry_date=[None, None, '2024-01-01', '2024-02-30', '2024-01-01',
                    '2024-01-01', '2024-01-01', '2024-01-01', '2024-01-01'],
        entry_executable=pd.Series([False, True, True, True, True, True, False, 1, True], dtype=object),
        label_status_1d=['entry_blocked', 'complete', 'entry_blocked', 'complete', 'censored',
                         'complete', 'entry_blocked', 'complete', 'invalid_data'],
        net_ret_1d=[np.nan, .1, np.nan, .1, np.nan, .1, np.nan, .1, np.nan]))
    result = entry_counts(panel, 1)
    assert result == dict(known_entries=4, filled_entries=3, unknown_entries=5, fill_rate=.75)


def test_returns_and_market_do_not_accept_inconsistent_entry_or_missing_exit():
    panel = pd.DataFrame(dict(date=['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'],
        symbol=['a', 'a', 'a', 'a'], value=[1000.] * 4,
        entry_date=[None, '2024-01-03', '2024-01-04', '2024-01-05'],
        entry_executable=[False, True, True, False],
        label_status_1d=['entry_blocked', 'entry_blocked', 'complete', 'complete'],
        exit_date_1d=['', '', '', '2024-01-06'], net_ret_1d=[np.nan, np.nan, .1, .1]))
    market = market_benchmark(panel, 1)
    stats = evaluate_picks(panel, market, 1, 1)
    assert market.isna().all() and stats['unknown_picks'] == stats['unknown_cohorts'] == 4
    assert stats['trades'] == 0 and math.isnan(stats['mean_cohort'])
    # The third row has evidence of entry, although its asserted exit is missing.
    assert stats['known_entries'] == stats['filled_entries'] == 1 and stats['unknown_entries'] == 3


def test_partial_entry_columns_do_not_fall_back_to_status_only():
    panel = pd.DataFrame(dict(date=['2024-01-01'], symbol=['a'], value=[1000.],
        entry_date=['2024-01-02'], label_status_1d=['complete'], net_ret_1d=[.1]))
    stats = evaluate_picks(panel, market_benchmark(panel, 1), 1, 1)
    assert stats['unknown_picks'] == stats['unknown_entries'] == 1
    assert math.isnan(stats['mean_trade'])


def test_impossible_entry_and_exit_chronology_cannot_create_returns():
    panel = pd.DataFrame(dict(date=['2024-01-03', '2024-01-01', '2024-01-01', '2024-01-01'],
        symbol=['a', 'b', 'c', 'd'], value=[1000.] * 4,
        entry_date=['2024-01-02', '2024-01-02', '2024-01-02', '2024-01-02'],
        entry_executable=[True, True, True, False],
        label_status_1d=['complete', 'complete', 'complete', 'entry_blocked'],
        exit_date_1d=['2024-01-04', '2024-01-01', '2024-01-02', ''],
        net_ret_1d=[.1, .1, .1, np.nan]))
    stats = evaluate_picks(panel, market_benchmark(panel, 1), 1, 1)
    assert stats['unknown_picks'] == 3 and stats['trades'] == 0
    assert stats['known_entries'] == 3 and stats['unknown_entries'] == 1
    assert math.isnan(stats['mean_cohort'])


def test_research_panel_never_reports_entry_outcome_at_or_after_end():
    labels = pd.DataFrame(dict(date=['2023-12-31', '2024-01-01', '2024-01-01'],
        symbol=['a', 'b', 'c'], entry_date=['2024-01-01', '2024-01-02', '2024-01-02'],
        entry_executable=[True, True, False], label_status_1d=['complete', 'complete', 'entry_blocked'],
        exit_date_1d=['2024-01-03', '2024-01-03', ''], planned_exit_date_1d=['2024-01-03'] * 3,
        net_ret_1d=[.1, .2, np.nan]))
    features = labels[['date', 'symbol']].copy()
    features['value'] = 1000.
    panel = research_panel(labels, features, '2023-12-31', '2024-01-02', 0)
    assert panel['label_status_1d'].eq('censored').all() and panel['net_ret_1d'].isna().all()
    assert bool(panel.loc[0, 'entry_executable'])
    assert panel.loc[1:, 'entry_executable'].isna().all()
    assert entry_counts(panel, 1) == dict(known_entries=1, filled_entries=1, unknown_entries=2, fill_rate=1.)
    labels.loc[1:, 'entry_executable'] = [False, True]
    changed = research_panel(labels, features, '2023-12-31', '2024-01-02', 0)
    assert entry_counts(changed, 1) == entry_counts(panel, 1)


def test_blocked_entry_known_before_boundary_is_not_censored_by_planned_exit():
    labels = pd.DataFrame(dict(date=['2024-01-01'], symbol=['a'], entry_date=['2024-01-02'],
        entry_executable=[False], label_status_1d=['entry_blocked'], exit_date_1d=[''],
        planned_exit_date_1d=['2024-01-04'], net_ret_1d=[np.nan]))
    features = pd.DataFrame(dict(date=['2024-01-01'], symbol=['a'], value=[1000.]))
    panel = research_panel(labels, features, '2024-01-01', '2024-01-03', 0)
    assert panel.loc[0, 'label_status_1d'] == 'entry_blocked'
    assert entry_counts(panel, 1) == dict(known_entries=1, filled_entries=0, unknown_entries=0, fill_rate=0.)
    assert market_benchmark(panel, 1).iloc[0] == 0.
