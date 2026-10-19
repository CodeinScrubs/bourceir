"""ورودی خراب و تازگی نماد دیگر نباید شاهد سلامت یا جریان بسازند."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from core.flow_intervals import estimate_retail_intervals
from core.timeutils import now_tehran
from data_collector.monitor import DataFreshnessMonitor
from ops.operator_status import operator_status
from tests.test_flow_filters import sample
from tests.test_operator_status import _matching_metrics


@pytest.mark.parametrize('bad', [True, np.bool_(True), -1., 0., float('nan'), float('inf'), '1000', None])
def test_invalid_price_is_unknown_and_cannot_be_a_bad_tick_baseline(bad):
    monitor = DataFreshnessMonitor()
    now = now_tehran()
    result = monitor.inspect_snapshot('الف', dict(last_price=bad, provider_timestamp=now.isoformat()), now=now)
    assert result['last_price'] is None and not result['is_healthy']
    assert result['warnings'] and 'الف' not in monitor.last_seen_prices
    followup = monitor.inspect_snapshot('الف', dict(last_price=1000.,
        provider_timestamp=(now + timedelta(seconds=1)).isoformat()), now=now + timedelta(seconds=1))
    assert not any('جهش مشکوک' in warning for warning in followup['warnings'])


def test_naive_check_time_is_rejected_instead_of_using_machine_timezone():
    with pytest.raises(ValueError):
        DataFreshnessMonitor().inspect_snapshot('الف', {}, now=now_tehran().replace(tzinfo=None))


def test_assumed_source_timezone_is_disclosed_and_never_healthy():
    now = now_tehran()
    result = DataFreshnessMonitor().inspect_snapshot('الف', dict(last_price=1000.,
        provider_timestamp=now.replace(tzinfo=None).isoformat()), now=now)
    assert result['delay_seconds'] == 0 and not result['is_healthy'] and result['warnings']


@pytest.mark.parametrize('ahead', [.1, 5.9, 60.])
def test_future_price_time_including_subsecond_is_not_fresh(ahead):
    now = now_tehran()
    result = DataFreshnessMonitor().inspect_snapshot('الف', dict(last_price=1000.,
        provider_timestamp=(now + timedelta(seconds=ahead)).isoformat()), now=now)
    assert result['delay_seconds'] is None and not result['is_healthy']
    assert result['warnings']


@pytest.mark.parametrize('field,bad', [('volume', 18000.5), ('real_buy_volume', 9000.5),
    ('real_sell_volume', 500.5), ('real_buy_count', '11'), ('last_price', np.bool_(True)),
    ('real_buy_count', 2**53), ('volume', '18000'), ('volume', 2**53+1)])
def test_fractional_coerced_or_inexact_counters_do_not_make_an_event(field, bad):
    row = sample(30, bv=9000, bc=11)
    row[field] = bad
    result = estimate_retail_intervals([sample(0), row], threshold_rial=1)
    assert result['events'] == [] and result['rejected_segments']['invalid_snapshot'] == 1


def test_integer_valued_native_counters_still_produce_a_labeled_proxy():
    result = estimate_retail_intervals([sample(0), sample(30, bv=9000, bc=11)], threshold_rial=1)
    assert result['events'] and not result['individual_trades_observed']


def test_each_candidate_date_is_checked_even_when_another_symbol_is_current(db):
    old = _matching_metrics()
    current = old.copy()
    current['symbol'], current['date'] = 'ب', '2024-01-02'
    before = db.conn.total_changes
    result = operator_status(db, pd.concat([old, current], ignore_index=True), pd.DataFrame(),
        at=pd.Timestamp('2024-01-03T10:00:00+03:30').to_pydatetime())
    first = next(row for row in result['candidates'] if row['symbol'] == 'الف')
    assert any('روز فیلتر' in reason for reason in first['blockers'])
    assert not result['filter_data_current']
    assert not first['accepted_for_signals'] and first['signal_quantity'] is None
    assert db.conn.total_changes == before
