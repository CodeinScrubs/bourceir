"""فرضیه‌های تازه فقط روی داده آزمون؛ قطع آینده، کیفیت و معنای دقیق شرط‌ها."""

import copy

import numpy as np
import pandas as pd
import pytest

from core.flow_filters import (
    FLOW_FILTER_VERSION,
    V4_FILTERS,
    effective_base_volume,
    filter_diagnostics_v4,
    flow_metrics,
    flow_metrics_v4,
)
from core.query_screener import hypothesis_evidence
from tests.test_flow_filters import history


def test_v4_preserves_every_v3_metric_and_does_not_read_adjusted_prices():
    p, c = history(80)
    original = flow_metrics(p, c)
    p['adj_close'], p['adjustment_factor'] = 999999, -12
    actual = flow_metrics_v4(p, c)
    pd.testing.assert_frame_equal(original, actual[original.columns])
    assert FLOW_FILTER_VERSION == 'flow-filters-v3'
    assert actual.historical_base_volume.isna().all()


def test_new_hypotheses_are_invariant_to_future_price_and_flow_poisoning():
    p, c = history(90)
    at = 75
    full = flow_metrics_v4(p, c)
    prefix = flow_metrics_v4(p.iloc[:at], c.iloc[:at])
    pd.testing.assert_frame_equal(full.iloc[:at].reset_index(drop=True), prefix)
    p.loc[at:, 'value'] = 1e30
    c.loc[at:, 'buy_i_value'] = 0
    poisoned = flow_metrics_v4(p, c)
    for name in V4_FILTERS:
        pd.testing.assert_frame_equal(filter_diagnostics_v4(full.iloc[:at], name), filter_diagnostics_v4(poisoned.iloc[:at], name))


def test_turnover_rank_bounds_include_missing_history_and_ties():
    p, c = history(70)
    prices, clients = [], []
    for i, scale in enumerate((1, 2, 3, 4, 5)):
        pp, cc = p.copy(), c.copy()
        pp['symbol'] = cc['symbol'] = f'نماد{i}'
        pp['volume'] *= scale
        pp['value'] *= scale
        for col in [x for x in cc if x not in ('date', 'symbol')]:
            cc[col] *= scale
        prices.append(pp)
        clients.append(cc)
    prices[-1].loc[prices[-1].index[-2], 'value'] = np.nan
    result = flow_metrics_v4(pd.concat(prices), pd.concat(clients))
    latest = result.loc[result.date == result.date.max()].set_index('symbol')
    assert latest.loc['نماد1', 'turnover_percentile_lower'] == .4
    assert latest.loc['نماد1', 'turnover_percentile_upper'] == .6
    # هیچ رتبه‌ای از اندازه شرکت یا سهام منتشرشده حدس زده نشده است.
    assert 'market_cap_rial' not in result


def test_unknown_conditions_never_pass_and_query_keeps_research_boundary():
    metrics = pd.DataFrame(dict(symbol=['الف'], date=['2024-01-01']))
    for name in V4_FILTERS:
        result = hypothesis_evidence(metrics, name)
        assert not result['accepted_for_signals']
        assert not result['diagnostics'].match.any()
        assert result['diagnostics'].iloc[0].status == 'نامعلوم'


def test_base_one_is_abolished_or_ineffective_not_a_high_coverage_ratio():
    base = effective_base_volume(pd.Series([1, 1000, np.nan, 0, -3, 1.5, float('inf')]))
    assert base.iloc[1] == 1000 and base.notna().sum() == 1


def test_reversal_needs_every_condition_and_does_not_claim_next_day_fill():
    metrics = pd.DataFrame(dict(symbol=['الف'], date=['2024-01-01'], value_prior_20d_mean_rial=[30e9],
        turnover_percentile_lower=[.2], turnover_percentile_upper=[.3], last_to_official=[1.011],
        range_position=[.8], buyer_power_value=[1.8], per_capita_buy_mean_3d_rial=[3e9],
        per_capita_buy_mean_5d_rial=[2e9], legal_buy_fraction=[.29], not_locked_up=[True]))
    result = hypothesis_evidence(metrics, 'intraday_reversal_accumulation')
    assert result['diagnostics'].match.all() and not result['next_session_execution_verified']
    metrics.loc[0, 'legal_buy_fraction'] = np.nan
    assert not filter_diagnostics_v4(metrics, 'intraday_reversal_accumulation').match.any()


def test_real_flow_missing_in_pullback_window_is_not_dropped():
    p, c = history(80)
    # جهش در t−۳ و دو اصلاح کاهشی در t−۲ و t−۱.
    for i, volume, last, reference in [(75, 400e6, 1020., 1000.), (76, 80e6, 1000., 1020.), (77, 70e6, 990., 1000.), (78, 100e6, 1005., 990.)]:
        p.loc[i, ['volume', 'value', 'open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'low_raw', 'high_raw']] = [
            volume, volume * last, 995., last, last, reference, min(980., last), max(1030., last)]
        c.loc[i, ['buy_i_volume', 'buy_n_volume', 'sell_i_volume', 'buy_i_value', 'sell_i_value']] = [
            volume * .8, volume * .2, volume * .5, volume * last * .8, volume * last * .5]
    # مرجع هر روز با پایانی قبلی برابر است؛ پنجره قابل مقایسه باید عبور کند.
    result = flow_metrics_v4(p, c)
    assert filter_diagnostics_v4(result, 'post_surge_pullback').match.iloc[78]
    gap = flow_metrics_v4(p.drop(index=76), c, calendar=p.date.tolist())
    target = gap.loc[gap.date == p.loc[78, 'date']]
    assert not filter_diagnostics_v4(target, 'post_surge_pullback').match.any()
    bad = copy.deepcopy(c)
    bad.loc[75, 'buy_i_value'] = np.nan
    actual = flow_metrics_v4(p, bad)
    assert actual.loc[75, 'buyer_power_value'] != actual.loc[75, 'buyer_power_value']
    assert not filter_diagnostics_v4(actual, 'post_surge_pullback').match.iloc[78]
    p.loc[60, 'ref_price_raw'] *= 2
    assert not filter_diagnostics_v4(flow_metrics_v4(p, c), 'post_surge_pullback').match.iloc[78]


def test_flow_v4_requires_explicit_source_before_any_database_access():
    from research.flow_v4_run import run_flow_v4
    with pytest.raises(ValueError, match='صریح'):
        run_flow_v4(None, source_id=None)
