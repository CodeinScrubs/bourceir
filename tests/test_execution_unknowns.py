"""A missing entry bar is not evidence of an unfilled order or zero return."""

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import replay_portfolio
from research.dip import DipRule, simulate, simulate_symbol
from research.dip_run import evaluate
from research.labels import compute_labels, market_calendar
from research.probability import resolved_outcomes
from tests.test_dip import CAL, _flat, _frame
from tests.test_portfolio import config, history, picks
from tests.test_portfolio_marks import book


@pytest.mark.parametrize('column,invalid', [
    ('ref_price_raw', True), ('ref_price_raw', 'invalid'), ('ref_price_raw', None),
    ('low_raw', True), ('high_raw', True), ('volume', True), ('volume', 1.5),
    ('volume', -1), ('volume', None), ('value', True), ('value', None),
    ('volume', 0),
])
def test_invalid_label_entry_never_becomes_a_known_result(column, invalid):
    prices, days = history(5, ('a',))
    prices[column] = prices[column].astype(object)
    prices.loc[1, column] = invalid
    item = compute_labels(prices, horizons=[1], calendar=days).loc[lambda x: x.date.eq(days[0])].iloc[0]
    assert item.label_status_1d == 'invalid_data'
    assert not item.entry_executable and pd.isna(item.net_ret_1d)


@pytest.mark.parametrize('column,invalid', [
    ('ref_price_raw', True), ('ref_price_raw', 'invalid'), ('ref_price_raw', None),
    ('open_raw', True), ('high_raw', True), ('low_raw', True),
    ('volume', True), ('volume', 1.5), ('volume', -1), ('volume', None),
    ('value', True), ('value', None), ('volume', 0),
])
def test_invalid_dip_entry_never_becomes_a_known_fill_or_no_fill(column, invalid):
    rows = _flat()
    rows[1] = (CAL[1], 97, 99, 96.5, 98, 100)
    frame = _frame(rows)
    frame[column] = frame[column].astype(object)
    frame.loc[1, column] = invalid
    item = simulate_symbol(frame, CAL, DipRule()).iloc[0]
    assert item.dip_status == 'invalid'
    assert pd.isna(item.entry_filled) and pd.isna(item.net_ret)


@pytest.mark.parametrize('column,invalid', [('volume', True), ('value', True), ('ref_price_raw', True)])
def test_invalid_dip_holding_bar_cannot_support_a_target_exit(column, invalid):
    rows = _flat()
    rows[1] = (CAL[1], 99, 100, 97.5, 98, 100)
    rows[2] = (CAL[2], 98.5, 101.5, 98, 101, 98)
    frame = _frame(rows)
    frame[column] = frame[column].astype(object)
    frame.loc[2, column] = invalid
    item = simulate_symbol(frame, CAL, DipRule()).iloc[0]
    assert item.dip_status == 'invalid' and item.entry_filled == 1
    assert item.exit_date == '' and pd.isna(item.net_ret)


@pytest.mark.parametrize('invalid', [True, None, -1, 0])
def test_dip_pipeline_retains_invalid_totals_instead_of_dropping_the_entry(invalid):
    rows = _flat()
    rows[1] = (CAL[1], 97, 99, 96.5, 98, 100)
    prices = _frame(rows).assign(symbol='a')
    prices['volume'] = prices['volume'].astype(object)
    prices.loc[1, 'volume'] = invalid  # Its positive value makes zero contradictory.
    item = simulate(prices, CAL, DipRule()).loc[lambda x: x.date.eq(CAL[0])].iloc[0]
    assert item.dip_status == 'invalid' and pd.isna(item.entry_filled) and pd.isna(item.net_ret)


def test_dip_verified_zero_activity_is_a_missing_session_not_a_zero_return():
    prices = _frame(_flat()).assign(symbol='a')
    prices.loc[1, ['volume', 'value']] = 0
    item = simulate(prices, CAL, DipRule()).loc[lambda x: x.date.eq(CAL[0])].iloc[0]
    assert item.dip_status == 'no_session' and pd.isna(item.entry_filled) and pd.isna(item.net_ret)


def test_market_calendar_never_counts_boolean_volume_as_active():
    prices = pd.DataFrame({'symbol': ['a', 'b'], 'date': ['2024-01-01'] * 2,
                           'volume': pd.Series([True, 100], dtype=object)})
    assert market_calendar(prices, min_symbols=2).size == 0


def test_invalid_holding_prefix_is_not_just_censored_when_no_valid_exit_is_observed():
    prices, days = history(3, ('a',))
    prices['ref_price_raw'] = prices['ref_price_raw'].astype(object)
    prices.loc[2, 'ref_price_raw'] = True
    item = compute_labels(prices, horizons=[1], calendar=days).loc[lambda x: x.date.eq(days[0])].iloc[0]
    assert item.entry_executable and item.label_status_1d == 'invalid_data' and pd.isna(item.net_ret_1d)


def test_missing_entry_label_does_not_become_known_zero():
    prices, days = history(4, ('a',))
    prices = prices.loc[prices.date != days[1]]
    labels = compute_labels(prices, horizons=[1], calendar=days)
    item = labels.loc[labels.date.eq(days[0])].iloc[0]
    assert item.label_status_1d == 'invalid_data' and not item.entry_executable
    result = resolved_outcomes(labels.loc[labels.date.eq(days[0])], horizon=1, as_of='2024-01-10')
    assert result.iloc[0].state == 'unknown' and pd.isna(result.iloc[0].normal_net)


