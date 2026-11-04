"""PIT labels, proxy-fill semantics and numerical validation for two-stage research."""

import copy
import json

import numpy as np
import pandas as pd
import pytest

from research.sized_dip_expectancy import _matrix
from research.sized_dip_outcomes import execution_contract
from research.two_stage_dip import (
    _fit_logit,
    _loss,
    _probability,
    fit_two_stage,
    forecast_two_stage,
    validate_two_stage_model,
)
from tests.test_expectancy import sample

BUDGET=2_000_000_000
BOUNDARY='2023-05-03'


def inputs():
    x,y=sample()
    x['value']=np.linspace(1e8,3e9,len(x))
    filled=~y.symbol.eq('ج')
    y['normal_entry_filled']=filled.astype(object)
    y['normal_entry_date']=(pd.to_datetime(y.date)+pd.Timedelta(days=1)).dt.strftime('%Y-%m-%d')
    y['resolved_at']=(pd.to_datetime(y.date)+pd.Timedelta(days=2)).dt.strftime('%Y-%m-%d')
    y=y.rename(columns={'stress_net':'floor_net'})
    y.loc[~filled,['normal_net','floor_net']]=0.
    y.loc[~filled,'state']='entry_blocked'
    y.loc[~filled,'resolved_at']=y.loc[~filled,'normal_entry_date']
    y.attrs.update(execution_contract(BUDGET))
    return x,y


def fit(x,y):
    return fit_two_stage(x,y,train_end_exclusive=BOUNDARY,budget_rial=BUDGET)


def test_model_roundtrips_and_predictions_are_proxy_fill_times_conditional_return():
    from research.expectancy import _forecast_matrix

    x,y=inputs()
    model=fit(x,y)
    restored=json.loads(json.dumps(model,allow_nan=False))
    assert model['fill_model']['status']=='fitted'
    assert model['conditional_return_model']['status']=='fitted'
    assert model['known_filled_entries']==240 and model['conditional_outcomes_known']==240
    assert not model['fill_model']['probability_calibrated'] and model['probability_of_profit'] is None
    test=x.iloc[:3].assign(date='2023-05-04')
    result=forecast_two_stage(test,restored)
    assert result.status.eq('forecasted').all()
    assert result.proxy_entry_probability.between(0,1).all()
    raw=_forecast_matrix(test,model['conditional_return_model'],
        _matrix(test,BOUNDARY,training=False,variant='sized11',budget_rial=BUDGET))
    np.testing.assert_allclose(result.normal_expected_net,raw.normal_expected_net*result.proxy_entry_probability)
    np.testing.assert_allclose(result.scenario_floor_expected_net,raw.stress_expected_net*result.proxy_entry_probability)
    pd.testing.assert_frame_equal(result,forecast_two_stage(test,model),check_exact=True)


def test_entry_can_be_known_even_when_subsequent_exit_is_unknown():
    x,y=inputs()
    y.loc[0,['state','resolved_at','normal_net','floor_net']]=['unknown',None,np.nan,np.nan]
    model=fit(x,y)
    assert model['fill_model']['known_entries']==360
    assert model['known_filled_entries']==240 and model['conditional_outcomes_known']==239
    assert model['filled_with_unresolved_return']==1
    assert not model['conditional_missingness_unbiased']


@pytest.mark.parametrize('mutation',['unknown_state','filled_entry_blocked','no_entry_complete',
    'no_entry_nonzero','no_entry_unresolved','resolved_before_entry','no_entry_boolean_return'])
def test_direct_fit_rejects_contradictory_observable_outcomes(mutation):
    x,y=inputs()
    if mutation=='unknown_state':
        y.loc[0,'state']='not_a_state'
    elif mutation=='filled_entry_blocked':
        y.loc[0,'state']='entry_blocked'
    elif mutation=='no_entry_complete':
        y.loc[2,'state']='complete'
    elif mutation=='no_entry_nonzero':
        y.loc[2,'normal_net']=.1
    elif mutation=='no_entry_unresolved':
        y.loc[2,'resolved_at']=None
    elif mutation=='resolved_before_entry':
        y.loc[0,'resolved_at']=y.loc[0,'normal_entry_date']
    else:
        y['normal_net']=y.normal_net.astype(object)
        y.loc[2,'normal_net']=False
    with pytest.raises(ValueError):
        fit(x,y)


def test_future_return_and_future_entry_never_change_training():
    x,y=inputs()
    y.loc[0,['state','resolved_at','normal_net','floor_net']]=['complete','2023-05-09',.25,.20]
    y.loc[1,'normal_entry_date']=BOUNDARY
    original=fit(x,y)
    changed=y.copy()
    changed.attrs.update(y.attrs)
    changed.loc[0,['state','normal_net','floor_net']]=['future_unseen_value',-.9,-.95]
    changed.loc[1,['normal_entry_filled','state','resolved_at','normal_net','floor_net']]=[
        False,'entry_blocked','2023-05-09',0.,0.]
    assert fit(x,changed)==original
    assert original['fill_model']['unknown_entries']==1
    assert original['filled_with_unresolved_return']==1


