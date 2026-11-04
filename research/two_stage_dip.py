"""Preregistered proxy-entry and conditional-return model; never a trading signal.

The entry probability describes the existing OHLC execution proxy. It is not
broker fill probability, probability of profit, or a calibrated forecast.
"""

import numpy as np

from core.timeutils import is_canonical_iso_date
from research.expectancy import _fit_matrix, _forecast_matrix, _keys, _validate_model
from research.probability import _numeric
from research.sized_dip_expectancy import VERSION as RETURN_VERSION
from research.sized_dip_expectancy import _matrix, predictors, recipe
from research.sized_dip_outcomes import budget_value, execution_contract

VERSION = 'two-stage-sized-dip-v1'
FILL_VERSION = 'proxy-entry-logit-v1'
PENALTY = .01
MAX_ITERATIONS = 50
GRADIENT_TOLERANCE = 1e-8
MAX_BACKTRACK = 20


def _probability(logit):
    return np.exp(-np.logaddexp(0., -np.asarray(logit, dtype=float)))


def _loss(design, target, weights, beta, penalty):
    z = design @ beta
    return float(weights @ (np.logaddexp(0., z) - target*z) + .5*penalty*np.dot(beta[1:], beta[1:]))


def _fit_logit(design, target, weights):
    """Deterministic damped Newton solver for the registered weighted L2 objective."""
    rate = float(weights @ target)
    beta = np.zeros(design.shape[1])
    beta[0] = np.log(rate/(1-rate))
    penalty = np.diag([0.]+[PENALTY]*(design.shape[1]-1))
    for iteration in range(MAX_ITERATIONS+1):
        p = _probability(design @ beta)
        gradient = design.T @ (weights*(p-target)) + penalty @ beta
        if not np.isfinite(gradient).all():
            break
        if np.max(np.abs(gradient)) <= GRADIENT_TOLERANCE:
            return beta, iteration, float(np.max(np.abs(gradient)))
        if iteration == MAX_ITERATIONS:
            break
        curvature = weights*p*(1-p)
        hessian = design.T @ (curvature[:,None]*design) + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        if not np.isfinite(step).all():
            break
        previous = _loss(design,target,weights,beta,PENALTY)
        descent = float(gradient @ step)
        scale = 1.
        for _ in range(MAX_BACKTRACK):
            candidate = beta-scale*step
            if (_loss(design,target,weights,candidate,PENALTY)
                    <= previous-1e-4*scale*descent):
                beta = candidate
                break
            scale *= .5
        else:
            break
    return None, iteration, None


def _known_entries(outcomes, boundary):
    """Mask labels unavailable at the training boundary before model fitting."""
    _keys(outcomes)
    if not is_canonical_iso_date(boundary) or outcomes.date.ge(boundary).any():
        raise ValueError('Training decision dates must precede the training boundary')
    required = {'normal_entry_filled', 'normal_entry_date', 'state', 'resolved_at', 'normal_net', 'floor_net'}
    if not required <= set(outcomes):
        raise ValueError('Entry availability and joint return labels are required')
    present = outcomes.normal_entry_filled.notna()
    dates = outcomes.loc[present,'normal_entry_date']
    if (any(not is_canonical_iso_date(d) for d in dates)
            or dates.le(outcomes.loc[present,'date']).any()):
        raise ValueError('A known entry label must have a later canonical entry session')
    known = present & outcomes.normal_entry_date.fillna('').lt(boundary)
    if not outcomes.loc[known,'normal_entry_filled'].map(lambda v:isinstance(v,(bool,np.bool_))).all():
        raise ValueError('Known entry labels must be explicit booleans')
    return known


