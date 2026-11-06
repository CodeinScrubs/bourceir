import json

import pandas as pd
import pytest

from config import settings
from core.market_index import INDEX_CODES, project_index_history
from data_collector.tsetmc import TsetmcProvider
from research.index_benchmark import TREND_SESSIONS, compare_index_path, dual_exchange_trend

RECEIVED = '2026-01-03T10:00:00+03:30'


def _row(stamp, **values):
    return {'dEven': stamp, 'insCode': int(INDEX_CODES['tse_total']), 'xNivInuClMresIbs': 135.5, **values}


def _body(*rows):
    return json.dumps({'indexB2': list(rows)}).encode()


def test_provider_returns_only_in_window_index_points(monkeypatch):
    provider = TsetmcProvider()
    body = _body(_row(20240102), _row(20240101, xNivInuClMresIbs=99),
                 _row(20250401, insCode='irrelevant', xNivInuClMresIbs='outside the window'))
    monkeypatch.setattr(provider, '_get_quote_context_json', lambda code, url, **_: dict(
        content=body, url=url, ins_code=code, received_at=RECEIVED))
    try:
        result = provider.get_development_index_history('tse_total', '2024-01-01', '2025-01-01')
    finally:
        provider.close()
    assert 'content' not in result['receipt']
    assert result['projection']['unit'] == 'index_points'
    assert list(result['projection']['levels'].items()) == [('2024-01-01', 99), ('2024-01-02', 135.5)]
    assert result['projection']['contemporaneous_publication_verified'] is False


@pytest.mark.parametrize('rows', [
    [_row(20240101, insCode=1)], [_row(20240101, xNivInuClMresIbs=0)],
    [_row(20240101, xNivInuClMresIbs=float('inf'))], [_row(20240230)],
    [_row(20240101), _row(20240101)], [_row(20260104)],
])
def test_index_source_rejects_ambiguous_or_invalid_history(rows):
    with pytest.raises(ValueError):
        project_index_history(_body(*rows), 'tse_total', '2024-01-01', '2025-01-01', received_at=RECEIVED)


def test_index_provider_refuses_sealed_window_before_network(monkeypatch):
    monkeypatch.setattr(TsetmcProvider, '_get_quote_context_json', lambda *_a, **_k: pytest.fail('network called'))
    provider = TsetmcProvider()
    try:
        with pytest.raises(ValueError, match='pre-holdout'):
            provider.get_development_index_history('tse_total', settings.RESEARCH_CONFIG['holdout_start'], '2026-01-01')
    finally:
        provider.close()


def test_exchange_gate_is_causal_and_rejects_absent_or_additional_sessions():
    days = pd.date_range('2020-01-01', periods=TREND_SESSIONS+4, freq='2D').strftime('%Y-%m-%d').tolist()
    levels = dict(zip(days, range(100, 100+len(days))))
    result = dual_exchange_trend(days, levels, levels)
    assert result.gate.iloc[:TREND_SESSIONS].isna().all()
    assert result.gate.iloc[TREND_SESSIONS:].all()
    changed = {**levels, days[-1]: 1}
    revised = dual_exchange_trend(days, levels, changed)
    pd.testing.assert_frame_equal(revised.iloc[:-1], result.iloc[:-1])
    assert not revised.gate.iloc[-1]
    no_date = dict(levels)
    del no_date[days[-2]]
    assert pd.isna(dual_exchange_trend(days, levels, no_date).gate.iloc[-1])
    extra = {**levels, '2020-02-13': 120}
    assert pd.isna(dual_exchange_trend(days, levels, extra).gate.iloc[-1])


def _account(days, values):
    return pd.DataFrame(dict(date=days, equity_rial=values, stale_positions=0, stale_fractional_exposures=0))


def test_index_comparison_uses_same_closes_for_each_calendar_year_window():
    days = ['2019-01-01', '2019-06-01', '2020-01-01', '2020-01-02']
    levels = dict(zip(days, [100, 110, 150, 150]))
    summary, rows = compare_index_path(_account(days, [100, 100, 125, 140]), days, levels)
    assert summary['account_minus_index'] == pytest.approx(-.10)
    assert summary['rolling_paired'] == 2
    assert summary['rolling_account_below_target'] == 1
    assert summary['rolling_account_below_index'] == 2
    assert rows[1]['base_date'] == days[0] and rows[1]['base_lag_days'] == 1


def test_index_comparison_retains_unknown_after_missing_account_or_index_day():
    days = ['2019-01-01', '2019-06-01', '2020-01-01']
    summary, rows = compare_index_path(_account(days[:2], [100, 100]), days, {days[0]: 100, days[2]: 140})
    assert summary['account_unavailable_sessions'] == 1
    assert summary['missing_index_dates'] == [days[1]]
    assert summary['rolling_account_unknown'] == summary['rolling_index_unknown'] == 1
    assert summary['rolling_paired'] == summary['rolling_account_below_target'] == 0
    assert rows[0]['target_met'] is None


def test_index_comparison_does_not_join_through_stale_or_off_calendar_marks():
    days = ['2019-01-01', '2019-06-01', '2020-01-01']
    equity = _account(days, [100, 110, 140])
    equity.loc[1, 'stale_positions'] = 1
    levels = {**dict.fromkeys(days, 100), '2019-07-01': 110}
    summary, rows = compare_index_path(equity, days, levels)
    assert summary['additional_index_dates'] == ['2019-07-01']
    assert summary['missing_index_dates'] == []
    assert summary['account_minus_index'] is rows[0]['account_return'] is rows[0]['index_return'] is None
    with pytest.raises(ValueError, match='exact prefix'):
        compare_index_path(equity.iloc[[0, 2]], days, levels)
