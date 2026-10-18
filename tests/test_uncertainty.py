"""Mathematical/contract checks; synthetic returns do not validate a trading method."""

import numpy as np
import pandas as pd
import pytest

from research.uncertainty import _indices, _load_development_curve, compare_equity_curves


def curves(n=120):
    daily = np.sin(np.arange(n) / 7.) * .002
    dates = pd.bdate_range('2024-01-01', periods=n).strftime('%Y-%m-%d')
    return (pd.DataFrame(dict(date=dates, equity_rial=100000.*np.cumprod(1.+daily))),
            pd.DataFrame(dict(date=dates, equity_rial=100000.)))


def test_stationary_blocks_continue_wrap_and_restart_at_exact_supplied_positions():
    class Random:
        def random(self, n):
            return np.array([.9, .9, .9, .01, .9, .9])

        def integers(self, low, high, size):
            return np.array([4, 1, 1, 2, 1, 1])
    assert _indices(Random(), 6, 5).tolist() == [4, 5, 0, 2, 3, 4]


def test_paired_diagnostics_are_deterministic_and_do_not_approve_profit():
    a, b = curves()
    options = dict(initial_capital_rial=100000., replications=1000, trials_attempted=12)
    first = compare_equity_curves(a, b, **options)
    assert first == compare_equity_curves(a, b, **options)
    assert len(first['intervals']) == 3 and first['unknown_daily_pairs'] == 0
    assert first['accepted_for_signals'] is False and first['probability_of_future_profit'] is None
    assert first['selection_corrected'] is False and first['trials_attempted'] == 12
    # Null-like oscillations must not become precise positive evidence in this fixture.
    assert all(i['mean_daily_difference_lower'] < 0 < i['mean_daily_difference_upper'] for i in first['intervals'])


def test_centered_one_sided_bootstrap_reports_declared_family_and_resolution():
    a, b = curves()
    report = compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000,
        trials_attempted=8, family_comparisons=48, expected_sessions=a.date.tolist())
    for interval in report['intervals']:
        p = interval['one_sided_p_value']
        assert 0 < p <= 1 and interval['declared_family_bonferroni_p_value'] == min(1, p * 48)
        assert interval['p_value_resolution'] == 1 / 1001
    assert not report['selection_corrected'] and not report['historical_trial_count_verified']


def test_negative_edge_has_large_one_sided_p_value_and_cash_has_no_fake_interval():
    a, b = curves()
    a['equity_rial'] *= np.cumprod(np.full(len(a), .99))
    report = compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000)
    assert all(i['one_sided_p_value'] > .95 for i in report['intervals'])


@pytest.mark.parametrize('family', [0, True, 3])
def test_family_cannot_omit_attempted_configurations(family):
    a, b = curves()
    with pytest.raises(ValueError):
        compare_equity_curves(a, b, initial_capital_rial=100000., trials_attempted=8, family_comparisons=family)


def test_unknown_equity_and_next_day_return_are_retained_not_dropped():
    a, b = curves()
    a.loc[50, 'equity_rial'] = np.nan
    result = compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000)
    assert result['sessions'] == 120 and result['unknown_daily_pairs'] == 2
    assert result['mean_daily_return_difference'] is None and result['intervals'] == []
    assert 'unknown_daily_pairs' in result['blockers']


def test_cash_only_curves_cannot_manufacture_a_precision_claim():
    _, cash = curves()
    result = compare_equity_curves(cash, cash, initial_capital_rial=100000., replications=1000)
    assert result['mean_daily_return_difference'] == 0
    assert result['intervals'] == [] and 'degenerate_daily_difference' in result['blockers']


@pytest.mark.parametrize('change', ['dates', 'duplicate', 'text', 'negative', 'infinite'])
def test_invalid_or_misaligned_curves_fail_closed(change):
    a, b = curves()
    if change == 'dates':
        b = b.iloc[1:]
    elif change == 'duplicate':
        a.loc[1, 'date'] = a.loc[0, 'date']
    else:
        a['equity_rial'] = a.equity_rial.astype(object)
        a.loc[5, 'equity_rial'] = dict(text='bad', negative=-1., infinite=float('inf'))[change]
    with pytest.raises(ValueError):
        compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000)


@pytest.mark.parametrize('date', ['2024-1-1', '20240101', '2024-02-30'])
def test_noncanonical_or_impossible_dates_are_rejected(date):
    a, b = curves()
    a.loc[0, 'date'] = b.loc[0, 'date'] = date
    with pytest.raises(ValueError, match='ISO dates'):
        compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000)


def test_jointly_missing_session_blocks_statistics_with_expected_calendar():
    a, b = curves()
    expected = a.date.tolist()
    result = compare_equity_curves(a.drop(50), b.drop(50), initial_capital_rial=100000.,
                                  replications=1000, expected_sessions=expected)
    assert result['calendar_matches_curve'] is False and result['missing_expected_sessions'] == 1
    assert result['intervals'] == [] and result['mean_daily_return_difference'] is None
    assert 'session_calendar_mismatch' in result['blockers']


def test_matching_calendar_does_not_approve_authenticity_or_optional_stopping():
    a, b = curves()
    result = compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000,
                                  expected_sessions=a.date.tolist())
    assert result['calendar_matches_curve'] and len(result['intervals']) == 3
    assert 'calendar_source_not_verified_by_this_tool' in result['blockers']
    assert 'fixed_sample_only_not_valid_for_optional_stopping' in result['blockers']
    assert not result['accepted_for_signals']


def test_sealed_curve_rejected_before_decoding_equity(tmp_path, monkeypatch):
    path = tmp_path / 'sealed.parquet'
    pd.DataFrame(dict(date=['2025-03-21'], equity_rial=[123456789.])).to_parquet(path)
    original, calls = pd.read_parquet, []

    def watched(*args, **kwargs):
        calls.append(kwargs['columns'])
        return original(*args, **kwargs)

    monkeypatch.setattr('research.uncertainty.pd.read_parquet', watched)
    with pytest.raises(ValueError, match='sealed-period'):
        _load_development_curve(path, '2025-03-21')
    assert calls == [['date']]


def test_curve_decoding_uses_one_immutable_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'development.parquet'
    original_curve, _ = curves()
    original_curve.to_parquet(path)
    original_read, calls = pd.read_parquet, []

    def change_after_date_read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        calls.append(kwargs['columns'])
        if kwargs['columns'] == ['date']:
            pd.DataFrame(dict(date=['2026-10-15'], equity_rial=[987654321.])).to_parquet(path)
        return result

    monkeypatch.setattr('research.uncertainty.pd.read_parquet', change_after_date_read)
    frame = _load_development_curve(path, '2025-03-21')
    pd.testing.assert_frame_equal(frame, original_curve)
    assert calls == [['date'], ['date', 'equity_rial']] and len(frame.attrs['input_sha256']) == 64


def test_cli_input_collision_preserves_input_before_decoding(tmp_path, monkeypatch):
    import sys

    from config import settings
    from research.uncertainty import main

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    source = tmp_path / 'private' / 'input.json'
    source.parent.mkdir()
    source.write_text('private input is not a report')
    before = source.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['uncertainty', '--strategy', str(source), '--benchmark', str(source),
                                    '--initial-capital-rial', '100000', '--output', str(source)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and source.read_bytes() == before