def _check_observable_outcomes(rows,known,boundary):
    """Reject contradictions in labels available at cutoff without reading future returns."""
    resolved=rows.resolved_at.notna()
    if any(not is_canonical_iso_date(day) for day in rows.loc[known & resolved,'resolved_at']):
        raise ValueError('Observed entry has an invalid outcome resolution date')
    filled=known & rows.normal_entry_filled.eq(True).fillna(False)  # noqa: E712
    unfilled=known & rows.normal_entry_filled.eq(False).fillna(False)  # noqa: E712
    blocked=rows.loc[unfilled]
    if (not blocked.state.eq('entry_blocked').all()
            or blocked.resolved_at.isna().any()
            or blocked.resolved_at.ne(blocked.normal_entry_date).any()):
        raise ValueError('Known non-entry must resolve as blocked on the entry session')
    for name in ('normal_net','floor_net'):
        values=_numeric(blocked[name])
        if not values.notna().all() or values.ne(0).any():
            raise ValueError('Known non-entry must have a numeric zero in both scenarios')
    visible=filled & resolved & rows.resolved_at.fillna('').lt(boundary)
    complete=rows.loc[visible]
    if (not complete.state.eq('complete').all()
            or complete.resolved_at.le(complete.normal_entry_date).any()):
        raise ValueError('An observed filled return must resolve as complete after entry')
    unresolved=rows.loc[filled & ~resolved]
    if not unresolved.state.eq('unknown').all():
        raise ValueError('An unobserved filled return must remain unknown')
    for name in ('normal_net','floor_net'):
        if unresolved[name].notna().any():
            raise ValueError('An unresolved filled return cannot contain an aggregate result')


def _fill_model(features, outcomes, x, *, boundary, budget, min_rows, min_sessions):
    known = _known_entries(outcomes,boundary)
    complete = np.isfinite(x).all(axis=1)
    usable = known.to_numpy() & complete
    joined = outcomes.loc[usable]
    sessions = int(joined.date.nunique())
    fill = np.asarray(joined.normal_entry_filled,dtype=bool)
    model = dict(version=FILL_VERSION,train_end_exclusive=boundary,predictors=list(predictors('sized11')),
        budget_rial=budget,penalty=PENALTY,max_iterations=MAX_ITERATIONS,gradient_tolerance=GRADIENT_TOLERANCE,
        max_backtracking_steps=MAX_BACKTRACK,training_rows=len(outcomes),
        known_entries=int(known.sum()),unknown_entries=int((~known).sum()),
        incomplete_feature_rows=int((~complete).sum()),fitted_rows=int(usable.sum()),fitted_sessions=sessions,
        min_rows=min_rows,min_sessions=min_sessions,max_entry_date=max(joined.normal_entry_date,default=None),
        training_rate=None,coefficients=None,status='insufficient_history',
        probability_is_proxy=True,probability_calibrated=False,probability_of_profit=None,accepted_for_signals=False)
    if usable.sum()<min_rows or sessions<min_sessions:
        return model
    if len(np.unique(fill))!=2:
        model['status']='one_class'
        return model
    a=x[usable]
    counts=joined.groupby('date').symbol.transform('size').to_numpy()
    weights=1./(sessions*counts)
    center=np.sum(a*weights[:,None],axis=0)
    scale=np.sqrt(np.sum((a-center)**2*weights[:,None],axis=0))
    if not np.isfinite(center).all() or not np.isfinite(scale).all():
        raise ValueError('Entry model standardization is nonfinite')
    active=scale>1e-12
    design=np.column_stack([np.ones(len(a)),(a[:,active]-center[active])/scale[active]])
    coefficients,iterations,gradient=_fit_logit(design,fill.astype(float),weights)
    if coefficients is None:
        model['status']='optimizer_unconverged'
        return model
    model.update(status='fitted',active_columns=active.tolist(),center=center.tolist(),scale=scale.tolist(),
        minimum=a.min(axis=0).tolist(),maximum=a.max(axis=0).tolist(),coefficients=coefficients.tolist(),
        training_rate=float(weights@fill),iterations=iterations,gradient_inf=gradient)
    return model


