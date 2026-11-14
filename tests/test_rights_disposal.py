"""Synthetic sale tapes belong only to tests; no financial account is used."""

from fractions import Fraction

import pandas as pd
import pytest

from research.rights_disposal import model_rights_disposal


def tape():
    return pd.DataFrame([dict(date=f'2024-01-{day:02}', symbol='آزمونح', ins_code='123',
        open_raw=100, high_raw=110, low_raw=90, close_raw=100, close_final_raw=100,
        ref_price_raw=100, volume=100, value=10000) for day in (1, 2, 3)])


def run(frame=None, **kwargs):
    options=dict(units=12, start='2024-01-01', end_exclusive='2024-01-04', sell_fee=.01)
    options.update(kwargs)
    return model_rights_disposal(tape() if frame is None else frame, **options)


def test_capacity_partial_fills_fees_and_no_subscription_or_share_creation():
    before=tape()
    result=run(before)
    assert result['status']=='modeled_complete_whole_units'
    assert [fill['quantity'] for fill in result['fills']]==[5, 5, 2]
    assert result['modeled_gross_rial']==1200 and result['modeled_fee_rial']==12
    assert result['modeled_net_cash_rial']==1188
    assert result['cash_added_for_exercise_rial']==result['ordinary_shares_created']==0
    assert not result['actual_fills_verified'] and not result['entitlement_verified']
    pd.testing.assert_frame_equal(before,tape())


def test_fractional_rights_never_silently_disappear_or_become_cash():
    result=run(units=Fraction(25,2))
    assert result['sold_units']==12
    assert result['remaining_units']==dict(numerator=1,denominator=2)
    assert result['remaining_value_rial'] is None


def test_no_observed_trading_does_not_value_the_rights_at_zero():
    frame=tape()
    frame[['volume','value']]=0
    result=run(frame)
    assert result['status']=='unpriced_or_unsold_units'
    assert result['sold_units']==0 and result['remaining_value_rial'] is None
    assert result['modeled_net_cash_rial']==0 and result['entire_path_proceeds_rial'] is None


def test_missing_or_contradictory_activity_halts_instead_of_selecting_later_profitable_bars():
    frame=tape()
    frame.loc[0,'volume']=float('nan')
    result=run(frame)
    assert result['status']=='unknown_input_path' and result['entire_path_proceeds_rial'] is None
    assert result['sold_units']==0


def test_sell_lock_is_an_explicit_proxy_without_fabricated_fill():
    frame=tape()
    frame.loc[0,['open_raw','high_raw','low_raw','close_raw','close_final_raw']]=90
    frame.loc[0,'value']=9000
    result=run(frame)
    assert result['first_fill_date']=='2024-01-02'
    assert result['decisions'][0]['status']=='no_fill_assumed_locked_sell_proxy'


def test_stress_sell_uses_observed_low_and_one_percent_capacity():
    result=run(stress=True,participation=.01)
    assert result['sold_units']==3 and result['modeled_gross_rial']==270
    assert all(x['price_rial']==90 for x in result['fills'])


def test_slippage_remains_inside_the_observed_range():
    result=run(units=1,slippage_bps=100)
    assert result['fills'][0]['price_rial']==99
    assert run(units=1,stress=True,slippage_bps=100)['fills'][0]['price_rial']==90


@pytest.mark.parametrize('kwargs', [dict(units=True),dict(units=0),dict(units=-1),
    dict(sell_fee=1),dict(participation=.051),dict(stress=1),dict(start='2024-02-30')])
def test_bad_scenario_inputs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        run(**kwargs)


def test_identity_mixing_duplicates_and_zero_price_with_positive_activity_fail_closed():
    frame=tape()
    frame.loc[1,'symbol']='دیگر'
    with pytest.raises(ValueError):
        run(frame)
    with pytest.raises(ValueError):
        run(pd.concat([tape(),tape()]))
    frame=tape()
    frame.loc[0,'open_raw']=0
    assert run(frame)['status']=='unknown_input_path'