def test_unobserved_entry_bar_invalidates_shared_portfolio_return():
    prices, days = history(5, ('a',))
    prices = prices.loc[prices.date != days[1]]
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days, config(max_positions=1))
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.metrics['max_drawdown'] is None and pd.isna(result.equity.iloc[-1].equity_rial)
    assert 'no_session' in set(result.decisions.status) and result.executions.empty


def test_missing_dip_entry_remains_unknown_in_simulation_and_summary():
    rows = [row for i, row in enumerate(_flat()) if i != 1]
    result = simulate_symbol(_frame(rows), CAL, DipRule()).iloc[0]
    assert result['dip_status'] == 'no_session' and pd.isna(result['entry_filled'])
    picks_frame = pd.DataFrame([dict(date=CAL[0], symbol='a')])
    trade = pd.DataFrame([dict(result, symbol='a')])
    summary = evaluate(trade, picks_frame, pd.Index([CAL[0]]), None, 1, pd.Series(dtype=float))
    assert summary['entry_unknown_picks'] == 1 and summary['cohort_unknown_days'] == 1
    assert np.isnan(summary['mean_daily_cohort'])


@pytest.mark.parametrize('recorded_entry_filled', [0, 1, None])
def test_legacy_no_session_even_with_zero_flag_cannot_become_idle_cash(recorded_entry_filled):
    picks_frame = pd.DataFrame([dict(date=CAL[0], symbol='a')])
    trade = pd.DataFrame([dict(date=CAL[0], symbol='a', dip_status='no_session',
                               entry_filled=recorded_entry_filled, net_ret=np.nan,
                               exit_kind='', days_held=np.nan)])
    summary = evaluate(trade, picks_frame, pd.Index([CAL[0]]), None, 1, pd.Series(dtype=float))
    assert summary['entry_known_picks'] == 0 and summary['entry_unknown_picks'] == 1
    assert summary['cohort_unknown_days'] == 1 and np.isnan(summary['mean_daily_cohort'])


def test_legacy_censored_without_entry_flag_is_not_assumed_filled():
    picks_frame = pd.DataFrame([dict(date=CAL[0], symbol='a')])
    trade = pd.DataFrame([dict(date=CAL[0], symbol='a', dip_status='censored',
                               net_ret=np.nan, exit_kind='', days_held=np.nan)])
    summary = evaluate(trade, picks_frame, pd.Index([CAL[0]]), None, 1, pd.Series(dtype=float))
    assert summary['entry_known_picks'] == 0 and summary['entry_unknown_picks'] == 1
    assert np.isnan(summary['fill_rate']) and summary['cohort_unknown_days'] == 1


@pytest.mark.parametrize('missing_index,horizon', [(2, 1), (2, 2), (3, 2)])
def test_missing_holding_bar_does_not_become_a_known_delayed_label(missing_index, horizon):
    prices, days = history(7, ('a',))
    prices = prices.loc[prices.date.ne(days[missing_index])]
    item = compute_labels(prices, horizons=[horizon], calendar=days).loc[lambda x: x.date.eq(days[0])].iloc[0]
    assert item[f'label_status_{horizon}d'] == 'invalid_data'
    assert item[f'exit_date_{horizon}d'] == ''
    for field in ('net_ret', 'net_ret_{}d_worst', 'mfe', 'mae', 'corporate_actions', 'exit_delay'):
        column = field.format(horizon) if '{}' in field else f'{field}_{horizon}d'
        assert pd.isna(item[column])


def test_missing_bar_after_a_completed_exit_does_not_invalidate_label():
    prices, days = history(7, ('a',))
    prices = prices.loc[prices.date.ne(days[4])]
    item = compute_labels(prices, horizons=[1], calendar=days).loc[lambda x: x.date.eq(days[0])].iloc[0]
    assert item.label_status_1d == 'complete' and item.exit_date_1d == days[2]


def test_missing_dip_holding_bar_is_not_a_later_modeled_exit():
    rows = _flat()
    rows[1] = (CAL[1], 99, 100, 97.5, 98, 100)
    rows = [row for i, row in enumerate(rows) if i != 2]
    item = simulate_symbol(_frame(rows), CAL, DipRule()).iloc[0]
    assert item.dip_status == 'invalid' and item.entry_filled == 1
    assert item.exit_date == '' and pd.isna(item.net_ret)


@pytest.mark.parametrize('kind', ['synthetic', 'cash', 'bonus', 'rights'])
def test_missing_required_exit_cannot_be_assumed_delayed(kind):
    prices, days = history(7, ('a',))
    prices = prices.loc[prices.date.ne(days[2])]
    result = replay_portfolio(prices, picks(days[:1], ('a',)), days,
                              config(max_positions=1, horizon=1), **book(kind))
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.metrics['max_drawdown'] is None and pd.isna(result.equity.iloc[-1].equity_rial)
    assert 'missing_exit_bar' in set(result.decisions.status)
    assert result.executions.side.tolist() == ['buy']
