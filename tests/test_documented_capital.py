from copy import deepcopy
from dataclasses import replace
from fractions import Fraction

import pytest

from backtest.cash_claim_evidence import audit_unpaid_cash_claim_replay
from backtest.corporate_actions import DatedCashDividendBook
from backtest.documented_capital import DocumentedCapitalBook, bind_capital_event
from backtest.portfolio import replay_portfolio
from tests.test_no_trade_valuation import publication, receipt
from tests.test_portfolio import config, history, picks

CODE='12345678901234567'


def scenario(*, cash=False, market_reference=None):
    frame,days=history(8,(CODE,))
    mark=market_reference if market_reference is not None else (55 if cash else 50)
    after=frame.date.ge(days[2])
    for name in ('open_raw','close_raw','close_final_raw','ref_price_raw'):
        frame.loc[after,name]=float(mark)
    frame.loc[after,'low_raw']=float(mark-1)
    frame.loc[after,'high_raw']=float(mark+1)
    frame.loc[after,'value']=float(mark*1000)
    frame=frame.loc[frame.date.ne(days[2])].copy()
    obs=publication(receipt(day=days[2],native_changes={'pClosing':mark,'priceYesterday':mark}))
    obs['http_status']=200
    event=bind_capital_event(observation=obs,event_id='CAP1',symbol=CODE,
        record_date=days[1],ex_date=days[2],previous_price_session=days[1],
        previous_official_rial=100,observed_reference_rial=mark,
        capital_before_rial=1_000_000,capital_after_rial=2_000_000,
        cash_and_claims_rial=1_000_000 if cash else 0,
        retained_earnings_rial=0 if cash else 1_000_000,revaluation_rial=0,
        subscription_price_rial=10 if cash else 0,document_sha256='a'*64)
    return frame,days,obs,event


def execute(*,cash=False,market_reference=None):
    frame,days,obs,event=scenario(cash=cash,market_reference=market_reference)
    tape=picks(days[:1],(CODE,));book=DocumentedCapitalBook([event],
        dividends=DatedCashDividendBook([],previous_price_sessions={}))
    result=replay_portfolio(frame,tape,days,config(max_positions=1),documented_capital=book,
        no_trade_entry_observations=[obs],no_trade_valuation_observations=[obs])
    return frame,days,tape,book,result


@pytest.mark.parametrize('cash',[False,True])
def test_only_original_shares_trade_and_pending_rights_do_not_create_cash(cash):
    frame,days,tape,book,result=execute(cash=cash)
    buys=result.executions.loc[result.executions.side.eq('buy')]
    sells=result.executions.loc[result.executions.side.eq('sell')]
    assert buys.quantity.sum()==sells.quantity.sum()==49
    claim=result.metrics['capital_claims'][0]
    expected=claim['pending_rights_units' if cash else 'pending_bonus_units']
    assert Fraction(expected['numerator'],expected['denominator'])==49
    assert result.metrics['new_saleable_capital_shares']==result.metrics['external_cash_rial']==0
    assert result.metrics['total_return'] is None and result.metrics['final_equity_rial'] is None
    assert result.equity.loc[result.equity.date.ge(days[2]),'equity_rial'].isna().all()
    audit=audit_unpaid_cash_claim_replay(result,frame,tape,days,days)
    assert not audit['invariant_violations'] and audit['cash_component_return']<0
    assert audit['total_return'] is None and audit['stock_inventory_liquidated']
    assert book.has_unvalued_claims


def test_observed_reference_does_not_determine_legal_bonus_ratio_or_explain_rounding():
    _,_,_,_,result=execute(market_reference=49)
    claim=result.metrics['capital_claims'][0]
    assert claim['pending_bonus_units']=={'numerator':49,'denominator':1}
    transition=result.metrics['capital_claim_ledger'][0]
    assert transition['reference_residual_rial']=={'numerator':-1,'denominator':1}
    assert transition['rounding_rule_verified'] is False


