"""Compute admission and temporal faults must not become strategy verdicts."""

import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from research.search_budget import main as budget_main
from research.search_budget import resampling_budget
from research.strategy_suite import SuitePlan, preflight_suite, run_suite
from research.uncertainty import compare_equity_curves
from research.walkforward import TimeFold, purged_training, time_folds
from tests.test_strategy_suite import inputs
from tests.test_uncertainty import curves


@pytest.fixture
def admission_market(tmp_path, monkeypatch):
    import research.strategy_suite as suite

    raw, clients, registry, days = inputs(120, tuple(f's{i}' for i in range(20)))
    monkeypatch.setattr(suite, 'snapshot_inputs', lambda *_: (raw, clients, registry))
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    plan = SuitePlan(start=days[0], end_exclusive='2024-06-01', min_train_sessions=60, test_sessions=30)
    return raw, clients, plan


def test_preflight_never_builds_features_replays_or_run_directories(admission_market, monkeypatch):
    import research.strategy_suite as suite

    def forbidden(*args, **kwargs):
        pytest.fail('Expensive computation was started by preflight')

    monkeypatch.setattr(suite, 'build_scores', forbidden)
    monkeypatch.setattr(suite, 'replay_portfolio', forbidden)
    _, _, plan = admission_market
    report = preflight_suite(object(), plan=plan, case_names=['cash', 'buy_hold'])
    assert report['status'] == 'ready_for_exploratory_replay'
    assert report['expected_replays'] == report['case_count'] * report['fold_count'] * 2 == 8
    assert not report['expensive_computation_started'] and report['strategy_verdict'] is None
    assert not report['accepted_for_signals'] and not report['economic_readiness']
    assert not settings.DATA_DIR.exists()


@pytest.mark.parametrize('defect,reason', [('range', 'invalid_raw_observations'),
    ('count', 'invalid_raw_observations'), ('budget', 'replay_budget_exceeded'),
    ('calendar', 'observations_outside_inferred_calendar')])
def test_bad_input_or_budget_blocks_actual_run_before_feature_compute(admission_market, monkeypatch, defect, reason):
    import research.strategy_suite as suite

    raw, clients, plan = admission_market
    if defect == 'range':
        raw.loc[0, 'low_raw'] = 1000
    elif defect == 'count':
        raw['trade_count'] = raw.trade_count.astype(float)
        raw.loc[0, 'trade_count'] = 1.5
    elif defect == 'calendar':
        raw.loc[0, 'date'] = clients.loc[0, 'date'] = '2023-01-01'
    limit = 1 if defect == 'budget' else 10000
    monkeypatch.setattr(suite, 'build_scores', lambda *_: pytest.fail('Features ran before admission'))
    report = preflight_suite(object(), plan=plan, case_names=['cash', 'buy_hold'], max_replays=limit)
    assert report['status'] == 'blocked' and reason in report['blockers']
    assert report['strategy_verdict'] is None
    with pytest.raises(ValueError, match=reason):
        run_suite(object(), plan=plan, case_names=['cash', 'buy_hold'], max_replays=limit)
    assert not settings.DATA_DIR.exists()


def test_unknown_optional_client_data_is_not_a_false_strategy_failure(admission_market):
    _, clients, plan = admission_market
    clients[clients.columns.difference(['symbol', 'date'])] = np.nan
    report = preflight_suite(object(), plan=plan, case_names=['cash'])
    assert report['blockers'] == [] and report['earlier_trial_count_unknown']
    assert not report['statistical_power_established']


@pytest.mark.parametrize('limit', [True, 0, -1, 2.5])
def test_replay_budget_rejected_before_reading_snapshot(monkeypatch, limit):
    import research.strategy_suite as suite

    monkeypatch.setattr(suite, 'snapshot_inputs', lambda *_: pytest.fail('Read before budget validation'))
    with pytest.raises(ValueError, match='budget'):
        preflight_suite(object(), case_names=['cash'], max_replays=limit)


def test_million_trial_resolution_is_reported_without_spending_compute(capsys):
    assert budget_main(['--trials', '1000000', '--comparisons-per-trial', '3', '--replications', '20000']) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['minimum_replications_for_resolution'] == 59_999_999
    assert report['extra_replications_for_resolution'] == 59_979_999
    assert report['family_tail_resolution_adequate'] is False
    assert report['strategy_verdict'] is None and not report['statistical_power_established']


