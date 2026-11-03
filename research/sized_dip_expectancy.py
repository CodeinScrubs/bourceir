"""Two preregistered fixed-budget Ridge variants; never calibrated win probabilities."""

import numpy as np

from research.expectancy import PREDICTORS, _features, _fit_matrix, _forecast_matrix, _validate_model
from research.probability import _numeric
from research.sized_dip_outcomes import budget_value, execution_contract

VERSION = 'conditional-sized-dip-expectancy-v1'
VARIANTS = ('sized10','sized11')
CAPACITY_NAME = 'log_value_capacity_to_budget'
CAPACITY_FORMULA = 'log1p(decision_day_raw_value * participation / budget_rial)'
TARGETS = ['normal_net','floor_net']


def predictors(variant):
    if variant not in VARIANTS:
        raise ValueError('A registered sized-DIP predictor variant is required')
    return (*PREDICTORS,CAPACITY_NAME) if variant=='sized11' else PREDICTORS


def recipe(variant):
    predictors(variant)
    return [dict(name=CAPACITY_NAME,formula=CAPACITY_FORMULA)] if variant=='sized11' else []


def _matrix(frame,boundary,*,training,variant,budget_rial):
    predictors(variant)
    budget_value(budget_rial)
    allowed={'date','symbol',*PREDICTORS}|({'value'} if variant=='sized11' else set())
    if set(frame)!=allowed:
        raise ValueError('Sized features must contain exactly the registered decision-day raw inputs')
    base=_features(frame[['date','symbol',*PREDICTORS]],boundary,training=training)
    if variant=='sized10':
        return base
    raw=_numeric(frame['value']).to_numpy(dtype=float,na_value=np.nan)
    with np.errstate(over='ignore',invalid='ignore'):
        capacity=np.log1p(np.where(raw>0,raw,np.nan)*.05/budget_rial)
    capacity[~np.isfinite(capacity)]=np.nan
    return np.column_stack([base,capacity])


def validate_sized_model(model):
    if not isinstance(model,dict):
        raise ValueError('A serialized sized-DIP model mapping is required')
    variant=model.get('variant')
    budget=budget_value(model.get('budget_rial'))
    _validate_model(model,version=VERSION,predictors=predictors(variant))
    if (model.get('execution_contract')!=execution_contract(budget)
            or model.get('targets')!=TARGETS or model.get('derived_feature_recipe')!=recipe(variant)):
        raise ValueError('Sized target, budget, execution or derived-feature recipe differs')


def fit_sized_model(features,outcomes,*,train_end_exclusive,budget_rial,variant,
                    min_sessions=60,min_rows=200):
    if type(min_sessions) is not int or min_sessions<3 or type(min_rows) is not int or min_rows<3:
        raise ValueError('Explicit supported computational sample minima are required')
    contract=execution_contract(budget_rial)
    if (not {'date','symbol','state','resolved_at',*TARGETS}<=set(outcomes)
            or any(outcomes.attrs.get(k)!=v for k,v in contract.items()) or 'stress_net' in outcomes):
        raise ValueError('Targets must carry the exact fixed-budget execution provenance')
    matrix=_matrix(features,train_end_exclusive,training=True,variant=variant,budget_rial=budget_rial)
    targets=outcomes[['date','symbol','state','resolved_at',*TARGETS]].rename(columns={'floor_net':'stress_net'})
    model=_fit_matrix(features,targets,matrix,train_end_exclusive=train_end_exclusive,
        min_sessions=min_sessions,min_rows=min_rows,version=VERSION,predictors=predictors(variant))
    model.update(variant=variant,budget_rial=budget_rial,execution_contract=contract,
        targets=list(TARGETS),derived_feature_recipe=recipe(variant))
    validate_sized_model(model)
    return model


def forecast_sized(features,model):
    validate_sized_model(model)
    matrix=_matrix(features,model['train_end_exclusive'],training=False,
        variant=model['variant'],budget_rial=model['budget_rial'])
    return _forecast_matrix(features,model,matrix).rename(columns={'stress_expected_net':'scenario_floor_expected_net'})
