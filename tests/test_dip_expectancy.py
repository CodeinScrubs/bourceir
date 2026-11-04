"""The new model's scenario floor cannot masquerade as actual stress or calibrated odds."""

import copy

import numpy as np
import pytest

from research.dip_expectancy import fit_dip_model, forecast_dip, validate_dip_model
from research.dip_outcomes import execution_contract
from research.expectancy import fit_expectancy, forecast_expectancy
from tests.test_expectancy import sample


def inputs():
    x,y=sample()
    y=y.rename(columns={'stress_net':'floor_net'})
    y.attrs.update(execution_contract())
    return x,y


def fitted():
    x,y=inputs()
    return x,y,fit_dip_model(x,y,train_end_exclusive='2023-05-03')


def test_fixed_model_has_explicit_targets_and_no_win_probability():
    x,y,model=fitted()
    predicted=forecast_dip(x.iloc[:3].assign(date='2023-05-04'),model)
    assert model['targets']==['normal_net','floor_net'] and model['probability_of_profit'] is None
    assert model['execution_contract']['entry_day_exit'] is False
    assert 'stress_expected_net' not in predicted and 'scenario_floor_expected_net' in predicted
    assert predicted.status.eq('forecasted').all()
    assert predicted.scenario_floor_expected_net.le(predicted.normal_expected_net).all()
    assert model['accepted_for_signals'] is False


@pytest.mark.parametrize('mutation',['version','rule','same_day','targets','target_policy','feature_order','coefficients'])
def test_incompatible_models_are_rejected_before_inference(mutation):
    x,y,model=fitted()
    model=copy.deepcopy(model)
    if mutation=='version':
        model['version']='conditional-expectancy-v1'
    elif mutation=='rule':
        model['execution_contract']['rule']['dip']=.03
    elif mutation=='same_day':
        model['execution_contract']['entry_day_exit']=True
    elif mutation=='targets':
        model['targets']=['normal_net','stress_net']
    elif mutation=='target_policy':
        model['execution_contract']['target_policy']='actual_stress'
    elif mutation=='feature_order':
        model['predictors']=model['predictors'][::-1]
    else:
        model['coefficients'][0][1]=float('nan')
    with pytest.raises(ValueError):
        validate_dip_model(model)
    with pytest.raises(ValueError):
        forecast_dip(x.iloc[:1].assign(date='2023-05-04'),model)


def test_unknown_pairs_stay_in_training_coverage_and_known_no_fills_can_be_zero():
    x,y=inputs()
    y.loc[0,['state','resolved_at','normal_net','floor_net']]=['unknown',None,np.nan,np.nan]
    y.loc[1,['state','normal_net','floor_net']]=['entry_blocked',0.,0.]
    model=fit_dip_model(x,y,train_end_exclusive='2023-05-03')
    assert model['unresolved_outcomes']==1 and model['fitted_rows']==len(x)-1
    assert model['training_rows']==len(x)


def test_resolution_at_or_after_training_cutoff_is_not_consumed():
    x,y=inputs()
    y.loc[0,'resolved_at']='2023-05-03'
    with pytest.raises(ValueError):
        fit_dip_model(x,y,train_end_exclusive='2023-05-03')


@pytest.mark.parametrize('key,value',[('rule',{}),('entry_day_exit',True),('fees',{}),('participation',.10),('version',None)])
def test_outcomes_from_other_execution_rules_cannot_be_silently_relabelled(key,value):
    x,y=inputs()
    y.attrs[key]=value
    with pytest.raises(ValueError,match='provenance'):
        fit_dip_model(x,y,train_end_exclusive='2023-05-03')


def test_wrong_future_features_unknown_inputs_and_unfitted_history_fail_closed():
    x,y,model=fitted()
    test=x.iloc[:2].assign(date='2023-05-04').copy()
    with pytest.raises(ValueError):
        forecast_dip(test.assign(normal_net=999),model)
    test.loc[test.index[0],'ret_5d']=np.nan
    result=forecast_dip(test,model)
    assert result.status.iloc[0]=='unknown_features' and result.scenario_floor_expected_net.isna().iloc[0]
    tiny=fit_dip_model(x.iloc[:2],y.iloc[:2],train_end_exclusive='2023-05-03')
    assert tiny['status']=='insufficient_history'
    assert forecast_dip(test,tiny).scenario_floor_expected_net.isna().all()


def test_vwap_models_and_dip_models_are_not_silently_interchangeable():
    x,y,model=fitted()
    test=x.iloc[:2].assign(date='2023-05-04')
    with pytest.raises(ValueError):
        forecast_expectancy(test,model)
    vwap=fit_expectancy(x,y.rename(columns={'floor_net':'stress_net'}),train_end_exclusive='2023-05-03')
    with pytest.raises(ValueError):
        forecast_dip(test,vwap)
