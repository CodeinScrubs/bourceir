"""Budget provenance, raw capacity inputs and chronology remain explicit at inference."""

import copy
import json

import numpy as np
import pandas as pd
import pytest

from research.sized_dip_expectancy import CAPACITY_NAME, fit_sized_model, forecast_sized, validate_sized_model
from research.sized_dip_outcomes import execution_contract
from tests.test_expectancy import sample

BUDGET = 2_000_000_000


def inputs(variant):
    x, y = sample()
    if variant == 'sized11':
        x['value'] = np.linspace(1e8, 3e9, len(x))
    y = y.rename(columns={'stress_net': 'floor_net'})
    y.attrs.update(execution_contract(BUDGET))
    return x, y


def fit(x, y, variant):
    return fit_sized_model(x, y, train_end_exclusive='2023-05-03', budget_rial=BUDGET, variant=variant)


@pytest.mark.parametrize('variant', ['sized10', 'sized11'])
def test_serialized_model_has_exact_budget_target_and_predictor_recipe(variant):
    x, y = inputs(variant)
    model = fit(x, y, variant)
    restored = json.loads(json.dumps(model, allow_nan=False))
    test = x.iloc[:3].assign(date='2023-05-04')
    predicted = forecast_sized(test, restored)
    assert len(model['predictors']) == (11 if variant == 'sized11' else 10)
    assert model['budget_rial'] == BUDGET and model['targets'] == ['normal_net', 'floor_net']
    assert model['execution_contract']['unspent_budget_in_denominator']
    assert not model['execution_contract']['account_loss_floor_modeled']
    assert model['probability_of_profit'] is None and not model['accepted_for_signals']
    assert predicted.status.eq('forecasted').all()
    pd.testing.assert_frame_equal(predicted, forecast_sized(test, model), check_exact=True)


def test_capacity_is_derived_only_from_current_raw_value_and_unknowns_are_not_zero():
    x, y = inputs('sized11')
    model = fit(x, y, 'sized11')
    expected = np.log1p(x.value*.05/BUDGET)
    assert model['minimum'][-1] == expected.min() and model['maximum'][-1] == expected.max()
    test = x.iloc[:4].assign(date='2023-05-04', symbol=['الف', 'ب', 'ج', 'د']).copy()
    test.loc[test.index[:3], 'value'] = [np.nan, 0., -1.]
    predicted = forecast_sized(test, model)
    assert predicted.status.tolist() == ['unknown_features']*3+['forecasted']
    assert predicted.normal_expected_net.iloc[:3].isna().all()
    with pytest.raises(ValueError):
        forecast_sized(test.assign(**{CAPACITY_NAME: 999.}), model)


@pytest.mark.parametrize('field', ['budget', 'variant', 'recipe', 'predictors', 'targets', 'coefficients', 'execution'])
def test_incompatible_serialized_models_are_rejected_before_forecasting(field):
    x, y = inputs('sized11')
    model = copy.deepcopy(fit(x, y, 'sized11'))
    if field == 'budget':
        model['budget_rial'] += 1
    elif field == 'variant':
        model['variant'] = 'sized12'
    elif field == 'recipe':
        model['derived_feature_recipe'][0]['formula'] = 'forward volume'
    elif field == 'predictors':
        model['predictors'] = model['predictors'][::-1]
    elif field == 'targets':
        model['targets'] = ['normal_net', 'stress_net']
    elif field == 'coefficients':
        model['coefficients'][0][0] = float('nan')
    else:
        model['execution_contract']['account_loss_floor_modeled'] = True
    with pytest.raises(ValueError):
        validate_sized_model(model)
    with pytest.raises(ValueError):
        forecast_sized(x.iloc[:1].assign(date='2023-05-04'), model)


@pytest.mark.parametrize('change', ['budget', 'missing_contract', 'future_result', 'future_feature', 'wrong_raw_type', 'duplicate'])
def test_training_refuses_unbound_targets_and_leaked_or_invalid_inputs(change):
    x, y = inputs('sized11')
    if change == 'budget':
        y.attrs['budget_rial'] += 1
    elif change == 'missing_contract':
        y.attrs.clear()
    elif change == 'future_result':
        y.loc[0, 'resolved_at'] = '2023-05-03'
    elif change == 'future_feature':
        x['net_ret_2d'] = 999.
    elif change == 'wrong_raw_type':
        x['value'] = x.value.astype(object)
        x.loc[0, 'value'] = True
    else:
        x = pd.concat([x, x.iloc[:1]])
    with pytest.raises(ValueError):
        fit(x, y, 'sized11')


def test_unknown_results_and_missing_capacity_remain_in_training_coverage():
    x, y = inputs('sized11')
    y.loc[0, ['state', 'resolved_at', 'normal_net', 'floor_net']] = ['unknown', None, np.nan, np.nan]
    x.loc[1, 'value'] = np.nan
    model = fit(x, y, 'sized11')
    assert model['training_rows'] == 360 and model['unresolved_outcomes'] == 1
    assert model['incomplete_feature_rows'] == 1 and model['fitted_rows'] == 358
