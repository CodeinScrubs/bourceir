"""Declared market calendar cannot be bypassed by a raw execution/mark row."""

import numpy as np
import pandas as pd
import pytest

from research import labels


def prices(days):
    return pd.DataFrame([
        dict(symbol='x', date=day, open_raw=100., low_raw=99., high_raw=101.,
             close_raw=100., close_final_raw=100., ref_price_raw=100.,
             volume=1000, value=100000.)
        for day in days
    ])


def row(frame, calendar, decision='2024-01-01', horizon=1):
    result = labels.compute_labels(frame, [horizon], calendar=calendar)
    return result.set_index('date').loc[decision]


def assert_invalid(outcome, horizon=1):
    assert outcome[f'label_status_{horizon}d'] == 'invalid_data'
    assert outcome[f'exit_date_{horizon}d'] == ''
    for field in ('net_ret', 'mfe', 'mae', 'exit_delay', 'corporate_actions'):
        assert pd.isna(outcome[f'{field}_{horizon}d'])
    assert pd.isna(outcome[f'net_ret_{horizon}d_worst'])


def test_off_calendar_exit_is_not_a_fill_or_a_delayed_calendar_session():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04', '2024-01-06']
    frame = prices([*calendar[:3], '2024-01-05', calendar[-1]])
    frame.loc[2, ['low_raw', 'high_raw', 'close_raw', 'close_final_raw']] = 97.
    frame.loc[2, 'value'] = 97000.
    frame.loc[3, 'ref_price_raw'] = 97.
    outcome = row(frame, calendar)
    assert outcome.entry_executable
    assert outcome.planned_exit_date_1d == '2024-01-04'
    assert_invalid(outcome)


@pytest.mark.parametrize('damage', ['flat', 'corporate_action', 'extreme_marks'])
def test_off_calendar_intermediate_raw_row_is_not_silently_dropped(damage):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    frame = prices(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
    if damage == 'corporate_action':
        frame.loc[2:, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw',
                       'ref_price_raw', 'value']] /= 2
    elif damage == 'extreme_marks':
        frame.loc[2, ['low_raw', 'high_raw']] = [1., 1000.]
    outcome = row(frame, calendar)
    assert outcome.entry_executable
    assert_invalid(outcome)


def test_off_calendar_decision_does_not_create_an_executable_opportunity():
    calendar = ['2024-01-02', '2024-01-03', '2024-01-04']
    outcome = row(prices(['2024-01-01', *calendar]), calendar)
    assert not outcome.entry_executable
    assert_invalid(outcome)


@pytest.mark.parametrize('entry_present', [True, False])
def test_off_calendar_between_decision_and_expected_entry_invalidates_path(entry_present):
    calendar = ['2024-01-01', '2024-01-03', '2024-01-04']
    days = ['2024-01-01', '2024-01-02', '2024-01-04']
    if entry_present:
        days.insert(2, '2024-01-03')
    outcome = row(prices(days), calendar)
    assert not outcome.entry_executable
    assert outcome.entry_date == '2024-01-03'
    assert_invalid(outcome)


@pytest.mark.parametrize('horizon', [1, 3])
def test_known_calendar_conflict_is_invalid_even_without_a_future_calendar_exit(horizon):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    frame = prices(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
    frame.loc[3, ['low_raw', 'high_raw', 'close_raw', 'close_final_raw']] = 97.
    frame.loc[3, 'value'] = 97000.
    outcome = row(frame, calendar, horizon=horizon)
    assert outcome.entry_executable
    assert_invalid(outcome, horizon)


def test_off_calendar_data_after_completed_exit_cannot_change_past_label():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-05']
    frame = prices(calendar)
    original = row(frame, calendar)
    assert original.label_status_1d == 'complete'
    later = prices(['2024-01-04'])
    later.loc[0, ['low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = [1, 5000, 4000, 3000, 100]
    changed = row(pd.concat([frame, later], ignore_index=True), calendar)
    pd.testing.assert_series_equal(changed, original)


def test_off_calendar_data_before_decision_does_not_invalidate_resolved_later_path():
    calendar = ['2024-01-02', '2024-01-03', '2024-01-04']
    outcome = row(prices(['2024-01-01', *calendar]), calendar, decision='2024-01-02')
    assert outcome.label_status_1d == 'complete'
    assert outcome.exit_date_1d == '2024-01-04'
    assert np.isfinite(outcome.net_ret_1d)


def test_inferred_calendar_conflict_is_unknown_without_discarding_the_raw_row(monkeypatch):
    monkeypatch.setattr(labels, 'CALENDAR_MIN_SYMBOLS', 2)
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    frame = pd.concat([prices(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04']),
                       prices(calendar).assign(symbol='y')], ignore_index=True)
    result = labels.compute_labels(frame, [1]).set_index(['symbol', 'date'])
    assert_invalid(result.loc[('x', '2024-01-01')])
    assert result.loc[('y', '2024-01-01'), 'label_status_1d'] == 'complete'
    assert ('x', '2024-01-03') in result.index


def test_missing_calendar_entry_stays_a_nonexecution_proxy_not_a_numeric_return():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03']
    outcome = row(prices(['2024-01-01', '2024-01-03']), calendar)
    assert outcome.label_status_1d == 'entry_blocked'
    assert not outcome.entry_executable
    assert pd.isna(outcome.net_ret_1d)
