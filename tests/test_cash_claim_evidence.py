from copy import deepcopy
from decimal import Decimal

import pytest

from backtest.cash_claim_evidence import audit_unpaid_cash_claim_replay
from backtest.cash_dividends import CashDividendBook
from backtest.portfolio import replay_portfolio
from tests.test_cash_dividend_portfolio import cash_history
from tests.test_portfolio import config, picks


def case():
    frame, days, event = cash_history()
    tape=picks(days[:1], ('a',))
    result=replay_portfolio(frame,tape,days,config(max_positions=1),cash_dividends=CashDividendBook([event]))
    return frame,days,tape,result


def test_unpaid_dividend_is_a_claim_and_cannot_become_cash_or_total_return():
    frame,days,tape,result=case()
    audit=audit_unpaid_cash_claim_replay(result,frame,tape,days,days)
    assert not audit['invariant_violations'] and audit['stock_inventory_liquidated']
    assert audit['cash_component_rial']==pytest.approx(result.equity.iloc[-1].cash_rial)
    assert Decimal(audit['outstanding_dividend_face_rial'])==490
    assert audit['total_NAV_rial'] is None and audit['total_return'] is None


@pytest.mark.parametrize('mode,expected', [
    ('cash','cash_not_reconciled'),('shares','unfunded_or_synthetic_share_sale'),
    ('claim','claim_not_equal_to_record_date_inventory'),('total','unpaid_claim_silently_valued_in_total_nav'),
    ('fee','execution_fee_mismatch')])
def test_detects_false_cash_shares_claims_returns_and_fees(mode,expected):
    frame,days,tape,result=case();result=deepcopy(result)
    if mode=='cash':result.equity.loc[result.equity.index[-1],'cash_rial']+=1
    elif mode=='shares':result.executions.loc[result.executions.side.eq('sell'),'quantity']+=1
    elif mode=='claim':result.metrics['cash_dividend_claims'][0]['entitled_quantity']+=1
    elif mode=='total':result.metrics['total_return']=.2
    else:result.executions.loc[result.executions.index[0],'fee_rial']+=1
    audit=audit_unpaid_cash_claim_replay(result,frame,tape,days,days)
    assert expected in audit['invariant_violations'] and audit['cash_component_return'] is None


def test_rejects_unimplemented_payment_and_synthetic_contracts():
    frame,days,tape,result=case()
    result.assumptions['source_payments']=[dict(payment_id='not-supported')]
    with pytest.raises(ValueError):audit_unpaid_cash_claim_replay(result,frame,tape,days,days)


def test_incomplete_path_never_gets_a_whole_period_cash_return():
    frame,days,tape,result=case()
    result.equity=result.equity.iloc[:-1]
    result.metrics['status']='invalid_data'
    audit=audit_unpaid_cash_claim_replay(result,frame,tape,days,days)
    assert audit['cash_component_return'] is None
