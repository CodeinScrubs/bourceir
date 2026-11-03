"""Synthetic rows for diagnostics; no financial profitability assertions."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from ops.hunter_attribution import checked_panel, diagnose_panel, main, read_panel

CUTOFF = '2025-03-21'


def panel():
    return pd.DataFrame({
        'fold': [0] * 6, 'date': ['2024-01-01'] * 6,
        'symbol': ['TEST-A', 'TEST-B', 'TEST-C', 'TEST-D', 'TEST-E', 'TEST-F'],
        'status': ['forecasted'] * 5 + ['unknown_features'],
        'normal_expected_net': [-.03, -.02, -.01, .01, .02, np.nan],
        'stress_expected_net': [-.04, -.03, -.02, -.01, .01, np.nan],
        'state': ['complete', 'entry_blocked', 'unknown', 'complete', 'complete', 'complete'],
        'resolved_at': ['2024-01-04', '2024-01-02', None, '2024-01-04', '2024-01-04', '2024-01-04'],
        'normal_net': [.04, 0., np.nan, .05, -.01, .03],
        'stress_net': [.02, 0., np.nan, .02, -.02, .01],
        'corporate_actions': [0, 0, np.nan, 1, 0, 0],
        'selected': [False, False, False, False, True, False],
    })


def test_disjoint_attribution_keeps_unknown_and_nonexecution_in_denominator():
    result = diagnose_panel(panel(), cutoff=CUTOFF)
    assert result['opportunities'] == 6
    assert {k: v['normal']['rows'] for k, v in result['gates'].items()} == {
        'not_forecasted': 1, 'normal_nonpositive': 3, 'stress_only_rejection': 1,
        'positive_gate_not_selected': 0, 'selected': 1}
    normal = result['summary']['normal']
    assert (normal['known'], normal['unknown'], normal['blocked_entries']) == (5, 1, 1)
    assert normal['all_row_mean_net'] is None
    assert normal['mean_known_net'] == pytest.approx(.11 / 5)
    assert normal['positive_fraction_known'] == pytest.approx(3 / 5)
    assert normal['positive_fraction_identification_bounds'] == pytest.approx([3 / 6, 4 / 6])
    assert sum(s['normal']['rows'] for s in result['corporate_action_strata'].values()) == 6
    assert result['corporate_action_strata']['unknown_crossings']['normal']['unknown'] == 1
    assert not result['parent_experiment_reverified'] and not result['profitability_proven']


def test_positive_gate_nonselection_is_not_claimed_to_be_rank_cap():
    rows = panel()
    rows.loc[4, 'selected'] = False
    result = diagnose_panel(rows, cutoff=CUTOFF)
    assert result['gates']['positive_gate_not_selected']['normal']['rows'] == 1
    assert any('rank cap' in message for message in result['limitations'])


def test_zero_is_known_not_a_win_and_empty_bucket_mean_is_unknown():
    result = diagnose_panel(panel(), cutoff=CUTOFF)
    normal = result['summary']['normal']
    assert (normal['known_positive'], normal['known_zero'], normal['known_negative']) == (3, 1, 1)
    assert result['gates']['positive_gate_not_selected']['normal']['all_row_mean_net'] is None


def test_negative_forecasts_can_have_perfect_order_without_positive_expectancy():
    rows = panel().iloc[:3].copy()
    rows['state'], rows['resolved_at'], rows['corporate_actions'] = 'complete', '2024-01-04', 0
    rows['normal_net'], rows['stress_net'] = [-.03, -.02, -.01], [-.04, -.03, -.02]
    result = diagnose_panel(rows, cutoff=CUTOFF)
    ranking = result['folds'][0]['ranking']['normal']
    assert ranking['mean_daily_spearman_on_resolved'] == pytest.approx(1.)
    assert result['summary']['normal']['all_row_mean_net'] < 0
    assert result['summary']['normal']['selected'] == 0


def test_rank_is_computed_before_unknown_outcomes_are_removed():
    result = diagnose_panel(panel(), cutoff=CUTOFF)
    ranking = result['folds'][0]['ranking']['normal']
    assert ranking['sessions_with_unknown_outcomes'] == 1
    assert ranking['complete_outcome_defined_ic_sessions'] == 0
    assert ranking['daily'][0]['unknown'] == 1
    # Third of five numeric forecasts belongs to decile 6 despite its unknown outcome.
    assert ranking['deciles'][5]['rows'] == ranking['deciles'][5]['unknown'] == 1


def test_ties_are_not_broken_with_outcomes_or_alphabetical_names():
    rows = panel()
    rows.loc[:4, ['normal_expected_net', 'stress_expected_net']] = [-.01, -.02]
    rows['selected'] = False
    first = diagnose_panel(rows, cutoff=CUTOFF)
    second = diagnose_panel(rows.iloc[::-1], cutoff=CUTOFF)
    assert first == second
    rank = first['folds'][0]['ranking']['normal']
    assert rank['mean_daily_spearman_on_resolved'] is None
    assert sum(bucket['rows'] > 0 for bucket in rank['deciles']) == 1


def test_equal_date_bias_does_not_overweight_large_cross_section():
    rows = panel().iloc[[0, 3]].copy()
    rows.loc[:, 'normal_net'] = [.01, .01]
    rows.loc[:, 'stress_net'] = [0., 0.]
    rows.loc[:, 'normal_expected_net'] = [0., .01]
    rows.loc[:, 'stress_expected_net'] = [-.01, 0.]
    rows.loc[rows.index[1], 'date'] = '2024-01-02'
    original = diagnose_panel(rows, cutoff=CUTOFF)['summary']['normal']
    extra = []
    for i in range(10):
        duplicate = rows.iloc[[0]].copy()
        duplicate['symbol'] = 'TEST-DUP-' + str(i)
        extra.append(duplicate)
    enlarged = diagnose_panel(pd.concat([rows, *extra], ignore_index=True), cutoff=CUTOFF)['summary']['normal']
    assert enlarged['equal_date_bias_observed_minus_prediction'] == original['equal_date_bias_observed_minus_prediction']
    assert enlarged['paired_bias_observed_minus_prediction'] != original['paired_bias_observed_minus_prediction']


@pytest.mark.parametrize('column,value', [
    ('normal_net', True), ('normal_net', '0.01'), ('normal_net', np.inf), ('normal_net', -1.01),
    ('stress_net', .5), ('state', 'success'), ('status', 'accepted'), ('selected', 1),
    ('selected', True), ('fold', True), ('fold', -.1), ('symbol', ''), ('symbol', 'ك'),
    ('corporate_actions', .5), ('corporate_actions', -1), ('corporate_actions', None),
    ('resolved_at', None), ('resolved_at', '2023-12-31'),
])
def test_invalid_values_fail_closed(column, value):
    rows = panel()
    rows[column] = rows[column].astype(object)
    rows.loc[0, column] = value
    with pytest.raises(ValueError):
        checked_panel(rows, cutoff=CUTOFF)


@pytest.mark.parametrize('change', ['missing_pred', 'numeric_unknown', 'known_unknown_date', 'blocked_gain',
                                    'duplicate', 'date_in_two_folds', 'extra_column', 'bad_missed_flag'])
def test_state_denominator_and_identity_conflicts_fail_closed(change):
    rows = panel()
    if change == 'missing_pred':
        rows.loc[0, 'normal_expected_net'] = np.nan
    elif change == 'numeric_unknown':
        rows.loc[2, ['normal_net', 'stress_net']] = 0.
    elif change == 'known_unknown_date':
        rows.loc[2, 'resolved_at'] = '2024-01-04'
    elif change == 'blocked_gain':
        rows.loc[1, 'normal_net'] = .1
    elif change == 'duplicate':
        rows = pd.concat([rows, rows.iloc[[0]]], ignore_index=True)
    elif change == 'date_in_two_folds':
        rows.loc[0, 'fold'] = 1
    elif change == 'extra_column':
        rows['unknown_contract'] = 'ignored?'
    else:
        rows['normal_missed_positive'] = False
    with pytest.raises(ValueError):
        checked_panel(rows, cutoff=CUTOFF)


def test_saved_missed_flags_are_verified_when_present():
    rows = panel()
    for scenario in ('normal', 'stress'):
        for threshold, name in [(0., 'positive'), (.03, 'above_3pct'), (.1, 'above_10pct')]:
            rows[f'{scenario}_missed_{name}'] = (~rows.selected & rows[scenario + '_net'].gt(threshold)).astype(
                'boolean').where(rows.state.ne('unknown'), pd.NA)
    assert diagnose_panel(rows, cutoff=CUTOFF)['opportunities'] == 6


def test_saved_nullable_missed_flags_preserve_unknown_outcomes(tmp_path):
    rows = panel()
    rows['normal_missed_positive'] = (~rows.selected & rows.normal_net.gt(0)).astype(
        'boolean').where(rows.state.ne('unknown'), pd.NA)
    path, digest = write_panel(tmp_path, rows)
    result = diagnose_panel(read_panel(path, digest, cutoff=CUTOFF), cutoff=CUTOFF)
    assert result['summary']['normal']['unknown'] == 1
    assert result['gates']['normal_nonpositive']['normal']['unknown'] == 1


@pytest.mark.parametrize('row,value', [(2, False), (0, pd.NA)])
def test_missed_flag_knownness_must_match_the_outcome(row, value):
    rows = panel()
    rows['normal_missed_positive'] = (~rows.selected & rows.normal_net.gt(0)).astype(
        'boolean').where(rows.state.ne('unknown'), pd.NA)
    rows.loc[row, 'normal_missed_positive'] = value
    with pytest.raises(ValueError, match='پرچم'):
        checked_panel(rows, cutoff=CUTOFF)


def write_panel(tmp_path, rows):
    path = tmp_path / 'panel.parquet'
    rows.to_parquet(path, index=False)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('column', ['date', 'resolved_at'])
def test_sealed_dates_reject_before_numeric_values_are_read(tmp_path, monkeypatch, column):
    rows = panel()
    rows.loc[0, column] = CUTOFF
    path, digest = write_panel(tmp_path, rows)
    original = pd.read_parquet
    reads = []
    def guarded_read(*args, **kwargs):
        reads.append(kwargs.get('columns'))
        assert kwargs.get('columns') == ['date', 'resolved_at']
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, 'read_parquet', guarded_read)
    with pytest.raises(ValueError, match='مهروموم'):
        read_panel(path, digest, cutoff=CUTOFF)
    assert reads == [['date', 'resolved_at']]


def test_wrong_hash_rejects_before_parquet_read(tmp_path, monkeypatch):
    path, _ = write_panel(tmp_path, panel())
    monkeypatch.setattr(pd, 'read_parquet', lambda *a, **k: pytest.fail('must reject bytes first'))
    with pytest.raises(ValueError, match='SHA256'):
        read_panel(path, '0' * 64, cutoff=CUTOFF)


def test_cli_writes_new_private_report_without_database_or_source_changes(tmp_path, monkeypatch, capsys):
    path, digest = write_panel(tmp_path, panel())
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'must-not-exist.db')
    for _ in range(2):
        assert main(['--panel', str(path), '--sha256', digest]) == 0
    reports = list((settings.DATA_DIR / 'private/hunter-attribution').glob('*/diagnostics.json'))
    assert len(reports) == 2
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not settings.DB_PATH.exists()
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['input_sha256'] == digest
    assert not report['new_model_fitted'] and not report['accepted_for_signals']
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_cutoff_cannot_be_advanced_into_holdout():
    with pytest.raises(ValueError, match='مهروموم'):
        diagnose_panel(panel(), cutoff='2025-03-22')
