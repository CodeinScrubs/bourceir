"""Synthetic inputs stay in tests; the audit does not create new fills or returns."""

from dataclasses import replace

import pandas as pd
import pytest

from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from research.corporate_exposure import trace_synthetic_exposures
from tests.test_portfolio import config, history, picks


def case(*,actions=True,stress=False,missing=False):
    prices,days = history(10,('a',))
    if actions:
        # A held position crosses a reference change on its due exit day.
        # A later reference change affects only the retained noncash fraction.
        for start,price in ((2,60.),(5,30.)):
            prices.loc[prices.date.ge(days[start]),[
                'open_raw','low_raw','high_raw','close_raw','close_final_raw','ref_price_raw']] = price
            prices.loc[prices.date.ge(days[start]),'value'] = price*1000
    if missing:
        prices = prices.loc[prices.date.ne(days[6])]
    prepared = prepare_portfolio_prices(prices)
    replay = replay_portfolio(prepared,picks(days[:1],('a',)),days,
        config(max_positions=1,horizon=1,stress=stress))
    return prepared,replay


def audit(prepared,replay):
    fees = replay.assumptions['fees']
    return trace_synthetic_exposures(prepared,replay.executions,replay.equity,replay.metrics,
        buy_fee=fees['buy'],sell_fee=fees['sell'],accounting_model=replay.assumptions['corporate_action_model'])


@pytest.mark.parametrize('actions',[False,True])
@pytest.mark.parametrize('stress',[False,True])
@pytest.mark.parametrize('missing',[False,True])
def test_every_cash_holding_and_fractional_mark_matches_the_saved_model(actions,stress,missing):
    prepared,replay = case(actions=actions,stress=stress,missing=missing)
    saved = replay.executions.copy(deep=True)
    out = audit(prepared,replay)
    assert out.summary['sessions_reconciled']==10
    assert out.summary['cash_holdings_marks_and_claims_reconciled']
    assert out.summary['action_occurrences']==replay.metrics['corporate_action_events']
    assert len(out.actions)==(2 if actions else 0)
    if actions:
        assert out.actions.fraction_only.tolist()==[False,True]
        assert out.actions.whole_before.iloc[0]>0 and out.actions.fraction_created.iloc[0]>0
        assert out.actions.reference_ratio.tolist()==[.6,.5]
        assert out.actions.actual_entitlement_verified.eq(False).all()
    assert not out.summary['economic_profit_verified']
    pd.testing.assert_frame_equal(saved,replay.executions,check_exact=True)


@pytest.mark.parametrize('change',['nav','cash','positions','stale_whole','stale_fraction','receivable','fee','quantity','claim','action_count'])
def test_mutated_financial_evidence_cannot_make_a_reconciled_census(change):
    prepared,replay = case()
    replay = replace(replay,equity=replay.equity.copy(deep=True),executions=replay.executions.copy(deep=True),
        metrics=dict(replay.metrics))
    if change in ('nav','cash','positions','stale_whole','stale_fraction','receivable'):
        field = {'nav':'equity_rial','cash':'cash_rial','positions':'positions','stale_whole':'stale_positions',
            'stale_fraction':'stale_fractional_exposures','receivable':'noncash_receivable_rial'}[change]
        replay.equity.loc[3,field] += 1
    elif change=='fee':
        replay.executions.loc[0,'fee_rial'] += 1
    elif change=='quantity':
        replay.executions.loc[0,'quantity'] += 1
    elif change=='claim':
        replay.metrics['fractional_exposures'] = []
    else:
        replay.metrics['corporate_action_events'] += 1
    with pytest.raises(ValueError):
        audit(prepared,replay)


def test_missing_mark_is_not_replaced_by_a_current_execution_or_another_issuer():
    prepared,replay = case()
    prices,days = history(10,('a',))
    prices.loc[prices.date.eq(days[2]),'ref_price_raw'] = None
    with pytest.raises(ValueError):
        audit(prepare_portfolio_prices(prices),replay)


@pytest.mark.parametrize('model',['cash_dividend_entitlement','bonus_share_entitlement',None])
def test_explicit_rights_books_cannot_be_reinterpreted_as_synthetic_reference_rights(model):
    prepared,replay = case()
    replay.assumptions = dict(replay.assumptions,corporate_action_model=model)
    with pytest.raises(ValueError):
        audit(prepared,replay)