def test_resolution_boundary_uses_decimal_confidence_and_unknown_is_not_zero():
    exact = resampling_budget(replications=999, family_comparisons=50, confidence=.95)
    short = resampling_budget(replications=998, family_comparisons=50, confidence=.95)
    unknown = resampling_budget(replications=999, family_comparisons=None)
    assert exact['family_tail_resolution_adequate'] is True
    assert short['family_tail_resolution_adequate'] is False
    assert exact['minimum_replications_for_resolution'] == 999
    assert unknown['minimum_replications_for_resolution'] is None
    assert unknown['family_tail_resolution_adequate'] is None


def test_unresolvable_family_screen_never_calls_bootstrap_and_remains_inconclusive(monkeypatch):
    import research.uncertainty as uncertainty

    a, b = curves()
    monkeypatch.setattr(uncertainty, '_indices', lambda *_: pytest.fail('Unresolvable screening spent bootstrap compute'))
    report = compare_equity_curves(a, b, initial_capital_rial=100000., replications=1000,
        trials_attempted=1000000, family_comparisons=3000000, family_screen_only=True,
        expected_sessions=a.date.tolist())
    assert report['inference_status'] == 'inconclusive_resolution'
    assert report['intervals'] == [] and report['bootstrap_replications_performed'] == 0
    assert report['strategy_verdict'] is None and not report['accepted_for_signals']


def test_adequate_family_screen_preserves_existing_numerical_diagnostics():
    a, b = curves()
    kwargs = dict(initial_capital_rial=100000., replications=1000, trials_attempted=1,
                  family_comparisons=3, expected_sessions=a.date.tolist())
    before = compare_equity_curves(a, b, **kwargs)
    after = compare_equity_curves(a, b, **kwargs, family_screen_only=True)
    assert before['intervals'] == after['intervals']
    assert after['bootstrap_replications_performed'] == 3000
    assert after['inference_status'] == 'diagnostic_only' and after['strategy_verdict'] is None


@pytest.mark.parametrize('change', ['bad_day', 'numeric', 'nested', 'bad_cutoff', 'duplicate', 'reversed'])
def test_fold_boundaries_cannot_silently_coerce_or_sort_bad_dates(change):
    days = pd.bdate_range('2023-01-01', periods=120).strftime('%Y-%m-%d').tolist()
    cutoff = '2024-01-01'
    if change == 'bad_day':
        days[0] = '2023-02-30'
    elif change == 'numeric':
        days = list(range(120))
    elif change == 'nested':
        days = [days]
    elif change == 'bad_cutoff':
        cutoff = '2024-1-1'
    elif change == 'duplicate':
        days[1] = days[0]
    else:
        days.reverse()
    with pytest.raises(ValueError):
        time_folds(days, min_train_sessions=60, test_sessions=30, end_exclusive=cutoff)


def test_malformed_or_unrealized_label_dates_never_enter_training():
    fold = TimeFold('2023-01-01', '2023-06-01', '2023-06-05', '2023-07-01', 2)
    panel = pd.DataFrame(dict(date=['2023-02-01'] * 8,
        entry_date=['2023-02-02'] * 6 + ['0', '2023-01-01'],
        label_status_2d=['complete'] * 4 + ['entry_blocked', 'entry_blocked', 'entry_blocked', 'complete'],
        exit_date_2d=['2023-02-06', '0', '2023-02-30', '2023-06-07', '', '', '', '2023-02-06']))
    panel.loc[5, 'entry_date'] = '2023-06-02'
    original = panel.copy(deep=True)
    assert purged_training(panel, fold, 2).index.tolist() == [0, 4]
    pd.testing.assert_frame_equal(panel, original)


@pytest.mark.parametrize('dates', [('2023-06-01', '2023-01-01', '2023-06-05', '2023-07-01'),
    ('2023-01-01', '2023-06-10', '2023-06-05', '2023-07-01'),
    ('2023-01-01', '2023-06-01', '2023-06-05', '2023-6-30')])
def test_invalid_temporal_contract_rejected(dates):
    with pytest.raises(ValueError):
        TimeFold(*dates, 2)
