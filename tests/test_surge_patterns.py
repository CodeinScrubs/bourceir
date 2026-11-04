import numpy as np
import pandas as pd
import pytest

from config import settings
from research.surge_patterns import (
    HYPOTHESES,
    block_diagnostic,
    build_pre_move_features,
    censor_at,
    event_counts,
    execution_counts,
    pattern_masks,
    summarize_patterns,
)


def source(n=100):
    days = pd.date_range('2023-01-01', periods=n).strftime('%Y-%m-%d').tolist()
    close = 100*np.power(1.01, np.arange(n))
    frame = pd.DataFrame(dict(symbol='الف', date=days, open_raw=close, high_raw=close*1.01,
        low_raw=close*.99, close_raw=close, close_final_raw=close,
        ref_price_raw=np.r_[100., close[:-1]], volume=100, value=100*close))
    return frame, days


def features(prices, days, decisions=None):
    return build_pre_move_features(prices, decisions if decisions is not None else prices[['symbol', 'date']],
                                   days, min_cross_section=1)


def test_returns_are_past_only_and_first_archive_reference_is_not_a_return():
    prices, days = source()
    prices.loc[0, 'ref_price_raw'] = 1.
    actual = features(prices, days)
    assert pd.isna(actual.ret_1.iloc[0]) and pd.isna(actual.ret_20.iloc[19])
    assert actual.ret_20.iloc[20] == pytest.approx(1.01**20-1)
    assert pd.isna(actual.breakout_60.iloc[59]) and actual.breakout_60.iloc[60] > 0


def test_later_prices_and_adjusted_columns_cannot_change_earlier_features():
    prices, days = source()
    before = features(prices, days)
    prices['adj_close'] = np.arange(len(prices)) + 900000
    prices['future_return'] = -9000
    for col in ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value'):
        prices.loc[80:, col] *= 17
    after = features(prices, days)
    pd.testing.assert_frame_equal(before.loc[before.date < days[80]], after.loc[after.date < days[80]])


def test_reference_reset_does_not_make_a_false_breakout_or_momentum():
    prices, days = source()
    before = features(prices, days)
    for col in ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw'):
        prices.loc[50:, col] /= 2
    prices.loc[50:, 'volume'] *= 2
    after = features(prices, days)
    for column in ('ret_1', 'ret_20', 'breakout_60', 'value_ratio'):
        np.testing.assert_allclose(before[column], after[column], equal_nan=True)


@pytest.mark.parametrize('column,value', [('volume', 0), ('volume', 2.5), ('value', np.nan),
    ('value', 9e15), ('close_final_raw', True), ('ref_price_raw', 0), ('close_raw', 999999)])
def test_bad_observation_poisoning_is_not_omitted(column, value):
    prices, days = source()
    prices[column] = prices[column].astype(object)
    prices.loc[50, column] = value
    actual = features(prices, days)
    assert pd.isna(actual.ret_20.iloc[69]) and pd.isna(actual.breakout_60.iloc[99])
    assert pd.isna(actual.locked_up_today.iloc[50])


def test_missing_observation_does_not_compress_twenty_market_sessions():
    prices, days = source()
    actual = features(prices.drop(index=50), days)
    assert days[50] not in set(actual.date)
    assert pd.isna(actual.set_index('date').loc[days[69], 'ret_20'])
    assert actual.set_index('date').loc[days[70], 'ret_20'] == pytest.approx(1.01**20-1)


def test_off_calendar_observation_cannot_be_removed_to_improve_features():
    prices, days = source()
    actual = features(prices, [d for d in days if d != days[50]], prices.loc[prices.date != days[50], ['symbol', 'date']])
    assert pd.isna(actual.set_index('date').loc[days[51], 'ret_1'])
    assert pd.isna(actual.set_index('date').loc[days[70], 'ret_20'])


def test_decision_denominator_is_not_extended_and_missing_symbol_remains_unknown():
    prices, days = source()
    anchors = pd.DataFrame([dict(symbol='الف', date=days[70]), dict(symbol='گمشده', date=days[70])])
    actual = features(prices, days, anchors)
    assert len(actual) == 2
    masks = pattern_masks(actual)
    assert masks.loc[actual.symbol.eq('گمشده')].isna().all().all()
    assert len(masks.columns) == len(HYPOTHESES) == 10


def test_value_surge_uses_prior_twenty_and_does_not_dilute_itself():
    prices, days = source()
    previous_mean = prices.value.iloc[59:79].mean()
    prices.loc[79, 'volume'] = 200
    prices.loc[79, 'value'] = prices.close_raw.iloc[79]*200
    actual = features(prices, days)
    assert actual.value_ratio.iloc[79] == pytest.approx(prices.value.iloc[79]/previous_mean)
    assert bool(pattern_masks(actual).value_surge.iloc[79])


def test_zero_volatility_is_not_an_invented_compression_ratio():
    prices, days = source()
    for col in ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw'):
        prices[col] = 100.
    assert features(prices, days).vol_5_vs_20.isna().all()


