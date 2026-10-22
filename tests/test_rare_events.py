import numpy as np
import pandas as pd
import pytest

from research.rare_events import compute_rare_events, summarize_rare_events


def source(values, refs=None):
    days = pd.date_range('2024-01-01', periods=len(values)).strftime('%Y-%m-%d').tolist()
    return pd.DataFrame(dict(symbol='الف', date=days, close_final_raw=values,
        ref_price_raw=refs or [values[0], *values[:-1]], volume=100)), days


def run(prices, calendar, horizons=(2, 3), decisions=None, observed=None):
    if decisions is None:
        decisions = prices.iloc[:1][['symbol', 'date']]
    return compute_rare_events(prices, decisions, calendar, horizons=horizons,
        observed_through=observed or max(prices.date))


def test_hit_boundary_endpoint_and_no_intraday_high_shortcut():
    prices, days = source([100, 150, 200, 140])
    prices['high_raw'] = 900
    result = run(prices, days, (1, 2, 3)).set_index('horizon_sessions')
    assert result.loc[1, 'status'] == 'complete_no_hit'
    assert result.loc[2, 'status'] == result.loc[3, 'status'] == 'hit'
    assert result.loc[2, 'first_hit_date'] == days[2]
    assert result.loc[3, 'endpoint_multiple'] == pytest.approx(1.4)


@pytest.mark.parametrize('last,expected', [(200., 'hit'), (np.nextafter(200., 0), 'complete_no_hit'),
                                           (np.nextafter(200., np.inf), 'hit')])
def test_exact_decimal_threshold_does_not_use_a_broad_float_tolerance(last, expected):
    prices, days = source([100., 114., last])
    assert run(prices, days, (2,)).iloc[0].status == expected


def test_missing_before_hit_cannot_be_repaired_by_later_price():
    prices, days = source([100, 130, 200, 250])
    result = run(prices.drop(index=1), days)
    assert result.status.tolist() == ['missing_observation']*2
    assert result.outcome.isna().all() and result.endpoint_multiple.isna().all()


def test_missing_after_hit_keeps_event_but_not_endpoint():
    prices, days = source([100, 200, 210, 220])
    result = run(prices.drop(index=2), days)
    assert result.status.eq('hit').all() and result.outcome.all()
    assert result.first_hit_date.eq(days[1]).all()
    assert result.endpoint_multiple.isna().all()
    assert result.path_problem.eq('missing_observation').all()


def test_right_censored_is_not_failure_and_resolved_hit_survives_tail():
    prices, days = source([100, 120, 200])
    result = run(prices, days, (1, 3, 60)).set_index('horizon_sessions')
    assert result.loc[1, 'status'] == 'complete_no_hit'
    assert result.loc[60, 'status'] == 'hit'
    assert result.loc[60, 'path_problem'] == 'right_censored'
    prices.loc[2, 'close_final_raw'] = 150
    assert run(prices, days, (3,)).iloc[0].status == 'right_censored'


def test_late_hit_cannot_enter_shorter_horizon():
    prices, days = source([100, 120, 150, 200])
    result = run(prices, days)
    assert result.status.tolist() == ['complete_no_hit', 'hit']


@pytest.mark.parametrize('column,value', [('ref_price_raw', 0), ('ref_price_raw', np.nan),
    ('close_final_raw', np.inf), ('close_final_raw', True), ('volume', 0), ('volume', 2.5)])
def test_invalid_prefix_is_unknown(column, value):
    prices, days = source([100, 200, 250, 260])
    prices[column] = prices[column].astype(object)
    prices.loc[1, column] = value
    result = run(prices, days)
    assert result.status.eq('invalid_observation').all() and result.outcome.isna().all()


def test_calendar_conflict_cannot_omit_negative_return():
    prices, days = source([100, 50, 100, 150])
    result = run(prices, [days[0], *days[2:]], (1, 2))
    assert result.status.eq('calendar_conflict').all()
    assert result.path_problem_date.eq(days[1]).all()


