"""Fixed execution-aligned DIP forecast; per-attempt scenario-floor EV is not win probability."""

from research.dip_outcomes import execution_contract
from research.expectancy import PREDICTORS, _features, _fit_matrix, _forecast_matrix, _validate_model

VERSION = 'conditional-dip-expectancy-v1'
TARGETS = ['normal_net','floor_net']


def validate_dip_model(model):
    _validate_model(model,version=VERSION,predictors=PREDICTORS)
    if model.get('execution_contract')!=execution_contract() or model.get('targets')!=TARGETS:
        raise ValueError('DIP execution rule, target semantics or price contract differs')


def _matrix(features,boundary,*,training):
    if set(features)!={'date','symbol',*PREDICTORS}:
        raise ValueError('Only the fixed decision-day predictors and keys are permitted')
    return _features(features,boundary,training=training)


def fit_dip_model(features,outcomes,*,train_end_exclusive,min_sessions=60,min_rows=200):
    if type(min_sessions) is not int or min_sessions<3 or type(min_rows) is not int or min_rows<3:
        raise ValueError('Computational sample minima must be explicit positive integers')
    required = {'date','symbol','state','resolved_at',*TARGETS}
    if not required<=set(outcomes) or 'stress_net' in outcomes:
        raise ValueError('Explicit normal and scenario-floor targets are required')
    if any(key not in outcomes.attrs or outcomes.attrs[key]!=value for key,value in execution_contract().items()):
        raise ValueError('Outcome provenance does not match the fixed DIP execution family')
    x = _matrix(features,train_end_exclusive,training=True)
    # Only the shared linear solver's internal second-column name is adapted. The serialized
    # family and all public output names explicitly identify the per-attempt scenario floor.
    targets = outcomes[['date','symbol','state','resolved_at',*TARGETS]].rename(columns={'floor_net':'stress_net'})
    model = _fit_matrix(features,targets,x,train_end_exclusive=train_end_exclusive,
        min_sessions=min_sessions,min_rows=min_rows,version=VERSION,predictors=PREDICTORS)
    model.update(execution_contract=execution_contract(),targets=list(TARGETS))
    validate_dip_model(model)
    return model


def forecast_dip(features,model):
    validate_dip_model(model)
    x = _matrix(features,model['train_end_exclusive'],training=False)
    return _forecast_matrix(features,model,x).rename(columns={'stress_expected_net':'scenario_floor_expected_net'})
