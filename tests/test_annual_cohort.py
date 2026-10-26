"""Synthetic census checks independent of Parquet/registry or live market data."""

import pandas as pd
import pytest

from research.annual_cohort import first_observed_cohort


def test_missing_anchor_is_not_silently_removed_from_opportunities():
    prices = pd.DataFrame({
        'symbol': ['old', 'old', 'control', 'control', 'control'],
        'date': ['2024-01-01', '2024-01-03', '2024-01-01', '2024-01-02', '2024-01-03'],
    })
    result = first_observed_cohort(prices, ['2024-01-01', '2024-01-02', '2024-01-03'])
    assert list(result.loc[result.symbol.eq('old'), 'date']) == [
        '2024-01-01', '2024-01-02', '2024-01-03'
    ]
    assert len(result) == 6


def test_future_first_observation_cannot_backfill_earlier_anchors():
    prices = pd.DataFrame({
        'symbol': ['legacy', 'new', 'new'],
        'date': ['2024-01-01', '2024-01-03', '2024-01-04'],
    })
    result = first_observed_cohort(prices, ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
    assert list(result.loc[result.symbol.eq('legacy'), 'date']) == [
        '2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'
    ]
    assert list(result.loc[result.symbol.eq('new'), 'date']) == [
        '2024-01-03', '2024-01-04'
    ]


def test_previous_symbol_not_discarded_only_because_it_stops_reporting_bars():
    prices = pd.DataFrame({'symbol': ['control', 'stale'], 'date': ['2024-01-01', '2024-01-01']})
    result = first_observed_cohort(prices, ['2024-01-01', '2024-01-20'])
    assert ('stale', '2024-01-20') in set(result.itertuples(index=False, name=None))


def test_row_order_does_not_change_membership():
    prices = pd.DataFrame({
        'symbol': ['z', 'a', 'z', 'a'],
        'date': ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-03'],
    })
    anchors = ['2024-01-01', '2024-01-02', '2024-01-03']
    before = first_observed_cohort(prices, anchors)
    after = first_observed_cohort(prices.sample(frac=1, random_state=23), anchors)
    pd.testing.assert_frame_equal(before, after)


@pytest.mark.parametrize('corruption', ['duplicate', 'null_symbol', 'blank_symbol', 'missing_date',
                                       'unsorted_anchors', 'duplicate_anchors', 'empty_anchors'])
def test_rejects_invalid_prevalidated_keys_or_anchor_calendar(corruption):
    prices = pd.DataFrame({'symbol': ['a', 'b'], 'date': ['2024-01-01', '2024-01-02']})
    anchors = ['2024-01-01', '2024-01-02']
    if corruption == 'duplicate':
        prices = pd.concat([prices, prices.iloc[[0]]], ignore_index=True)
    elif corruption == 'null_symbol':
        prices.loc[0, 'symbol'] = None
    elif corruption == 'blank_symbol':
        prices.loc[0, 'symbol'] = ''
    elif corruption == 'missing_date':
        prices.loc[0, 'date'] = None
    elif corruption == 'unsorted_anchors':
        anchors = anchors[::-1]
    elif corruption == 'duplicate_anchors':
        anchors.append('2024-01-02')
    else:
        anchors = []
    with pytest.raises(ValueError):
        first_observed_cohort(prices, anchors)
