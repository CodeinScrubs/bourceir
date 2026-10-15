"""Synthetic fixtures only: exact units, missingness, causality and action boundaries."""

import numpy as np
import pandas as pd
import pytest

from core.flow_filters import filter_conditions, filter_diagnostics, flow_metrics
from core.flow_intervals import estimate_retail_intervals
from database.flow_data import load_flow_inputs


def history(n=85):
    dates = pd.bdate_range('2024-01-01', periods=n).strftime('%Y-%m-%d')
    p = pd.DataFrame(dict(symbol='کیان', date=dates, open_raw=1000., low_raw=998., high_raw=1002.,
        close_raw=1000., close_final_raw=1000., ref_price_raw=1000., volume=100_000_000.,
        value=100_000_000_000., trade_count=200.))
    c = pd.DataFrame(dict(symbol='کیان', date=dates, buy_i_volume=80_000_000., buy_n_volume=20_000_000.,
        sell_i_volume=50_000_000., buy_i_count=50., sell_i_count=400., buy_i_value=80_080_000_000.,
        sell_i_value=49_950_000_000.))
    return p, c


def test_exact_daily_mean_is_not_pooled_and_does_not_round_before_comparison():
    p, c = history()
    c.loc[c.index[-3:], ['buy_i_value', 'buy_i_count', 'buy_i_volume']] = [[100_100., 1., 100.], [200_200., 2., 200.], [900_900., 3., 900.]]
    c.loc[c.index[-3:],'buy_n_volume'] = p.loc[p.index[-3:], 'volume']-c.loc[c.index[-3:], 'buy_i_volume']
    row = flow_metrics(p, c).iloc[-1]
    assert row.per_capita_buy_mean_3d_rial == pytest.approx(500_500/3)
    assert row.per_capita_buy_pooled_3d_rial == pytest.approx(1_201_200/6)
    assert row.buyer_power_volume != row.buyer_power_value
    assert row.per_capita_buy_rial == 300_300


def test_prior_volume_windows_exclude_today_and_require_full_observations():
    p, c = history()
    p.loc[p.index[-1], 'volume'] *= 4
    full = flow_metrics(p, c)
    assert full.iloc[-1].volume_ratio_30d == 4
    assert pd.isna(full.iloc[29].volume_ratio_30d)
    assert full.iloc[30].volume_ratio_30d == 1
    p.loc[p.index[-3], 'volume'] = np.nan
    assert pd.isna(flow_metrics(p, c).iloc[-1].volume_ratio_30d)


@pytest.mark.parametrize('column,value', [('buy_i_count', 0), ('buy_i_count', -1), ('buy_i_count', .5),
    ('sell_i_value', np.inf), ('buy_i_value', np.nan)])
def test_invalid_or_missing_side_never_makes_default_power(column, value):
    p, c = history()
    c.loc[c.index[-1], column] = value
    row = flow_metrics(p, c).iloc[-1]
    assert pd.isna(row.buyer_power_value)


def test_missing_history_session_is_not_dropped_from_three_or_five_session_mean():
    p, c = history()
    c = c.drop(c.index[-2])
    row = flow_metrics(p, c).iloc[-1]
    assert pd.isna(row.per_capita_buy_mean_3d_rial)
    assert pd.isna(row.per_capita_buy_mean_5d_rial)
    assert pd.notna(row.per_capita_buy_rial)


def test_official_close_and_unknown_original_conditions_are_not_substituted():
    p, c = history()
    p.loc[p.index[-1], 'close_final_raw'] = np.nan
    metrics = flow_metrics(p, c).tail(1)
    assert pd.isna(metrics.iloc[0].last_above_official)
    assert pd.isna(metrics.iloc[0].last_vs_official_pp)
    checks = filter_conditions(metrics, 'special_original')
    assert checks.shape[1] == 9 and checks.isna().sum(axis=1).iloc[0] == 4
    diagnostics = filter_diagnostics(metrics, 'special_original')
    assert not diagnostics.iloc[0].match
    assert diagnostics.iloc[0].known_conditions == 5


def test_temporal_truncation_future_poisoning_and_normalization():
    p, c = history()
    expected = flow_metrics(p.iloc[:70], c.iloc[:70])
    p.loc[p.index[70:], 'high_raw'] = 999_999_999
    c.loc[c.index[70:], 'buy_i_value'] = 1e30
    pd.testing.assert_frame_equal(flow_metrics(p, c).iloc[:70], expected)
    p['symbol'], c['symbol'] = 'كيان', 'کیان'
    assert flow_metrics(p, c).symbol.unique().tolist() == ['کیان']


def test_full_sixty_session_breakout_survives_action_and_does_not_sample_eight_days():
    p, c = history()
    p.loc[p.index[-1], ['high_raw', 'close_raw', 'close_final_raw']] = [1020., 1020., 1010.]
    before = flow_metrics(p, c)
    assert bool(before.iloc[-1].breakout_60d)
    for col in ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']:
        p.loc[p.index >= 35, col] *= .5
    assert bool(flow_metrics(p, c).iloc[-1].breakout_60d)
    # A high on an otherwise omitted day must prevent a full-window breakout.
    p.loc[67, 'high_raw'] = 700.
    assert not bool(flow_metrics(p, c).iloc[-1].breakout_60d)
    p.loc[67, 'ref_price_raw'] = np.nan
    assert pd.isna(flow_metrics(p, c).iloc[-1].breakout_60d)


