"""Cache parity and invalidation; no market data or operational database is created."""

import pandas as pd
import pytest

from config import settings
from core.flow_filters import flow_metrics
from dashboard import flow_lab
from tests.test_flow_filters import history


@pytest.fixture(autouse=True)
def isolated_flow_cache():
    flow_lab._cached_v3_metrics.clear()
    yield
    flow_lab._cached_v3_metrics.clear()


def test_v3_cached_result_matches_calculation_and_callers_cannot_mutate_cache(monkeypatch):
    prices, clients = history()
    calls = []

    def calculate(p, c):
        calls.append(True)
        return flow_metrics(p, c)

    monkeypatch.setattr(flow_lab, 'flow_metrics', calculate)
    expected = flow_metrics(prices, clients)
    first = flow_lab._v3_metrics(prices, clients)
    pd.testing.assert_frame_equal(first, expected)
    first.loc[0, 'value'] = -1
    pd.testing.assert_frame_equal(flow_lab._v3_metrics(prices.copy(), clients.copy()), expected)
    assert len(calls) == 1


def test_v3_cache_covers_unsampled_rows_client_corrections_and_effective_settings(monkeypatch):
    prices = pd.DataFrame({'value': range(60001)})
    clients = pd.DataFrame({'buy_i_value': [100] * 60001})
    calls = []

    def calculate(p, c):
        calls.append(True)
        return pd.DataFrame({'observed': [p.value.iloc[30001] + c.buy_i_value.iloc[30001]]})

    monkeypatch.setattr(flow_lab, 'flow_metrics', calculate)
    first = flow_lab._v3_metrics(prices, clients)
    assert flow_lab._v3_metrics(prices.copy(), clients.copy()).equals(first)
    assert len(calls) == 1
    prices.loc[30001, 'value'] += 1
    assert not flow_lab._v3_metrics(prices, clients).equals(first)
    assert len(calls) == 2
    clients.loc[30001, 'buy_i_value'] += 1
    flow_lab._v3_metrics(prices, clients)
    assert len(calls) == 3
    monkeypatch.setitem(settings.BACKTEST_CONFIG, 'lock_min_move_pct', .006)
    flow_lab._v3_metrics(prices, clients)
    assert len(calls) == 4


def test_v3_cache_invalidates_on_dependency_code_revision(monkeypatch):
    prices, clients = history()
    calls = []

    def calculate(p, c):
        calls.append(True)
        return flow_metrics(p, c)

    monkeypatch.setattr(flow_lab, 'flow_metrics', calculate)
    flow_lab._v3_metrics(prices, clients)
    from pathlib import Path

    original = Path.read_bytes

    def revised_source(path):
        content = original(path)
        return content + b'\n# revised\n' if path == settings.BASE_DIR / 'core/client_quality.py' else content

    monkeypatch.setattr(Path, 'read_bytes', revised_source)
    flow_lab._v3_metrics(prices, clients)
    assert len(calls) == 2