def test_calendar_conflict_after_resolved_hit_does_not_revoke_hit():
    prices, days = source([100, 200, 210, 220])
    result = run(prices, [*days[:2], days[3]], (1, 2))
    assert result.status.eq('hit').all()
    assert pd.isna(result.iloc[0].path_problem)
    assert result.iloc[1].path_problem == 'calendar_conflict'


def test_reference_reset_does_not_create_false_synthetic_double():
    prices, days = source([100, 50, 100], [100, 50, 100])
    result = run(prices, days, (2,)).iloc[0]
    assert result.status == 'complete_no_hit' and result.endpoint_multiple == 1
    assert result.prefix_reference_changes == 2
    assert result.economic_double is None and result.executable_return is None


def test_mutating_later_prices_preserves_resolved_label():
    prices, days = source([100, 200, 210, 220])
    before = run(prices, days, (1, 3))
    prices['close_final_raw'] = prices.close_final_raw.astype(float)
    prices.loc[2:, 'close_final_raw'] = [10, np.nan]
    after = run(prices, days, (1, 3))
    pd.testing.assert_frame_equal(before.iloc[:1], after.iloc[:1])
    for col in ['outcome', 'first_hit_date', 'first_hit_session', 'resolved_at', 'status']:
        pd.testing.assert_series_equal(before[col], after[col])


def test_missing_and_invalid_anchors_remain_in_denominator():
    prices, days = source([100, 120, 150])
    anchors = pd.DataFrame([{'symbol':'ناشناخته', 'date':days[0]}, {'symbol':'الف', 'date':days[0]}])
    prices.loc[0, 'volume'] = 0
    result = run(prices, days, (2,), anchors)
    assert set(result.status) == {'missing_anchor', 'invalid_anchor'}
    summary = summarize_rare_events(result)[0]
    assert summary['opportunities'] == summary['unknown'] == 2
    assert summary['observed_hit_fraction_lower'] == 0 and summary['possible_hit_fraction_upper'] == 1
    assert summary['resolved_subset_hit_fraction'] is None


def test_summary_retains_unknowns_overlap_and_denominator():
    prices, days = source([100, 200, 210, 220])
    anchors = prices.iloc[[0, 1, 3]][['symbol', 'date']]
    result = run(prices, days, (2,), anchors)
    summary = summarize_rare_events(result)[0]
    assert (summary['hits'], summary['complete_no_hit'], summary['unknown']) == (1, 1, 1)
    assert summary['observed_hit_fraction_lower'] == pytest.approx(1/3)
    assert summary['possible_hit_fraction_upper'] == pytest.approx(2/3)
    assert summary['overlapping_anchor_rows'] == 2
    assert not summary['independent_samples'] and not summary['accepted_for_signals']


def test_empty_cohort_and_input_guards():
    prices, days = source([100, 200])
    assert summarize_rare_events(run(prices, days, (1,), prices.iloc[:0][['symbol','date']])) == []
    with pytest.raises(ValueError):
        run(pd.concat([prices, prices.iloc[:1]]), days)
    with pytest.raises(ValueError):
        run(prices, days[::-1])
    with pytest.raises(ValueError):
        run(prices, days, (True,))
    with pytest.raises(ValueError):
        run(prices, days, (1, 1))
    with pytest.raises(ValueError):
        run(prices, days, observed=days[0])
    with pytest.raises(ValueError):
        run(prices, days, observed='2025-03-21')
    duplicate = prices.copy()
    duplicate['symbol'] = ['كيان', 'کیان']
    duplicate['date'] = days[0]
    with pytest.raises(ValueError):
        run(duplicate, days)


def test_long_horizon_truncation_does_not_turn_unknown_into_no_hit():
    prices, days = source([100, 150])
    result = run(prices, days, (1, 60))
    assert result.status.tolist() == ['complete_no_hit', 'right_censored']
    assert pd.isna(result.iloc[1].outcome)
