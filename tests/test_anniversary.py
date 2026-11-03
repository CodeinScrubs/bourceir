"""Chronological selection, equal sleeves, blocked fills and archived replay."""

import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from core.opportunity_radar import RAW
from research.anniversary import aggregate_sleeves, asof_selection, replay_sleeve
from research.anniversary_run import evaluate, freeze_selection, review


def market(n=35):
    days=pd.date_range('2024-01-01',periods=n).strftime('%Y-%m-%d').tolist()
    rows=[]
    registry=[]
    for i in range(25):
        name=chr(ord('a')+i)
        close=100*(1+i/2000)**np.arange(n)
        rows.append(pd.DataFrame(dict(symbol=name,date=days,open_raw=close,high_raw=close*1.01,
            low_raw=close*.99,close_raw=close,close_final_raw=close,ref_price_raw=np.r_[100.,close[:-1]],
            volume=100000.,value=100000*close)))
        registry.append(dict(symbol_fa=name,ins_code=str(i+1),instrument_type_code=300))
    return pd.concat(rows,ignore_index=True),registry,days


def selection(p,r,d):
    return asof_selection(p,r,requested_start=d,k=10,min_value=1.)


def case(p,name,days,**extra):
    return replay_sleeve(p,symbol=name,decision=days[0],calendar=days,initial_capital_rial=10000.,
        fees=dict(settings.FEES),slippage_bps=10.,**extra)


def test_selection_slices_future_first_and_ignores_adjusted_columns_and_labels():
    p,r,days=market()
    before=selection(p,r,days[22])
    p['adj_close']=1e50
    p['future_return']=1e99
    for column in RAW:
        p.loc[p.date.gt(days[22]),column]=np.nan
    after=selection(p,r,days[22])
    pd.testing.assert_frame_equal(before['panel'],after['panel'])
    assert before['selected'].symbol.tolist()==list('yxwvutsrqp')
    assert before['decision_date']==days[22]
    assert before['selected'].probability_of_multibagger.isna().all()


def test_one_missing_market_day_cannot_be_compressed_into_twenty_company_observations():
    p,r,days=market()
    p=p.loc[~(p.symbol.eq('y')&p.date.eq(days[15]))]
    chosen=selection(p,r,days[22])
    assert 'y' not in set(chosen['selected'].symbol)
    row=chosen['panel'].set_index('symbol').loc['y']
    assert row.status=='incomplete_or_invalid_21_session_window' and pd.isna(row.ret_20d)


@pytest.mark.parametrize('field,value',[('open_raw',np.nan),('close_raw',999999),('ref_price_raw',0),
    ('value',1e30),('volume',2.5),('close_final_raw',True)])
def test_bad_asof_data_does_not_receive_a_plausible_momentum_score(field,value):
    p,r,days=market()
    p[field]=p[field].astype(object)
    p.loc[p.symbol.eq('y')&p.date.eq(days[20]),field]=value
    chosen=selection(p,r,days[22])
    assert 'y' not in set(chosen['selected'].symbol)


def test_tie_order_is_value_then_symbol_and_never_future_performance():
    p,r,days=market()
    for name in ['x','y']:
        mask=p.symbol.eq(name)
        p.loc[mask,['open_raw','high_raw','low_raw','close_raw','close_final_raw','ref_price_raw']]=[100,101,99,100,100,100]
        p.loc[mask,'value']=10000000
    a=selection(p,r,days[22])
    b=selection(p.sample(frac=1,random_state=2),r[::-1],days[22])
    pd.testing.assert_frame_equal(a['selected'],b['selected'])


def test_blocked_buy_is_cash_without_later_replacement():
    p,_,days=market(8)
    own=p.loc[p.symbol.eq('a')].copy()
    own.loc[own.date.eq(days[1]),['open_raw','high_raw','low_raw','close_raw','close_final_raw','value']]=[103,103,103,103,103,10300000]
    result,curve,executions,_=case(own,'a',days)
    assert result['status']=='cash_unfilled' and result['net_model_return']==0
    assert executions.empty and curve.cash_rial.eq(10000).all()


def test_unknown_execution_is_not_idle_cash_and_final_stale_mark_is_not_current_wealth():
    p,_,days=market(8)
    own=p.loc[p.symbol.eq('a')].copy()
    own.loc[own.date.eq(days[1]),'value']=np.nan
    result,*_=case(own,'a',days)
    assert result['net_model_return'] is None
    own=p.loc[p.symbol.eq('a')&p.date.ne(days[-1])]
    result,curve,_,decisions=case(own,'a',days)
    assert result['status']=='invalid_data' and result['net_model_return'] is None
    assert 'missing_exit_bar' in set(decisions.status)
    assert pd.isna(curve.equity_rial.iloc[-1])