def test_duplicate_keys_rejected_and_unknown_filter_rejected():
    p, c = history()
    with pytest.raises(ValueError, match='یکتا'):
        flow_metrics(pd.concat([p, p.tail(1)]), c)
    with pytest.raises(ValueError):
        filter_conditions(flow_metrics(p, c), 'arbitrary-score')


def test_cached_missing_condition_explanations_match_each_nullable_pattern():
    p, c = history()
    c.loc[80:, 'buy_i_value'] = np.nan
    metrics = flow_metrics(p, c)
    for name in ('special_original', 'retail_acceleration', 'volume_breakout', 'daily_pressure'):
        conditions = filter_conditions(metrics, name)
        expected = conditions.apply(lambda row: '؛ '.join(row.index[row.isna()]), axis=1)
        assert filter_diagnostics(metrics, name).missing_conditions.tolist() == expected.tolist()


def sample(seconds, *, bv=1000, bc=10, sv=500, sc=10, day='2026-10-14', **kwargs):
    timestamp = f'{day}T09:{seconds//60:02d}:{seconds%60:02d}+03:30'
    return dict(symbol='کیان', captured_at=timestamp, provider_timestamp=timestamp, source='tsetmc', price_unit='Rial',
        volume=max(bv, sv)*2, last_price=10_000., real_buy_volume=bv, real_sell_volume=sv,
        real_buy_count=bc, real_sell_count=sc, **kwargs)


def test_counter_estimate_accumulates_volume_without_claiming_single_trades():
    report = estimate_retail_intervals([sample(0), sample(30, bv=2000), sample(60, bv=4000, bc=12)], threshold_rial=10_000_000)
    event = report['events'][0]
    assert event['estimated_per_new_count_rial'] == 15_000_000
    assert event['delta_count'] == 2 and event['delta_volume'] == 3000
    assert not event['single_trade_proven'] and not report['individual_trades_observed']
    assert not report['independent_client_counter_clock'] and not report['complete_session_proven']
    assert estimate_retail_intervals([sample(0), sample(30, bv=3000, bc=12)], threshold_rial=10_000_000)['events'] == []


@pytest.mark.parametrize('records,reason', [
    ([sample(0), sample(60, bv=9000, bc=11)], 'archive_gap'),
    ([sample(30), sample(0, bv=9000, bc=11)], 'nonmonotonic_clock'),
    ([sample(0), sample(30, bv=900, bc=9)], 'counter_revision_or_reset'),
    ([sample(0), sample(30, bv=9000, bc=11, day='2026-10-15')], 'new_session'),
    ([sample(0), sample(30, bv=9000, bc=0)], 'invalid_snapshot')])
def test_counter_resets_revisions_gaps_and_invalid_values_do_not_produce_events(records, reason):
    report = estimate_retail_intervals(records, threshold_rial=1)
    assert report['events'] == [] and report['rejected_segments'][reason] == 1


def test_pending_volume_is_forgotten_across_gap_or_revision():
    report = estimate_retail_intervals([sample(0), sample(30, bv=3000), sample(120, bv=9000),
        sample(150, bv=9100, bc=11)], threshold_rial=10_000_000)
    assert report['events'] == []


def test_mixed_symbol_counters_are_not_subtracted():
    second = sample(30)
    second['symbol'] = 'دیگر'
    with pytest.raises(ValueError, match='مختلف'):
        estimate_retail_intervals([sample(0), second])


@pytest.mark.parametrize('unit', [None, 'Toman'])
def test_interval_unknown_or_incorrect_price_unit_does_not_make_money_estimate(unit):
    row = sample(30, bv=9000, bc=11)
    row['price_unit'] = unit
    report = estimate_retail_intervals([sample(0), row], threshold_rial=1)
    assert report['events'] == [] and report['rejected_segments']['invalid_snapshot'] == 1


def test_iterator_input_is_read_and_out_of_order_row_cannot_double_count():
    records = [sample(0), sample(30, bv=3000, bc=11), sample(15, bv=2000, bc=10),
               sample(45, bv=6000, bc=12), sample(60, bv=6500, bc=13)]
    report = estimate_retail_intervals(iter(records), threshold_rial=10_000_000)
    assert len(report['events']) == 1
    assert report['rejected_segments']['nonmonotonic_clock'] == 1


def test_loader_filters_scope_and_exclusive_boundary_in_sql(db):
    p, c = history(3)
    for code, symbol in [(300, 'داخل'), (309, 'پایه'), (305, 'صندوق')]:
        sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=str(code), instrument_type_code=code))
        db.upsert_daily_prices_df(sid, p.drop(columns='symbol'))
        db.upsert_client_types_df(sid, c.drop(columns='symbol'))
    prices, clients = load_flow_inputs(db, before=p.date.iloc[-1], tail_per_symbol=1)
    assert prices.symbol.tolist() == ['داخل'] and clients.symbol.tolist() == ['داخل']
    assert prices.date.tolist() == [p.date.iloc[-2]]


def test_power_and_positive_net_flow_are_different_conditions():
    p, c = history()
    c['buy_i_value'], c['sell_i_value'] = 10_000_000_000., 20_000_000_000.
    c['buy_i_volume'],c['sell_i_volume'],c['buy_n_volume'] = 10_000_000.,20_000_000.,90_000_000.
    row = flow_metrics(p, c).iloc[-1]
    assert row.buyer_power_value > 2 and row.net_real_value_rial < 0
