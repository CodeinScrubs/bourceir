"""Four fixed PIT interactions, one Ridge penalty; an unaccepted research family."""

import numpy as np

from research.expectancy import (
    CONTEXT_VERSION,
    MIN_ROWS,
    MIN_SESSIONS,
    PREDICTORS,
    _features,
    _fit_matrix,
    _forecast_matrix,
    _validate_model,
)

RECIPE = (
    ('context_relative_breadth', 'rel_ret_5d*(mkt_breadth_5d-0.5)'),
    ('context_buyer_relative', '(buyer_power_5d-1.0)*rel_ret_5d'),
    ('context_flow_breadth', 'real_flow_ratio_5d*(mkt_breadth_5d-0.5)'),
    ('context_value_relative', '(value_ratio_20d-1.0)*rel_ret_5d'),
)
CONTEXT_PREDICTORS = (*PREDICTORS, *(name for name, _ in RECIPE))


def recipe():
    return [dict(name=name, formula=formula) for name, formula in RECIPE]


def context_matrix(features, boundary, *, training):
    """Derive from the ten source features; caller-supplied interactions are forbidden."""
    base = _features(features, boundary, training=training)
    if any(name in features for name, _ in RECIPE):
        raise ValueError('تعامل آماده پذیرفته نیست؛ تعامل باید از ورودی پایه بازسازی شود')
    relative = base[:, PREDICTORS.index('rel_ret_5d')]
    breadth = base[:, PREDICTORS.index('mkt_breadth_5d')] - .5
    buyer = base[:, PREDICTORS.index('buyer_power_5d')] - 1.
    flow = base[:, PREDICTORS.index('real_flow_ratio_5d')]
    value = base[:, PREDICTORS.index('value_ratio_20d')] - 1.
    with np.errstate(over='ignore', invalid='ignore'):
        interactions = np.column_stack([relative*breadth, buyer*relative, flow*breadth, value*relative])
    interactions[~np.isfinite(interactions)] = np.nan
    return np.column_stack([base, interactions])


def validate_context_model(model):
    _validate_model(model, version=CONTEXT_VERSION, predictors=CONTEXT_PREDICTORS)
    if model.get('interaction_recipe') != recipe():
        raise ValueError('دستور تعامل مدل با خانواده ثبت‌شده برابر نیست')


def fit_context_expectancy(features, outcomes, *, train_end_exclusive,
                           min_sessions=MIN_SESSIONS, min_rows=MIN_ROWS):
    if type(min_sessions) is not int or min_sessions < 3 or type(min_rows) is not int or min_rows < 3:
        raise ValueError('کف محاسباتی باید صحیح و حداقل سه باشد')
    matrix = context_matrix(features, train_end_exclusive, training=True)
    model = _fit_matrix(features, outcomes, matrix, train_end_exclusive=train_end_exclusive,
        min_sessions=min_sessions, min_rows=min_rows, version=CONTEXT_VERSION, predictors=CONTEXT_PREDICTORS)
    model['interaction_recipe'] = recipe()
    validate_context_model(model)
    return model


def forecast_context_expectancy(features, model):
    validate_context_model(model)
    matrix = context_matrix(features, model['train_end_exclusive'], training=False)
    return _forecast_matrix(features, model, matrix)
