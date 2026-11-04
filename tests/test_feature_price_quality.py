"""Invalid raw observations must not become ordinary numeric model features."""

import numpy as np
import pandas as pd
import pytest

from features.daily import DAILY_FEATURES, compute_daily_features
from features.market import compute_market_features


def prices_frame(n=60):
    return pd.DataFrame({
        'date': pd.bdate_range('2023-01-02', periods=n).strftime('%Y-%m-%d'),
        'open_raw': 100., 'high_raw': 103., 'low_raw': 97., 'close_raw': 100.,
        'close_final_raw': 100., 'ref_price_raw': 100.,
        'volume': 1000., 'value': 100000., 'trade_count': 10,
    })


@pytest.mark.parametrize(('field', 'value'), [
    ('ref_price_raw', np.nan), ('ref_price_raw', 0.), ('ref_price_raw', np.inf),
    ('ref_price_raw', True), ('volume', np.nan), ('volume', 0.), ('volume', 1.5),
    ('high_raw', np.nan), ('low_raw', -1.), ('low_raw', 104.),
])
def test_incomplete_lock_inputs_are_unknown_in_features(field, value):
    prices = prices_frame()
    prices[field] = prices[field].astype(object)
    prices.loc[50, field] = value
    before = prices.copy(deep=True)
    result = compute_daily_features(prices)
    assert result.loc[50, ['locked_up', 'locked_down', 'lock_up_streak', 'lock_down_streak']].isna().all()
    pd.testing.assert_frame_equal(prices, before)


def test_unknown_lock_does_not_reset_a_streak_to_a_known_length():
    prices = prices_frame(6)
    columns = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw']
    prices.loc[[1, 2, 3, 5], columns] = 103.
    prices.loc[[1, 2, 3, 5], 'value'] = 103000.
    prices.loc[2, 'ref_price_raw'] = np.nan
    result = compute_daily_features(prices)
    np.testing.assert_allclose(result.locked_up, [0., 1., np.nan, 1., 0., 1.], equal_nan=True)
    np.testing.assert_allclose(result.lock_up_streak, [0., 1., np.nan, np.nan, 0., 1.], equal_nan=True)
    # A known non-lock ends uncertainty for that direction immediately.
    np.testing.assert_allclose(result.lock_down_streak, [0., 0., np.nan, 0., 0., 0.], equal_nan=True)


def test_atr_does_not_count_an_incomplete_daily_range_as_observed():
    prices = prices_frame()
    prices.loc[45:49, 'high_raw'] = np.nan
    result = compute_daily_features(prices)
    # Nine complete ranges remain in the trailing 14 sessions; the contract needs ten.
    assert pd.isna(result.loc[50, 'atr_14d_pct'])
    assert pd.isna(result.loc[45, 'range_pct'])
    assert result.loc[44, 'atr_14d_pct'] == pytest.approx(.06)


def test_incoherent_value_does_not_create_a_turnover_surge_or_poison_the_baseline():
    prices = prices_frame()
    prices.loc[45, 'value'] = 1e10  # Implied average price lies far outside the day's trades.
    result = compute_daily_features(prices)
    assert pd.isna(result.loc[45, 'value_ratio_20d'])
    assert result.loc[46, 'value_ratio_20d'] == pytest.approx(1.)
    assert result.loc[45, 'ret_1d'] == 0.  # Independent valid closing/reference prices survive.


def test_out_of_range_last_price_does_not_create_closing_pressure():
    prices = prices_frame()
    prices.loc[45, 'close_raw'] = 120.
    result = compute_daily_features(prices)
    assert result.loc[45, ['close_position', 'final_vs_last_pct']].isna().all()
    assert result.loc[45, 'ret_1d'] == 0.


def test_boolean_and_absent_reference_are_not_assumed_prices():
    prices = prices_frame()
    prices['ref_price_raw'] = prices.ref_price_raw.astype(object)
    prices.loc[40, 'ref_price_raw'] = True
    assert pd.isna(compute_daily_features(prices).loc[40, 'ret_1d'])
    result = compute_daily_features(prices.drop(columns='ref_price_raw'))
    assert result.ret_1d.isna().all()


def test_market_lock_ratio_excludes_unknown_instead_of_counting_it_as_unlocked():
    one, two = prices_frame(2), prices_frame(2)
    one[['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw']] = 103.
    one['value'] = 103000.
    two['ref_price_raw'] = np.nan
    panel = pd.concat([compute_daily_features(one).assign(symbol='a'),
                       compute_daily_features(two).assign(symbol='b')], ignore_index=True)
    result = compute_market_features(panel)
    assert result.mkt_locked_up_ratio.eq(1.).all()
    assert result.mkt_symbols.eq(2).all()  # Missing observation remains in the coverage population.


def test_quality_masks_are_causal_and_preserve_unrelated_raw_columns():
    prices = prices_frame()
    prices.loc[40, 'ref_price_raw'] = np.nan
    prices.loc[42, 'high_raw'] = np.nan
    prices.loc[43, 'value'] = 1e10
    prices['adj_close'] = np.arange(len(prices)) * 1000.
    full = compute_daily_features(prices)
    for cut in [39, 40, 41, 42, 43, 44, 55]:
        partial = compute_daily_features(prices.iloc[:cut + 1])
        np.testing.assert_allclose(partial.iloc[-1][DAILY_FEATURES].astype(float),
                                   full.iloc[cut][DAILY_FEATURES].astype(float), equal_nan=True)


def test_official_close_outside_traded_range_is_preserved():
    prices = prices_frame()
    prices[['close_final_raw', 'ref_price_raw']] = 120.
    result = compute_daily_features(prices)
    assert result.ret_1d.iloc[1:].eq(0.).all()
    np.testing.assert_allclose(result.final_vs_last_pct, .2)
    assert result.locked_down.eq(0.).all()


def test_nullable_and_numeric_string_prices_are_normalized_without_imputation():
    prices = prices_frame()
    prices['ref_price_raw'] = prices.ref_price_raw.astype('string')
    prices.loc[40, 'ref_price_raw'] = pd.NA
    prices['close_raw'] = prices.close_raw.astype('Float64')
    prices.loc[41, 'close_raw'] = pd.NA
    result = compute_daily_features(prices)
    assert pd.isna(result.loc[40, 'ret_1d'])
    assert pd.isna(result.loc[41, 'final_vs_last_pct'])
    assert result.loc[42, 'ret_1d'] == 0.
    assert not np.isinf(result[DAILY_FEATURES].to_numpy(dtype=float)).any()


def test_raw_quality_implementation_is_bound_to_feature_provenance(monkeypatch):
    from pathlib import Path

    from features.provenance import feature_logic_fingerprint

    original = Path.read_bytes
    before = feature_logic_fingerprint()

    def changed(path):
        content = original(path)
        return content + b'\n# changed quality dependency\n' if path.as_posix().endswith('core/price_quality.py') else content

    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert feature_logic_fingerprint() != before