def validate_two_stage_model(model):
    if (not isinstance(model,dict) or model.get('version')!=VERSION or model.get('variant')!='sized11'
            or model.get('execution_contract')!=execution_contract(budget_value(model.get('budget_rial')))
            or model.get('derived_feature_recipe')!=recipe('sized11') or model.get('accepted_for_signals') is not False
            or model.get('probability_of_profit') is not None):
        raise ValueError('Two-stage model family or execution contract differs')
    conditional=model.get('conditional_return_model')
    _validate_model(conditional,version=RETURN_VERSION,predictors=predictors('sized11'))
    fill=model.get('fill_model')
    if (not isinstance(fill,dict) or fill.get('version')!=FILL_VERSION
            or fill.get('predictors')!=list(predictors('sized11'))
            or fill.get('train_end_exclusive')!=conditional['train_end_exclusive']
            or fill.get('budget_rial')!=model['budget_rial'] or fill.get('penalty')!=PENALTY
            or fill.get('max_iterations')!=MAX_ITERATIONS or fill.get('gradient_tolerance')!=GRADIENT_TOLERANCE
            or fill.get('max_backtracking_steps')!=MAX_BACKTRACK or fill.get('probability_is_proxy') is not True
            or fill.get('probability_calibrated') is not False or fill.get('probability_of_profit') is not None
            or fill.get('accepted_for_signals') is not False):
        raise ValueError('Entry-model metadata differs')
    coverage=('known_filled_entries','conditional_outcomes_known','filled_with_unresolved_return')
    if (model.get('conditional_missingness_unbiased') is not False
            or any(type(model.get(k)) is not int or model[k]<0 for k in coverage)
            or model['known_filled_entries']!=model['conditional_outcomes_known']+model['filled_with_unresolved_return']
            or conditional['training_rows']!=model['conditional_outcomes_known']):
        raise ValueError('Conditional-return coverage is inconsistent')
    counts=('training_rows','known_entries','unknown_entries','incomplete_feature_rows',
            'fitted_rows','fitted_sessions','min_rows','min_sessions')
    if (any(type(fill.get(k)) is not int or fill[k]<0 for k in counts)
            or fill['known_entries']+fill['unknown_entries']!=fill['training_rows']
            or fill['fitted_rows']>fill['known_entries'] or fill['fitted_sessions']>fill['fitted_rows']
            or fill['incomplete_feature_rows']>fill['training_rows']
            or fill['fitted_rows']>fill['training_rows']-fill['incomplete_feature_rows']
            or model['known_filled_entries']>fill['known_entries']
            or fill['min_rows']<3 or fill['min_sessions']<3):
        raise ValueError('Entry-model coverage is inconsistent')
    last=fill.get('max_entry_date')
    if ((fill['fitted_rows'] and (not is_canonical_iso_date(last) or last>=fill['train_end_exclusive']))
            or not fill['fitted_rows'] and last is not None):
        raise ValueError('Entry training date exceeds its boundary')
    if fill['coefficients'] is None:
        if fill['status'] not in ('insufficient_history','one_class','optimizer_unconverged'):
            raise ValueError('Unfitted entry model has invalid status')
        return
    if fill['status']!='fitted' or fill['fitted_rows']<fill['min_rows'] or fill['fitted_sessions']<fill['min_sessions']:
        raise ValueError('Fitted entry model has insufficient data')
    n=len(predictors('sized11'))
    active=fill.get('active_columns')
    if not isinstance(active,list) or len(active)!=n or any(type(v) is not bool for v in active):
        raise ValueError('Entry-model active predictors differ')
    def vector(name,length):
        v=fill.get(name)
        if not isinstance(v,list) or len(v)!=length or any(type(z) not in (int,float) or not np.isfinite(z) for z in v):
            raise ValueError('Entry-model numeric vector is invalid')
        return np.asarray(v,dtype=float)
    center,scale=vector('center',n),vector('scale',n)
    lower,upper=vector('minimum',n),vector('maximum',n)
    vector('coefficients',sum(active)+1)
    tol=np.finfo(float).eps*np.maximum(1.,np.maximum(np.abs(lower),np.abs(upper)))*32
    if ((scale<0).any() or not np.array_equal(scale>1e-12,active) or (lower>upper).any()
            or (center<lower-tol).any() or (center>upper+tol).any()
            or type(fill.get('iterations')) is not int or not 0<=fill['iterations']<=MAX_ITERATIONS
            or type(fill.get('gradient_inf')) not in (int,float) or not 0<=fill['gradient_inf']<=GRADIENT_TOLERANCE
            or type(fill.get('training_rate')) not in (int,float) or not 0<fill['training_rate']<1):
        raise ValueError('Entry-model fit or convergence is inconsistent')