def test_off_calendar_holding_observation_cannot_be_dropped_to_improve_a_result():
    p,_,days=market(8)
    own=p.loc[p.symbol.eq('a')]
    result,*_=case(own,'a',[d for d in days if d!=days[4]])
    assert result['status']=='off_calendar_path' and result['net_model_return'] is None


@pytest.mark.parametrize('explicit_actions',[False,True])
def test_calendar_conflict_preserves_unknown_accounting_without_running_engine(monkeypatch,explicit_actions):
    from backtest.cash_dividends import CashDividendBook
    from backtest.corporate_actions import BonusDividendBook
    from research import anniversary as module

    p,_,days=market(8)
    book=BonusDividendBook([],dividends=CashDividendBook([])) if explicit_actions else None
    monkeypatch.setattr(module,'replay_portfolio',lambda *a,**k:pytest.fail('Conflicting calendar must not run accounting'))
    result,curve,executions,decisions=case(p,'a',[d for d in days if d!=days[4]],corporate_actions=book)
    assert result['status']=='off_calendar_path'
    for key in ('net_model_return','invested_rial','cash_final_rial','entry_quantity','fees_rial',
                'corporate_action_events','fully_liquidated','model_final_value_rial'):
        assert result[key] is None
    assert executions.empty and decisions.status.tolist()==['off_calendar_path']
    assert curve.equity_rial.isna().all() and curve.cash_rial.isna().all()
    assert not result['engine_assumptions']['replay_performed']
    assert not result['accepted_for_signals']
    if book is not None:
        assert book.credit_exit_session is None and not book.ledger
        assert result['corporate_accounting']['status']=='not_replayed'
        assert result['corporate_accounting']['cash_dividend_income_rial'] is None


def test_calendar_unknown_sleeve_keeps_total_costs_and_return_unknown():
    p,_,days=market(8)
    one,c1,*_=case(p,'a',days)
    two,c2,*_=case(p,'y',[d for d in days if d!=days[4]])
    result,curve=aggregate_sleeves([one,two],[c1,c2],k=2,initial_capital_rial=20000.)
    assert result['sleeves']==2 and result['unknown_sleeves']==1 and result['known_sleeves']==1
    for key in ('net_model_return','final_model_value_rial','net_cash_return','fees_rial',
                'corporate_action_events','fully_sized_sleeves','fully_liquidated_sleeves',
                'max_drawdown','max_loss_from_initial'):
        assert result[key] is None
    assert curve.equity_rial.isna().all()


def test_equal_initial_capital_mean_matches_sum_and_unknown_sleeve_stays_in_denominator():
    p,_,days=market(8)
    one,c1,*_=case(p,'a',days)
    two,c2,*_=case(p,'y',days)
    result,total=aggregate_sleeves([one,two],[c1,c2],k=2,initial_capital_rial=20000)
    assert result['net_model_return']==pytest.approx((one['net_model_return']+two['net_model_return'])/2)
    assert total.equity_rial.iloc[-1]==pytest.approx(result['final_model_value_rial'])
    unknown=dict(two,net_model_return=None)
    result,_=aggregate_sleeves([one,unknown],[c1,c2],k=2,initial_capital_rial=20000)
    assert result['net_model_return'] is None and result['max_drawdown'] is None
    with pytest.raises(ValueError,match='denominator'):
        aggregate_sleeves([one],[c1],k=2,initial_capital_rial=20000)


def test_review_preserves_unknown_boolean_across_a_real_parquet_roundtrip(tmp_path):
    from research.anniversary_run import _same_frame

    # An invalid terminal row has no risk flag. Arrow restores the object's NaN
    # as None; both mean unknown, rather than a changed risk decision.
    frame=pd.DataFrame(dict(date=['2024-01-01','2024-01-02'],risk_stopped=[False,np.nan],
                            equity_rial=[100.,np.nan]))
    target=tmp_path/'curve.parquet'
    frame.to_parquet(target,index=False)
    _same_frame(target,frame)


@pytest.mark.parametrize('replacement',[False,True])
def test_review_does_not_equate_an_unknown_risk_flag_with_a_boolean(tmp_path,replacement):
    from research.anniversary_run import _same_frame

    expected=pd.DataFrame(dict(risk_stopped=pd.Series([False,np.nan],dtype=object)))
    altered=expected.copy()
    altered.loc[1,'risk_stopped']=replacement
    target=tmp_path/'curve.parquet'
    altered.to_parquet(target,index=False)
    with pytest.raises(AssertionError):
        _same_frame(target,expected)