def test_unavailable_fill_or_unknown_features_cannot_receive_numeric_expected_return():
    x,y=inputs()
    y['normal_entry_filled']=False
    y['state']='entry_blocked'
    y['resolved_at']=y.normal_entry_date
    y[['normal_net','floor_net']]=0.
    model=fit(x,y)
    assert model['fill_model']['status']=='one_class'
    result=forecast_two_stage(x.iloc[:3].assign(date='2023-05-04'),model)
    assert result.status.eq('fill_model_unavailable').all()
    assert result[['normal_expected_net','scenario_floor_expected_net','proxy_entry_probability']].isna().all().all()
    x,y=inputs()
    model=fit(x,y)
    test=x.iloc[:3].assign(date='2023-05-04').copy()
    test.loc[test.index[0],'value']=np.nan
    result=forecast_two_stage(test,model)
    assert result.status.iloc[0]=='unknown_features'
    assert pd.isna(result.proxy_entry_probability.iloc[0]) and pd.isna(result.normal_expected_net.iloc[0])


@pytest.mark.parametrize('mutation',['budget','feature_order','coefficients','rate','calibration','cutoff','coverage','future_feature'])
def test_forecast_rejects_changed_model_or_future_features(mutation):
    x,y=inputs()
    model=copy.deepcopy(fit(x,y))
    test=x.iloc[:1].assign(date='2023-05-04')
    if mutation=='budget':
        model['budget_rial']+=1
    elif mutation=='feature_order':
        model['fill_model']['predictors'].reverse()
    elif mutation=='coefficients':
        model['fill_model']['coefficients'][0]=float('nan')
    elif mutation=='rate':
        model['fill_model']['training_rate']=1.5
    elif mutation=='calibration':
        model['fill_model']['probability_calibrated']=True
    elif mutation=='cutoff':
        model['fill_model']['max_entry_date']=BOUNDARY
    elif mutation=='coverage':
        model['filled_with_unresolved_return']+=1
    else:
        test=test.assign(net_ret_2d=999.)
    with pytest.raises(ValueError):
        forecast_two_stage(test,model)


@pytest.mark.parametrize('mutation',['missing_label','nonboolean','entry_before_decision','future_decision','bad_return','duplicate'])
def test_training_rejects_unbound_inconsistent_or_leaky_inputs(mutation):
    x,y=inputs()
    if mutation=='missing_label':
        y=y.iloc[1:].copy()
    elif mutation=='nonboolean':
        y.loc[0,'normal_entry_filled']=1
    elif mutation=='entry_before_decision':
        y.loc[0,'normal_entry_date']=y.loc[0,'date']
    elif mutation=='future_decision':
        x.loc[0,'date']=BOUNDARY
    elif mutation=='bad_return':
        y.loc[0,'floor_net']=y.loc[0,'normal_net']+.5
    else:
        x=pd.concat([x,x.iloc[:1]],ignore_index=True)
    y.attrs.update(execution_contract(BUDGET))
    with pytest.raises(ValueError):
        fit(x,y)


def test_newton_solver_handles_intercept_only_and_descends_its_objective():
    design=np.ones((3,1))
    target=np.array([0.,1.,1.])
    weights=np.full(3,1/3)
    beta,iterations,gradient=_fit_logit(design,target,weights)
    assert iterations==0 and gradient<=1e-8
    assert _probability(beta[0])==pytest.approx(2/3,abs=1e-12)
    assert _loss(design,target,weights,beta,.01)<_loss(design,target,weights,np.array([0.]),.01)


def test_newton_solution_has_small_independent_finite_difference_gradient():
    design=np.array([[1.,-2.,.5],[1.,-1.,1.],[1.,-.2,-1.],
                     [1.,.3,.8],[1.,1.,-.5],[1.,2.,1.5]])
    target=np.array([0.,0.,1.,0.,1.,1.])
    weights=np.array([.10,.15,.20,.15,.20,.20])
    beta,_,gradient=_fit_logit(design,target,weights)
    assert beta is not None and gradient<=1e-8
    step=1e-5
    finite_differences=[]
    for index in range(len(beta)):
        shift=np.zeros_like(beta)
        shift[index]=step
        finite_differences.append((_loss(design,target,weights,beta+shift,.01)
                                   -_loss(design,target,weights,beta-shift,.01))/(2*step))
    assert np.max(np.abs(finite_differences))<1e-7


def test_validation_does_not_mark_a_changed_contract_as_calibrated():
    x,y=inputs()
    model=fit(x,y)
    model['execution_contract']['probability_of_profit']=.99
    with pytest.raises(ValueError):
        validate_two_stage_model(model)