def fit_two_stage(features,outcomes,*,train_end_exclusive,budget_rial,min_rows=200,min_sessions=60):
    budget_value(budget_rial)
    if type(min_rows) is not int or min_rows<3 or type(min_sessions) is not int or min_sessions<3:
        raise ValueError('Computational minima must be explicit integers')
    if any(outcomes.attrs.get(k)!=v for k,v in execution_contract(budget_rial).items()):
        raise ValueError('Independent budget target provenance differs')
    x=_matrix(features,train_end_exclusive,training=True,variant='sized11',budget_rial=budget_rial)
    _keys(outcomes)
    joined=features[['date','symbol']].merge(outcomes,on=['date','symbol'],how='left',validate='one_to_one',indicator=True)
    if joined._merge.ne('both').any() or len(joined)!=len(outcomes):
        raise ValueError('Every training decision requires one explicit outcome')
    joined=joined.drop(columns='_merge')
    known=_known_entries(joined,train_end_exclusive)
    _check_observable_outcomes(joined,known,train_end_exclusive)
    fill=_fill_model(features,joined,x,boundary=train_end_exclusive,budget=budget_rial,
                     min_rows=min_rows,min_sessions=min_sessions)
    eligible=(known & joined.normal_entry_filled.eq(True).fillna(False) & joined.resolved_at.notna()
              & joined.resolved_at.fillna('').lt(train_end_exclusive) & joined.state.eq('complete'))  # noqa: E712
    subset_features=features.loc[eligible.to_numpy()].reset_index(drop=True)
    subset_outcomes=joined.loc[eligible,['date','symbol','state','resolved_at','normal_net','floor_net']].rename(
        columns={'floor_net':'stress_net'}).reset_index(drop=True)
    subset_x=x[eligible.to_numpy()]
    conditional=_fit_matrix(subset_features,subset_outcomes,subset_x,train_end_exclusive=train_end_exclusive,
        min_sessions=min_sessions,min_rows=min_rows,version=RETURN_VERSION,predictors=predictors('sized11'))
    model=dict(version=VERSION,variant='sized11',budget_rial=budget_rial,execution_contract=execution_contract(budget_rial),
        derived_feature_recipe=recipe('sized11'),fill_model=fill,conditional_return_model=conditional,
        known_filled_entries=int((known & joined.normal_entry_filled.eq(True).fillna(False)).sum()),  # noqa: E712
        conditional_outcomes_known=int(eligible.sum()),
        filled_with_unresolved_return=int((known & joined.normal_entry_filled.eq(True).fillna(False) & ~eligible).sum()),  # noqa: E712
        conditional_missingness_unbiased=False,probability_of_profit=None,accepted_for_signals=False)
    validate_two_stage_model(model)
    return model


def forecast_two_stage(features,model):
    validate_two_stage_model(model)
    fill,conditional=model['fill_model'],model['conditional_return_model']
    x=_matrix(features,conditional['train_end_exclusive'],training=False,variant='sized11',budget_rial=model['budget_rial'])
    out=_forecast_matrix(features,conditional,x).rename(columns={'stress_expected_net':'scenario_floor_expected_net'})
    out['proxy_entry_probability']=np.nan
    if fill['coefficients'] is None:
        out[['normal_expected_net','scenario_floor_expected_net']]=np.nan
        out['status']='fill_model_unavailable'
        return out
    active=np.asarray(fill['active_columns'],dtype=bool)
    lower,upper=np.asarray(fill['minimum']),np.asarray(fill['maximum'])
    tolerance=np.finfo(float).eps*np.maximum(1.,np.maximum(np.abs(lower),np.abs(upper)))*32
    complete=np.isfinite(x).all(axis=1)
    supported=complete&((x>=lower-tolerance)&(x<=upper+tolerance)).all(axis=1)
    valid=supported&out.status.eq('forecasted').to_numpy()
    if valid.any():
        center,scale=np.asarray(fill['center']),np.asarray(fill['scale'])
        design=np.column_stack([np.ones(valid.sum()),(x[valid][:,active]-center[active])/scale[active]])
        probability=_probability(design@np.asarray(fill['coefficients']))
        out.loc[valid,'proxy_entry_probability']=probability
        out.loc[valid,'normal_expected_net']*=probability
        out.loc[valid,'scenario_floor_expected_net']*=probability
    unsupported=~supported&out.status.eq('forecasted').to_numpy()
    out.loc[unsupported,['normal_expected_net','scenario_floor_expected_net']]=np.nan
    out.loc[unsupported,'status']=np.where(complete[unsupported],'outside_training_support','unknown_features')
    return out
