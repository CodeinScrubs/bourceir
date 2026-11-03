"""مقادیر ساختگی فقط برای آزمون نگاشت کنترل‌ها و حفظ مخرج نامعلوم."""

import pytest

from research.expectancy_run import CANDIDATES, SEEDS, candidate_comparisons, expected_modes


def results():
    names = sorted(expected_modes())
    return [dict(modes={name: dict(metrics=dict(total_return=i / 1000)) for i, name in enumerate(names)}),
            dict(modes={name: dict(metrics=dict(total_return=-i / 1000)) for i, name in enumerate(names)})]


@pytest.mark.parametrize('candidate', CANDIDATES)
def test_all_frozen_controls_are_compared_in_the_matching_execution_scenario(candidate):
    comparisons = candidate_comparisons(results(), candidate)
    assert len(comparisons) == 11
    assert comparisons['stress']['left'] == candidate  # کلید قدیمی حساسیت اجرا محفوظ است.
    assert comparisons['stress']['right'] == candidate + '-stress'
    assert comparisons['stressed_past_baseline']['right'] == 'past_baseline_stress'
    assert comparisons['stressed_idle_cash']['left'] == candidate + '-stress'
    for seed in SEEDS:
        normal, stress = comparisons[f'random_{seed}'], comparisons[f'stressed_random_{seed}']
        assert (normal['left'], normal['right']) == (candidate, f'{candidate}-random{seed}')
        assert (stress['left'], stress['right']) == (candidate + '-stress', f'{candidate}-random{seed}-stress')
        assert normal['statistical_significance'] is None and not normal['comparable_risk_proven']


def test_unknown_comparison_does_not_turn_into_zero_or_drop_from_total_folds():
    rows = results()
    rows[0]['modes']['conditional-2d-random41-stress']['metrics']['total_return'] = None
    comparison = candidate_comparisons(rows, 'conditional-2d')['stressed_random_41']
    assert (comparison['total_folds'], comparison['paired_known_folds'], comparison['excluded_unknown_pairs']) == (2, 1, 1)
    assert comparison['rows'][0]['difference'] is None


def test_unknown_candidate_is_rejected():
    with pytest.raises(ValueError):
        candidate_comparisons(results(), 'not-predeclared')
