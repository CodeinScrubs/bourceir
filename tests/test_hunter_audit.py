"""Synthetic fixtures only: exhaustive unknown-result bounds and archive guards."""

from itertools import product

import numpy as np
import pandas as pd
import pytest

from config import settings
from research.hunter_audit import opportunity_audit, summarize_opportunities
from research.hunter_audit_run import main, run_hunter_audit
from tests.test_prediction_contracts import fitted


def audit_inputs():
    model, current, outcomes = fitted()
    forecast = current[['date', 'symbol']].assign(normal_expected_net=.04, stress_expected_net=.02, status='forecasted')
    outcomes['normal_net'], outcomes['stress_net'] = [.10, -.05, np.nan], [.07, -.08, np.nan]
    outcomes['corporate_actions'] = [2., 0., np.nan]
    outcomes.loc[2, 'state'], outcomes.loc[2, 'resolved_at'] = 'unknown', None
    picks = forecast.iloc[[0, 2]][['date', 'symbol']].assign(rank=[0, 1])
    return forecast, outcomes, picks, model


def audit(*args):
    return opportunity_audit(*args, holdout_start='2025-03-21')


def test_unknown_denominator_and_corporate_action_exposure_are_visible():
    report = audit(*audit_inputs())
    total = report['total']
    assert total['opportunities'] == 3 and total['selected'] == 2
    assert total['unknown_outcomes'] == total['selected_unknown_outcomes'] == 1
    assert total['known_corporate_action_crossings'] == 2
    assert total['corporate_action_opportunities'] == 1
    assert total['unknown_corporate_action_counts'] == 1
    assert total['scenarios']['normal']['precision_bounds'] == [.5, 1.]
    assert total['scenarios']['normal']['recall_bounds'] == [1., 1.]
    assert not report['accepted_for_signals'] and not report['holdout_eligible']


def test_material_gain_targets_are_fixed_strict_and_separate_for_stress():
    forecast, outcomes, picks, model = audit_inputs()
    outcomes.loc[0, 'normal_net'], outcomes.loc[0, 'stress_net'] = .11, .03
    scores = audit(forecast, outcomes, picks, model)['total']['scenarios']
    assert [s['net_return_strictly_above'] for s in scores['normal']['gain_thresholds']] == [.03, .10]
    assert [s['selected_known_positive'] for s in scores['normal']['gain_thresholds']] == [1, 1]
    assert [s['selected_known_positive'] for s in scores['stress']['gain_thresholds']] == [0, 0]
    assert all(s['precision_bounds'] == [.5, 1.] for s in scores['normal']['gain_thresholds'])


def test_recall_bounds_exhaust_every_possible_unknown_assignment():
    for states in product((-1, 0, 1), repeat=3):
        for mask in product((False, True), repeat=3):
            rows = pd.DataFrame(dict(state=['unknown' if s == 0 else 'complete' for s in states],
                normal_net=[np.nan if s == 0 else s/10 for s in states],
                stress_net=[np.nan if s == 0 else s/10 for s in states], selected=mask,
                status=['forecasted']*3, corporate_actions=[np.nan if s == 0 else 0. for s in states]))
            actual = summarize_opportunities(rows)['scenarios']['normal']
            recalls, precisions = [], []
            for assignment in product((False, True), repeat=states.count(0)):
                choices = iter(assignment)
                positive = [next(choices) if s == 0 else s == 1 for s in states]
                hits = sum(p and chosen for p, chosen in zip(positive, mask, strict=True))
                if any(positive):
                    recalls.append(hits/sum(positive))
                if any(mask):
                    precisions.append(hits/sum(mask))
            for key, values in [('recall_bounds', recalls), ('precision_bounds', precisions)]:
                expected = [min(values), max(values)] if values else [None, None]
                assert actual[key] == expected, (states, mask, key)


def test_missed_winners_are_counted_even_when_forecasting_abstains():
    forecast, outcomes, picks, model = audit_inputs()
    forecast.loc[0, ['normal_expected_net', 'stress_expected_net']] = np.nan
    forecast.loc[0, 'status'] = 'outside_training_support'
    report = audit(forecast, outcomes, picks.iloc[1:].assign(rank=0), model)
    score = report['total']['scenarios']['normal']
    assert score['missed_known_positive'] == 1
    assert score['missed_positive_by_forecast_status'] == {'outside_training_support': 1}
    assert score['recall_bounds'] == [0., .5]