@pytest.mark.parametrize('where', ['prices', 'decisions', 'calendar'])
def test_holdout_is_rejected_in_all_inputs(where):
    prices, days = source()
    decisions = prices[['symbol', 'date']].copy()
    boundary = settings.RESEARCH_CONFIG['holdout_start']
    if where == 'prices':
        prices.loc[len(prices)-1, 'date'] = boundary
    elif where == 'decisions':
        decisions.loc[len(decisions)-1, 'date'] = boundary
    else:
        days.append(boundary)
    with pytest.raises(ValueError, match='holdout|development'):
        features(prices, days, decisions)


def test_arabic_persian_symbol_collision_is_not_double_counted():
    prices, days = source()
    prices.symbol = 'ک'
    duplicate = prices.iloc[[0]].copy()
    duplicate.symbol = 'ك'
    with pytest.raises(ValueError, match='Duplicate'):
        features(pd.concat([prices, duplicate], ignore_index=True), days)


def outcomes():
    return pd.DataFrame(dict(symbol=['الف', 'ب', 'ج'], date=['2024-01-01']*3,
        status=['hit', 'complete_no_hit', 'missing_observation'],
        outcome=pd.array([True, False, None], dtype='boolean'),
        resolved_at=['2024-01-05', '2024-02-01', None]))


def test_unknown_outcomes_remain_in_bounds_and_are_not_failures():
    actual = event_counts(outcomes())
    assert actual['hit_fraction_lower'] == 1/3 and actual['hit_fraction_upper'] == 2/3
    assert actual['resolved_subset_hit_fraction'] == .5 and actual['unknown'] == 1


def test_fold_resolution_is_purged_and_later_hit_cannot_leak_into_test():
    raw = outcomes()
    actual = censor_at(raw, '2024-01-05')
    assert actual.outcome.isna().all()
    assert actual.status.tolist() == ['fold_right_censored', 'fold_right_censored', 'missing_observation']
    assert raw.status.iloc[0] == 'hit'


def test_block_controls_match_dates_and_do_not_credit_a_bull_regime():
    rows, selected = [], []
    for i, day in enumerate(pd.date_range('2024-01-01', periods=32).strftime('%Y-%m-%d')):
        bull = i < 16
        # Identical conditional rates within every date; selected counts concentrate in bull dates.
        for j in range(12):
            chosen = j < (10 if bull else 2)
            rows.append(dict(date=day, symbol=str(j), status='hit' if bull else 'complete_no_hit'))
            selected.append(chosen)
    actual = block_diagnostic(pd.DataFrame(rows), pd.Series(selected, dtype='boolean'), family_comparisons=40, replications=1000)
    assert actual['difference'] == 0
    assert actual['intervals'][0]['family_ci'] == [0, 0]
    assert not actual['all_opportunity_advantage_established']


def test_missing_dated_control_is_not_dropped_to_sharpen_interval():
    rows = pd.DataFrame(dict(date=pd.date_range('2024-01-01', periods=32).strftime('%Y-%m-%d'),
                             symbol='الف', status='hit'))
    actual = block_diagnostic(rows, pd.Series([True]*31+[False], dtype='boolean'), family_comparisons=40, replications=1000)
    assert actual['status'] == 'missing_same_date_controls' and not actual['intervals']


def test_execution_diagnostics_retain_idle_cash_unknowns_and_delayed_exit():
    rows = pd.DataFrame(dict(label_status_59d=['complete', 'entry_blocked', 'invalid_data', 'complete'],
        entry_date=['2024-01-02']*4, exit_date_59d=['2024-03-02', '', '', '2025-01-02'],
        net_ret_59d=[.5, np.nan, np.nan, 9.], net_ret_59d_worst=[.3, np.nan, np.nan, 8.],
        corporate_actions_59d=[1, np.nan, np.nan, 0], exit_delay_59d=[3, np.nan, np.nan, 0]))
    actual = execution_counts(rows, 59, end_exclusive='2025-01-01')
    assert actual['normal_mean_known_opportunities'] == .25
    assert actual['stress_mean_known_opportunities'] == .15
    assert actual['entry_blocked'] == 1 and actual['unknown'] == 2
    assert actual['completed_with_synthetic_actions'] == 1 and actual['max_exit_delay_sessions'] == 3


def test_full_cohort_is_required_and_no_hit_cannot_have_an_unknown_outcome():
    prices, days = source()
    f = features(prices, days, prices.iloc[[70]][['symbol', 'date']])
    o = pd.DataFrame([dict(symbol='الف', date=days[70], horizon_sessions=h,
                          status='complete_no_hit', outcome=None, resolved_at=days[90]) for h in (20, 60)])
    with pytest.raises(ValueError, match='explicit boolean'):
        summarize_patterns(f, o)
    o.outcome = False
    extra = f.copy()
    extra.symbol = 'ب'
    with pytest.raises(ValueError, match='full frozen'):
        summarize_patterns(pd.concat([f, extra], ignore_index=True), o)
