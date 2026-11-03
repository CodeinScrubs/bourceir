"""Regression fixtures remain synthetic and confined to tests."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from research.expectancy import fit_expectancy, forecast_errors, forecast_expectancy
from tests.test_expectancy import sample


def fitted():
    features, outcomes = sample()
    model = fit_expectancy(features, outcomes, train_end_exclusive='2023-05-03')
    current = features.iloc[:3].copy()
    current['date'] = '2023-05-04'
    result = outcomes.iloc[:3].copy()
    result['date'], result['resolved_at'] = '2023-05-04', '2023-05-06'
    return model, current, result


@pytest.mark.parametrize('field,value', [
    ('version', 'another-model'), ('status', 'wrong'), ('penalty', True),
    ('accepted_for_signals', True), ('probability_of_profit', .9),
    ('conditions_on_resolved_training', False), ('fitted_rows', 100000),
    ('fitted_sessions', True), ('min_rows', 0), ('max_resolved_at', '2023-05-03'),
])
def test_forecasting_rejects_incompatible_model_contract(field, value):
    model, current, _ = fitted()
    model[field] = value
    with pytest.raises(ValueError):
        forecast_expectancy(current, model)


def test_feature_order_is_part_of_the_model_contract():
    model, current, _ = fitted()
    model['predictors'].reverse()
    with pytest.raises(ValueError, match='ترتیب'):
        forecast_expectancy(current, model)


@pytest.mark.parametrize('field', ['center', 'scale', 'minimum', 'maximum', 'unconditioned_mean'])
@pytest.mark.parametrize('bad', [float('inf'), '0.1', True])
def test_model_arrays_do_not_coerce_nonfinite_text_or_boolean(field, bad):
    model, current, _ = fitted()
    model[field][0] = bad
    with pytest.raises(ValueError):
        forecast_expectancy(current, model)


def test_coefficients_shape_and_boolean_mask_fail_closed():
    model, current, _ = fitted()
    wrong = deepcopy(model)
    wrong['coefficients'] = wrong['coefficients'][:-1]
    with pytest.raises(ValueError):
        forecast_expectancy(current, wrong)
    model['active_columns'][0] = 1
    with pytest.raises(ValueError):
        forecast_expectancy(current, model)


def test_unavailable_model_cannot_carry_fitted_state():
    features, outcomes = sample()
    model = fit_expectancy(features.iloc[:3], outcomes.iloc[:3], train_end_exclusive='2023-05-03')
    model['center'] = [0.] * 10
    _, current, _ = fitted()
    with pytest.raises(ValueError):
        forecast_expectancy(current, model)


def test_model_schema_missing_field_is_a_clear_value_error():
    model, current, _ = fitted()
    del model['predictors']
    with pytest.raises(ValueError):
        forecast_expectancy(current, model)


def test_duplicate_feature_column_is_not_used_positionally():
    model, current, _ = fitted()
    duplicate = pd.concat([current, current[['ret_1d']]], axis=1)
    with pytest.raises(ValueError):
        forecast_expectancy(duplicate, model)


def test_extreme_finite_training_scale_is_rejected_without_a_fitted_nan_model():
    features, outcomes = sample()
    features['ret_1d'] = np.tile([-1e300, 1e300, 0.], len(features)//3)
    with pytest.raises(ValueError, match='مقیاس'):
        fit_expectancy(features, outcomes, train_end_exclusive='2023-05-03')


def test_error_report_requires_the_exact_outcome_denominator():
    model, current, outcomes = fitted()
    prediction = forecast_expectancy(current, model)
    with pytest.raises(ValueError, match='مخرج'):
        forecast_errors(prediction, outcomes.iloc[:2], model)
    extra = outcomes.iloc[:1].assign(symbol='نماداضافی')
    with pytest.raises(ValueError, match='مخرج'):
        forecast_errors(prediction, pd.concat([outcomes, extra]), model)


def test_training_unknown_cannot_carry_a_resolution_timestamp():
    features, outcomes = sample()
    outcomes.loc[0, ['normal_net', 'stress_net']] = np.nan
    outcomes.loc[0, 'state'] = 'unknown'
    with pytest.raises(ValueError, match='نامعلومی'):
        fit_expectancy(features, outcomes, train_end_exclusive='2023-05-03')


def test_missing_training_outcome_columns_give_a_contract_error():
    features, outcomes = sample()
    with pytest.raises(ValueError, match='ستون'):
        fit_expectancy(features, outcomes.drop(columns='normal_net'), train_end_exclusive='2023-05-03')


def test_explicit_unknown_remains_unknown_and_counts_toward_coverage():
    model, current, outcomes = fitted()
    prediction = forecast_expectancy(current, model)
    outcomes.loc[0, ['normal_net', 'stress_net']] = np.nan
    outcomes.loc[0, 'state'], outcomes.loc[0, 'resolved_at'] = 'unknown', None
    scores = forecast_errors(prediction, outcomes, model)
    assert scores['opportunities'] == 3
    assert scores['scenarios']['normal']['unknown_forecast_outcomes'] == 1
    assert scores['scenarios']['normal']['resolved_forecasts'] == 2


@pytest.mark.parametrize('field,bad', [
    ('state', 'unknown'), ('normal_net', True), ('stress_net', 100.),
    ('resolved_at', '2023-05-04'),
])
def test_error_report_rejects_incoherent_outcomes(field, bad):
    model, current, outcomes = fitted()
    prediction = forecast_expectancy(current, model)
    outcomes[field] = outcomes[field].astype(object)
    outcomes.loc[0, field] = bad
    with pytest.raises(ValueError):
        forecast_errors(prediction, outcomes, model)


def test_error_report_rejects_numeric_prediction_with_unavailable_status():
    model, current, outcomes = fitted()
    prediction = forecast_expectancy(current, model)
    prediction.loc[0, 'status'] = 'model_unavailable'
    with pytest.raises(ValueError):
        forecast_errors(prediction, outcomes, model)


def test_valid_model_round_trip_preserves_the_original_predictions():
    import json

    model, current, _ = fitted()
    expected = forecast_expectancy(current, model)
    actual = forecast_expectancy(current, json.loads(json.dumps(model, allow_nan=False)))
    pd.testing.assert_frame_equal(expected, actual)