def test_empty_picks_are_not_missing_outcomes_and_zero_positives_have_no_recall():
    forecast, outcomes, picks, model = audit_inputs()
    picks = picks.iloc[:0]
    report = audit(forecast, outcomes, picks, model)
    assert report['total']['scenarios']['normal']['precision_bounds'] == [None, None]
    assert report['total']['scenarios']['normal']['recall_bounds'] == [0., 0.]
    outcomes['normal_net'], outcomes['stress_net'] = 0., 0.
    outcomes['state'], outcomes['resolved_at'], outcomes['corporate_actions'] = 'entry_blocked', '2023-05-05', 0.
    report = audit(forecast, outcomes, picks, model)
    assert report['total']['scenarios']['normal']['recall_bounds'] == [None, None]
    assert report['total']['entry_blocked'] == 3


def test_missing_outcome_and_unlisted_pick_fail_instead_of_shrinking_denominator():
    forecast, outcomes, picks, model = audit_inputs()
    with pytest.raises(ValueError, match='مخرج'):
        audit(forecast, outcomes.iloc[:2], picks, model)
    picks.loc[0, 'symbol'] = 'خارجازپنل'
    with pytest.raises(ValueError, match='پنل'):
        audit(forecast, outcomes, picks, model)


@pytest.mark.parametrize('ranks', [[True, 1], [0., 1.], [0, 0], [1, 2], [0, -1]])
def test_rank_contract(ranks):
    forecast, outcomes, picks, model = audit_inputs()
    picks['rank'] = ranks
    with pytest.raises(ValueError, match='رتبه'):
        audit(forecast, outcomes, picks, model)


@pytest.mark.parametrize('change', ['duplicate', 'nonpositive', 'missing_actions', 'unknown_actions', 'negative_actions'])
def test_malformed_coverage_is_not_reported(change):
    forecast, outcomes, picks, model = audit_inputs()
    if change == 'duplicate':
        picks = pd.concat([picks, picks.iloc[:1]])
    elif change == 'nonpositive':
        forecast.loc[0, 'stress_expected_net'] = 0.
    elif change == 'missing_actions':
        outcomes = outcomes.drop(columns='corporate_actions')
    elif change == 'unknown_actions':
        outcomes.loc[2, 'corporate_actions'] = 0.
    else:
        outcomes.loc[0, 'corporate_actions'] = -1.
    with pytest.raises(ValueError):
        audit(forecast, outcomes, picks, model)


def test_development_guard_rejects_decisions_and_late_resolutions():
    forecast, outcomes, picks, model = audit_inputs()
    outcomes.loc[0, 'resolved_at'] = '2025-03-21'
    with pytest.raises(ValueError, match='مرز توسعه'):
        audit(forecast, outcomes, picks, model)
    forecast.loc[0, 'date'] = '2025-03-21'
    with pytest.raises(ValueError, match='مهروموم'):
        audit(forecast, outcomes, picks, model)
    with pytest.raises(ValueError, match='دوره توسعه'):
        opportunity_audit(*audit_inputs(), holdout_start='2099-01-01')


def test_late_resolution_guard_precedes_numerical_evaluation(monkeypatch):
    from research import hunter_audit

    forecast, outcomes, picks, model = audit_inputs()
    outcomes.loc[0, 'resolved_at'] = '2025-03-21'
    def forbidden(*args):
        raise AssertionError('numeric evaluation must not run')
    monkeypatch.setattr(hunter_audit, 'forecast_errors', forbidden)
    with pytest.raises(ValueError, match='مرز توسعه'):
        audit(forecast, outcomes, picks, model)


def test_cli_and_bad_source_never_create_a_database(tmp_path, monkeypatch):
    database = tmp_path / 'absent.db'
    monkeypatch.setattr(settings, 'DB_PATH', database)
    with pytest.raises(ValueError, match='شناسه'):
        run_hunter_audit(None, source_id='../outside')
    with pytest.raises(ValueError, match='دیتابیس موجود'):
        main(['--source', 'EXP-2024-01-01-EXPECTANCY-'+'a'*32])
    assert not database.exists()


def test_empty_audit_is_explicitly_empty():
    forecast, outcomes, picks, model = audit_inputs()
    report = audit(forecast.iloc[:0], outcomes.iloc[:0], picks.iloc[:0], model)
    assert report['daily'] == []
    assert report['total']['opportunities'] == 0
    assert report['total']['forecast_coverage'] is None
