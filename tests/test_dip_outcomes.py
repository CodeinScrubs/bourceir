"""Synthetic prices live only here; compare targets with the shared-capital execution oracle."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from research.dip import DipRule, simulate_symbol
from research.dip_outcomes import RULE, dip_attempt_outcomes


def sample():
    days = pd.date_range('2024-01-01',periods=5).strftime('%Y-%m-%d').to_numpy()
    data = pd.DataFrame(dict(symbol='آزمون',date=days,open_raw=100.,low_raw=99.,high_raw=101.,
        close_raw=100.,close_final_raw=100.,ref_price_raw=100.,volume=10000.,value=1000000.))
    data.loc[1,['open_raw','low_raw','high_raw','close_raw','value']]=[97.,96.,101.,100.,980000.]
    data.loc[2,['open_raw','low_raw','high_raw','close_raw']]=[99.,98.,102.,101.]
    decisions = data.loc[:0,['date','symbol']]
    return data,decisions,days


def labels(data=None,selected=None,days=None,**kwargs):
    if data is None:
        data,selected,days = sample()
    return dip_attempt_outcomes(data,selected,days,**kwargs)


def oracle(data,selected,days,stress,rule=RULE):
    result = replay_portfolio(data,selected,days,PortfolioConfig(initial_capital_rial=110.,horizon=rule.hold,
        max_positions=1,stress=stress),dip_rule=rule)
    buys=result.executions.loc[result.executions.side.eq('buy')]
    sells=result.executions.loc[result.executions.side.eq('sell')]
    assert len(buys)==len(sells)==1 and buys.iloc[0].quantity==sells.iloc[0].quantity==1
    buy,sell=buys.iloc[0],sells.iloc[0]
    net=(sell.price_rial-sell.fee_rial)/(buy.price_rial+buy.fee_rial)-1
    return buy,sell,net


@pytest.mark.parametrize('stress',[False,True])
@pytest.mark.parametrize('entry_kind',['open','limit'])
def test_known_one_unit_price_time_and_net_agree_with_the_full_portfolio(stress,entry_kind):
    data,selected,days = sample()
    if entry_kind=='limit':
        data.loc[1,'open_raw']=99.
    result=labels(data,selected,days).iloc[0]
    buy,sell,net=oracle(data,selected,days,stress)
    prefix='stress' if stress else 'normal'
    assert result[prefix+'_entry_date']==buy.date
    assert result[prefix+'_exit_date']==sell.date==days[2]
    assert result[prefix+'_entry_price']==buy.price_rial
    assert result[prefix+'_exit_price']==sell.price_rial
    assert result[prefix+'_observed_net']==pytest.approx(net,abs=1e-14)
    assert result.state=='complete' and result.resolved_at==days[2]


def test_legacy_same_day_target_is_not_reused_for_the_two_session_family():
    data,selected,days = sample()
    legacy=simulate_symbol(data,days,RULE).iloc[0]
    aligned=labels(data,selected,days).iloc[0]
    assert legacy.exit_date==days[1] and aligned.normal_exit_date==days[2]
    assert aligned.normal_entry_date!=aligned.normal_exit_date


def test_actual_stress_can_outperform_normal_because_capacity_changes_exit_dates():
    data,selected,days = sample()
    data.loc[2,['volume','value']]=[20.,1980.]
    data.loc[3,['open_raw','low_raw','high_raw','close_raw','value']]=[90.,89.,92.,90.,900000.]
    row=labels(data,selected,days).iloc[0]
    for stress in (False,True):
        _,sell,net=oracle(data,selected,days,stress)
        prefix='stress' if stress else 'normal'
        assert row[prefix+'_exit_date']==sell.date
        assert row[prefix+'_observed_net']==pytest.approx(net)
    assert row.stress_observed_net>row.normal_net
    assert row.stress_exit_date==days[2] and row.normal_exit_date==days[3]
    assert row.floor_net==row.normal_net and row.resolved_at==days[3]


@pytest.mark.parametrize('cause',['limit','capacity'])
def test_only_observed_known_failed_attempts_are_zero(cause):
    data,selected,days=sample()
    if cause=='limit':
        data.loc[1,['open_raw','low_raw']]=[100.,99.]
        data.loc[1,'value']=1000000.
    else:
        data.loc[0,['volume','value']]=[10.,1000.]
    row=labels(data,selected,days).iloc[0]
    assert row.state=='entry_blocked' and row.normal_net==row.floor_net==0
    assert row.resolved_at==days[1] and not row.normal_entry_filled


@pytest.mark.parametrize('cause',['entry_missing','exit_missing','entry_invalid','exit_invalid','reference','censored'])
def test_missing_invalid_and_uncertain_economic_outcomes_stay_in_the_denominator(cause):
    data,selected,days=sample()
    if cause=='entry_missing':
        data=data.drop(index=1)
    elif cause=='exit_missing':
        data=data.drop(index=2)
    elif cause=='entry_invalid':
        data.loc[1,'open_raw']=np.nan
    elif cause=='exit_invalid':
        data.loc[2,'value']=100.
    elif cause=='reference':
        data.loc[2,'ref_price_raw']=90.
    else:
        data=data.iloc[:2]
        days=days[:2]
    result=labels(data,selected,days)
    assert len(result)==1
    row=result.iloc[0]
    assert row.state=='unknown' and row.resolved_at is None
    assert pd.isna(row.normal_net) and pd.isna(row.floor_net)
    if cause=='entry_missing':
        assert row.normal_entry_filled is None and row.normal_reason=='missing_entry_bar'


def test_one_known_scenario_is_retained_without_training_on_a_partly_unknown_pair():
    data,selected,days=sample()
    data.loc[2,['volume','value']]=[20.,1980.]
    data=data.drop(index=3)
    row=labels(data,selected,days).iloc[0]
    assert row.stress_state=='complete' and np.isfinite(row.stress_observed_net)
    assert row.normal_state=='unknown' and row.state=='unknown'
    assert pd.isna(row.normal_net) and pd.isna(row.floor_net)


def test_reference_change_before_entry_does_not_create_a_right_for_the_new_buyer():
    data,selected,days=sample()
    data.loc[1:,['open_raw','low_raw','high_raw','close_raw','close_final_raw','ref_price_raw','value']]=[
        50.,49.,51.,50.,50.,50.,500000.]
    data.loc[1,['open_raw','low_raw','value']]=[48.,47.,490000.]
    row=labels(data,selected,days).iloc[0]
    assert row.state=='complete' and row.normal_entry_date==days[1]


def test_future_unneeded_prices_and_input_mutation_do_not_rewrite_a_resolved_outcome():
    data,selected,days=sample()
    original=data.copy(deep=True)
    before=labels(data,selected,days)
    pd.testing.assert_frame_equal(data,original)
    data.loc[4,'ref_price_raw']=10.
    after=labels(data,selected,days)
    pd.testing.assert_frame_equal(before,after,check_exact=True)
    cached=prepare_portfolio_prices(original)
    pd.testing.assert_frame_equal(before,labels(cached,selected,days),check_exact=True)


@pytest.mark.parametrize('mutation',['sealed','duplicate','unknown_calendar','future_column','bad_participation','bad_fee'])
def test_invalid_scope_and_units_are_rejected(mutation):
    data,selected,days=sample()
    kwargs={}
    if mutation=='sealed':
        days=np.append(days,'2025-03-21')
    elif mutation=='duplicate':
        selected=pd.concat([selected,selected])
    elif mutation=='unknown_calendar':
        selected=selected.assign(date='2023-12-31')
    elif mutation=='future_column':
        selected=selected.assign(net_ret_1d=1.)
    elif mutation=='bad_participation':
        kwargs['participation']=.06
    else:
        kwargs['fees']={'buy':True,'sell':.01}
    with pytest.raises(ValueError):
        labels(data,selected,days,**kwargs)


def test_stop_loss_is_not_silently_supported_or_horizon_zero_enabled():
    with pytest.raises(ValueError):
        labels(rule=replace(RULE,stop_loss=.02))
    with pytest.raises(ValueError):
        DipRule(hold=0)