@pytest.fixture
def pinned(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    root=settings.DATA_DIR/'private'/'case'
    root.mkdir(parents=True)
    p,r,days=market(27)
    p.to_parquet(root/'prices.parquet',index=False)
    (root/'registry.json').write_text(json.dumps(r),encoding='utf-8')
    intent=dict(version='anniversary-replay-v1',requested_start=days[22],requested_end=days[-1],
        k=10,min_daily_value_rial=1.,fees=dict(settings.FEES),slippage_bps=10.,participation=.05,
        initial_capital_rial=100000.,accepted_for_signals=False,period_previously_observed=True,models_fitted=0)
    (root/'intent.json').write_text(json.dumps(intent),encoding='utf-8')
    return root,intent


def freeze(root):
    return freeze_selection(root/'study',prices_path=root/'prices.parquet',registry_path=root/'registry.json',intent_path=root/'intent.json')


def test_two_phase_registration_precedes_selection_and_outcomes_replay_exactly(pinned,monkeypatch):
    root,_=pinned
    from research import anniversary_run as run

    register=run._register
    events=[]
    def logged(*args):
        events.append(args[1]); return register(*args)
    monkeypatch.setattr(run,'_register',logged)
    selected=freeze(root)
    assert events[0].endswith('-PROTOCOL') and events[1].endswith('-SELECTION')
    assert selected['outcomes_read'] is False and len(selected['selected'])==10
    result=evaluate(root/'study')
    assert result['scenarios']['normal']['net_model_return'] is not None
    assert result['scenarios']['normal']['random_controls']==100
    assert not result['economic_profit_verified'] and not result['unseen_test']
    assert review(root/'study')['verified']
    original_code=run.code
    with monkeypatch.context() as patched:
        patched.setattr(run,'code',lambda:dict(changed='technical-patch'))
        with pytest.raises(ValueError,match='code differs'):
            review(root/'study')
        assert not review(root/'study',allow_review_code_change=True)['current_code_matches_capture']
    assert run.code is original_code
    with pytest.raises(ValueError,match='already exists'):
        freeze(root)
    with pytest.raises(ValueError,match='Result exists'):
        evaluate(root/'study')
    (root/'study'/'selected.parquet').write_bytes(b'changed')
    with pytest.raises(ValueError,match='selection artifact'):
        review(root/'study')


def test_annual_replay_only_passes_one_predecessor_to_the_existing_engine(monkeypatch):
    from research import anniversary as module

    p,_,days=market(45)
    engine=module.replay_portfolio
    lengths=[]
    def seen(prices,*args,**kwargs):
        lengths.append(len(prices)); return engine(prices,*args,**kwargs)
    monkeypatch.setattr(module,'replay_portfolio',seen)
    result,curve,executions,_=case(p,'a',days[30:])
    assert result['net_model_return'] is not None and lengths==[16]
    from backtest.portfolio import PortfolioConfig
    baseline=engine(p.loc[p.symbol.eq('a')],pd.DataFrame([dict(symbol='a',date=days[30],rank=1)]),days[30:],
        PortfolioConfig(initial_capital_rial=10000.,max_positions=1,horizon=13,slippage_bps=10.),start=days[30])
    pd.testing.assert_frame_equal(curve,baseline.equity)
    pd.testing.assert_frame_equal(executions,baseline.executions)


@pytest.mark.parametrize('start,end',[('2025-10-23','2026-10-23'),('2024-10-23','2025-10-23')])
def test_sealed_period_needs_explicit_bounded_reconstruction_ack_before_any_prices(pinned,monkeypatch,start,end):
    root,intent=pinned
    intent.update(requested_start=start,requested_end=end)
    (root/'intent.json').write_text(json.dumps(intent),encoding='utf-8')
    monkeypatch.setattr(pd,'read_parquet',lambda *a,**k:pytest.fail('numerical values must not be read'))
    with pytest.raises(ValueError,match='acknowledgement'):
        freeze(root)
    assert not (root/'study').exists()


def test_out_of_scope_observations_cannot_move_the_decision_calendar():
    p,r,days=market()
    past=p.loc[p.date.le(days[22])].copy()
    before=selection(past,r,days[23])
    foreign=p.loc[p.date.eq(days[23])].copy()
    foreign['symbol']='foreign_'+foreign.symbol
    after=selection(pd.concat([past,foreign],ignore_index=True),r,days[23])
    assert after['decision_date']==before['decision_date']
    pd.testing.assert_frame_equal(after['selected'],before['selected'])


@pytest.mark.parametrize('field,value',[('price_unit','Toman'),('price_unit',None),
    ('ins_code','999999'),('ins_code',True),('ins_code',25.0)])
def test_explicit_units_and_native_identity_must_match_before_selection(field,value):
    p,r,days=market()
    p['price_unit']='Rial'
    p['ins_code']=p.symbol.map({x['symbol_fa']:x['ins_code'] for x in r}).astype(object)
    p.loc[p.symbol.eq('y')&p.date.eq(days[20]),field]=value
    with pytest.raises(ValueError,match='Rial|identity'):
        selection(p,r,days[22])


def test_future_identity_and_units_cannot_change_the_past_selection():
    p,r,days=market()
    before=selection(p,r,days[22])
    p['price_unit']='Rial'
    p['ins_code']=p.symbol.map({x['symbol_fa']:x['ins_code'] for x in r})
    p.loc[p.date.gt(days[22]),'price_unit']='Toman'
    p.loc[p.date.gt(days[22]),'ins_code']='wrong'
    after=selection(p,r,days[22])
    pd.testing.assert_frame_equal(before['selected'],after['selected'])


def test_out_of_scope_future_session_cannot_move_the_terminal_date(pinned):
    root,intent=pinned
    p=pd.read_parquet(root/'prices.parquet')
    terminal=p.date.max()
    foreign=p.loc[p.date.eq(terminal)].copy()
    foreign['symbol']='foreign_'+foreign.symbol
    foreign['date']='2024-01-28'
    pd.concat([p,foreign],ignore_index=True).to_parquet(root/'prices.parquet',index=False)
    intent['requested_end']='2024-01-28'
    (root/'intent.json').write_text(json.dumps(intent),encoding='utf-8')
    freeze(root)
    result=evaluate(root/'study')
    assert result['terminal_session']==terminal
    assert result['scenarios']['normal']['net_model_return'] is not None


@pytest.mark.parametrize('corrupt',['basket-curve','control-curve','control-executions','control-decisions'])
def test_review_checks_the_actual_artifact_not_just_its_self_consistent_hash(pinned,monkeypatch,corrupt):
    """A faulty writer can archive an incorrect frame with a perfectly valid hash."""
    root,_=pinned
    freeze(root)
    writer=pd.DataFrame.to_parquet
    target={'basket-curve':'normal-basket-curve.parquet','control-curve':'1-normal-curve.parquet',
            'control-executions':'1-normal-executions.parquet','control-decisions':'1-normal-decisions.parquet'}[corrupt]
    changed=[]
    def faulty_writer(frame,path,*args,**kwargs):
        if path.name==target:
            frame=frame.copy()
            if corrupt.endswith('curve'):
                frame.loc[frame.index[-1],'equity_rial']+=1000.
            elif corrupt=='control-executions':
                frame.loc[frame.index[0],'quantity']+=1
            else:
                frame=pd.DataFrame([dict(status='invented_decision')])
            changed.append(True)
        return writer(frame,path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(pd.DataFrame,'to_parquet',faulty_writer)
        evaluate(root/'study')
    assert changed
    with pytest.raises(AssertionError):
        review(root/'study')


@pytest.mark.parametrize('field',['ref_price_raw','close_final_raw'])
def test_boolean_numeric_execution_fields_remain_unknown_instead_of_one(field):
    p,_,days=market(8)
    own=p.loc[p.symbol.eq('a')].copy()
    own[field]=True
    result,*_=case(own,'a',days)
    assert result['net_model_return'] is None


def test_nullable_unknown_unit_cannot_disappear_from_an_all_check():
    p,r,days=market()
    p['price_unit']=pd.Series('Rial',index=p.index,dtype='string')
    p.loc[p.symbol.eq('y')&p.date.eq(days[20]),'price_unit']=pd.NA
    with pytest.raises(ValueError,match='Rial'):
        selection(p,r,days[22])


def test_outcome_metadata_is_checked_only_when_the_outcome_phase_starts(pinned):
    root,_=pinned
    p=pd.read_parquet(root/'prices.parquet')
    p['price_unit']='Rial'
    p.loc[p.symbol.eq('y')&p.date.eq(p.date.max()),'price_unit']='Toman'
    p.to_parquet(root/'prices.parquet',index=False)
    chosen=freeze(root)
    assert chosen['outcomes_read'] is False
    with pytest.raises(ValueError,match='Rial'):
        evaluate(root/'study')


def test_direct_sleeve_rejects_explicit_wrong_units():
    p,_,days=market(8)
    p['price_unit']='Toman'
    with pytest.raises(ValueError,match='Rial'):
        case(p,'a',days)
