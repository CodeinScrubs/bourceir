"""Synthetic mechanisms and protocol guards; not evidence of market performance."""

import json
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from research.context_expectancy import (
    CONTEXT_PREDICTORS,
    context_matrix,
    recipe,
    validate_context_model,
)
from research.expectancy import (
    CONTEXT_VERSION,
    PREDICTORS,
    fit_expectancy,
    forecast_errors,
    forecast_expectancy,
)


def mechanism():
    rng = np.random.default_rng(138)
    features = pd.DataFrame(rng.uniform(-.1, .1, (600, len(PREDICTORS))), columns=PREDICTORS)
    features['date'] = np.repeat(pd.date_range('2024-01-01', periods=200).strftime('%Y-%m-%d'), 3)
    features['symbol'] = ['الف', 'ب', 'ج']*200
    features['rel_ret_5d'] = rng.uniform(-.2, .2, 600)
    features['buyer_power_5d'] = rng.uniform(.1, 1.9, 600)
    features['mkt_breadth_5d'] = rng.uniform(0, 1, 600)
    features['value_ratio_20d'] = rng.uniform(.2, 1.8, 600)
    outcomes = features[['date', 'symbol']].copy()
    outcomes['normal_net'] = 1.5*(features.buyer_power_5d-1)*features.rel_ret_5d-.04
    outcomes['stress_net'] = outcomes.normal_net-.008
    outcomes['state'], outcomes['resolved_at'] = 'complete', '2024-07-20'
    return features, outcomes


def test_exact_four_interactions_are_computed_from_the_same_base_rows():
    features, _ = mechanism()
    matrix = context_matrix(features, '2024-07-21', training=True)
    expected = np.column_stack([
        features.rel_ret_5d*(features.mkt_breadth_5d-.5),
        (features.buyer_power_5d-1)*features.rel_ret_5d,
        features.real_flow_ratio_5d*(features.mkt_breadth_5d-.5),
        (features.value_ratio_20d-1)*features.rel_ret_5d])
    np.testing.assert_array_equal(matrix[:, 10:], expected)
    assert matrix.shape == (600, 14)
    assert CONTEXT_PREDICTORS[:10] == PREDICTORS


def test_context_recovers_a_cross_term_without_changing_original_family():
    features, outcomes = mechanism()
    context = fit_expectancy(features, outcomes, train_end_exclusive='2024-07-21', model_family='context-v1')
    linear = fit_expectancy(features, outcomes, train_end_exclusive='2024-07-21')
    assert context['version'] == CONTEXT_VERSION and context['predictors'] == list(CONTEXT_PREDICTORS)
    assert context['interaction_recipe'] == recipe()
    assert linear['predictors'] == list(PREDICTORS) and 'interaction_recipe' not in linear
    current = features.copy()
    current['date'] = np.repeat(pd.date_range('2024-07-22', periods=200).strftime('%Y-%m-%d'), 3)
    after = forecast_expectancy(current, json.loads(json.dumps(context)))
    baseline = forecast_expectancy(current, linear)
    assert after.status.eq('forecasted').all() and baseline.status.eq('forecasted').all()
    context_error = np.mean((after.normal_expected_net-outcomes.normal_net)**2)
    baseline_error = np.mean((baseline.normal_expected_net-outcomes.normal_net)**2)
    assert context_error < baseline_error*.05
    assert after.normal_expected_net.gt(0).any() and after.stress_expected_net.gt(0).any()
    assert context['probability_of_profit'] is None and context['accepted_for_signals'] is False
    observed = outcomes.copy()
    observed['date'] = current.date
    observed['resolved_at'] = (pd.to_datetime(current.date)+pd.Timedelta(days=2)).dt.strftime('%Y-%m-%d')
    errors = forecast_errors(after, observed, context)
    assert errors['scenarios']['normal']['resolved_forecasts'] == 600


@pytest.mark.parametrize('field', ['adj_close', 'net_ret_2d', 'entry_date'])
def test_context_rejects_future_or_adjusted_inputs(field):
    features, _ = mechanism()
    features[field] = 1.
    with pytest.raises(ValueError, match='آینده'):
        context_matrix(features, '2024-07-21', training=True)


def test_precomputed_interactions_cannot_override_the_registered_recipe():
    features, _ = mechanism()
    features[CONTEXT_PREDICTORS[-1]] = 99.
    with pytest.raises(ValueError, match='تعامل آماده'):
        context_matrix(features, '2024-07-21', training=True)


@pytest.mark.parametrize('change', ['recipe', 'order', 'coefficients', 'version'])
def test_context_rejects_tampered_model_contract(change):
    features, outcomes = mechanism()
    model = fit_expectancy(features, outcomes, train_end_exclusive='2024-07-21', model_family='context-v1')
    if change == 'recipe':
        model['interaction_recipe'][0]['formula'] = 'net_ret_2d'
    elif change == 'order':
        model['predictors'].reverse()
    elif change == 'coefficients':
        model['coefficients'] = model['coefficients'][:-1]
    else:
        model['version'] = 'conditional-expectancy-v1'
    with pytest.raises(ValueError):
        validate_context_model(model)


def test_unknown_and_overflow_interactions_never_become_numeric_forecasts():
    features, outcomes = mechanism()
    model = fit_expectancy(features, outcomes, train_end_exclusive='2024-07-21', model_family='context-v1')
    current = features.iloc[:2].copy()
    current['date'] = '2024-07-22'
    current.loc[current.index[0], 'real_flow_ratio_5d'] = np.nan
    current.loc[current.index[1], ['rel_ret_5d', 'buyer_power_5d']] = [1e200, 1e200]
    predicted = forecast_expectancy(current, model)
    assert predicted.status.eq('unknown_features').all()
    assert predicted[['normal_expected_net', 'stress_expected_net']].isna().all().all()


def test_insufficient_context_model_keeps_recipe_and_unavailable_state():
    features, outcomes = mechanism()
    model = fit_expectancy(features.iloc[:3], outcomes.iloc[:3], train_end_exclusive='2024-07-21',
                           model_family='context-v1')
    current = features.iloc[:1].copy().assign(date='2024-07-22')
    assert model['status'] == 'insufficient_history' and model['interaction_recipe'] == recipe()
    assert forecast_expectancy(current, model).status.tolist() == ['model_unavailable']
    wrong = deepcopy(model)
    wrong['interaction_recipe'] = []
    with pytest.raises(ValueError):
        forecast_expectancy(current, wrong)


def test_unknown_family_is_not_silently_downgraded():
    features, outcomes = mechanism()
    with pytest.raises(ValueError, match='خانواده'):
        fit_expectancy(features, outcomes, train_end_exclusive='2024-07-21', model_family='later')