def test_legacy_model_still_rejects_unexplained_no_trade_reference_change():
    frame,days,obs,_=scenario()
    legacy=replay_portfolio(frame,picks(days[:1],(CODE,)),days,config(max_positions=1),
        no_trade_entry_observations=[obs],no_trade_valuation_observations=[obs])
    assert legacy.metrics['total_return'] is None
    assert 'discontinuous_no_trade_valuation' in set(legacy.decisions.status)


@pytest.mark.parametrize('field,value',[('capital_after_rial',2_000_001),
    ('cash_and_claims_rial',True),('native_sha256','invalid'),('record_date','2024-01-03')])
def test_rejects_incomplete_funding_or_ambiguous_proof(field,value):
    _,_,_,event=scenario()
    with pytest.raises(ValueError):replace(event,**{field:value})


def test_failed_capital_transition_rolls_back_claim_creation_and_clocks():
    _,days,_,event=scenario()
    book=DocumentedCapitalBook([event],dividends=DatedCashDividendBook([],previous_price_sessions={}))
    book.start(list(days))
    book.begin_day(days[0],{},{});book.end_day(days[0],{})
    positions={'lot':dict(symbol=CODE,quantity=10,cost=1000.)}
    book.begin_day(days[1],{},positions);book.end_day(days[1],positions)
    with pytest.raises(ValueError):book.begin_day(days[2],{CODE:dict(previous_date=days[1],previous_official=99,ref_price_raw=50)},positions)
    assert not book.claims and not book.ledger and book.last_begin==book.last_end==days[1]
    assert book.eligible=={'CAP1':10}


@pytest.mark.parametrize('mode', ['units','cash','credit'])
def test_independent_audit_rejects_tampered_capital_claims(mode):
    frame,days,tape,_,result=execute(cash=True)
    result=deepcopy(result)
    if mode=='units':result.metrics['capital_claims'][0]['pending_rights_units']['numerator']+=1
    elif mode=='cash':result.metrics['rights_sale_income_rial']=1
    else:result.metrics['capital_claims'][0]['holder_credit_date']=days[3]
    audit=audit_unpaid_cash_claim_replay(result,frame,tape,days,days)
    assert audit['invariant_violations'] and audit['cash_component_return'] is None


def test_mixed_cash_and_reserves_keep_both_legal_ratios_and_fractions():
    frame,days,obs,event=scenario()
    event=replace(event,cash_and_claims_rial=500_000,retained_earnings_rial=0,
                  other_reserves_rial=500_000,subscription_price_rial=10)
    book=DocumentedCapitalBook([event],dividends=DatedCashDividendBook([],previous_price_sessions={}))
    tape=picks(days[:1],(CODE,))
    result=replay_portfolio(frame,tape,days,config(max_positions=1),documented_capital=book,
        no_trade_entry_observations=[obs],no_trade_valuation_observations=[obs])
    claim=result.metrics['capital_claims'][0]
    assert claim['pending_bonus_units']==claim['pending_rights_units']=={'numerator':49,'denominator':2}
    assert not audit_unpaid_cash_claim_replay(result,frame,tape,days,days)['invariant_violations']


def test_failed_pre_valuation_day_does_not_require_uncommitted_capital_claim():
    frame,days,obs,event=scenario()
    book=DocumentedCapitalBook([event],dividends=DatedCashDividendBook([],previous_price_sessions={}))
    tape=picks(days[:1],(CODE,))
    # Without a native mark the engine stops before creating today's claims.
    result=replay_portfolio(frame,tape,days,config(max_positions=1),documented_capital=book,
        no_trade_entry_observations=[],no_trade_valuation_observations=[])
    assert result.metrics['status']=='invalid_data' and result.metrics['capital_claims']==[]
    audit=audit_unpaid_cash_claim_replay(result,frame,tape,days,days)
    assert not audit['invariant_violations']
    assert audit['uncommitted_terminal_capital_events']==['CAP1']
    assert audit['cash_component_return'] is None and not audit['full_execution_window_observed']
