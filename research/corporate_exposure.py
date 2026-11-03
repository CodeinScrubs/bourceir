"""Reconcile an existing synthetic portfolio and locate its evidence requirements.

This is a ledger reconstruction, not a selection rule, entitlement inference, or
counterfactual return. It never changes an execution or replaces an unknown mark.
"""

import math
from dataclasses import dataclass

import pandas as pd

from backtest.prepared import PRICE_SNAPSHOT_VERSION, PreparedPortfolioPrices
from core.risk import finite_number
from core.timeutils import is_canonical_iso_date

VERSION = 'synthetic-corporate-exposure-census-v1'
ACTION_COLUMNS = ['date','symbol','previous_price_date','previous_official_rial',
    'reference_rial','official_mark_rial','reference_ratio','reference_gap_rial',
    'whole_before','whole_after','fraction_before','fraction_after','fraction_created',
    'fraction_cost_allocated_rial','modeled_post_action_exposure_rial','fraction_only',
    'one_rial_reference_change','actual_entitlement_verified']


@dataclass
class ExposureCensus:
    actions: pd.DataFrame
    summary: dict


def _near(actual,expected,label):
    if not math.isfinite(actual) or not math.isfinite(expected) or not math.isclose(
            actual,expected,rel_tol=1e-11,abs_tol=1e-3):
        raise ValueError('Synthetic ledger does not reconcile: '+label)


def trace_synthetic_exposures(prepared,executions,equity,metrics,*,buy_fee,sell_fee,accounting_model):
    """Reconstruct only complete, single-lot synthetic-reference portfolios.

    Actual corporate-action books have different rights and cash timing and must
    not be supplied here. The caller verifies that accounting model and freezes
    original bytes. Quantities/costs describe the saved synthetic model only.
    """
    if (accounting_model!='synthetic_whole_shares_plus_noncash_fraction'
            or not isinstance(prepared,PreparedPortfolioPrices) or prepared.version!=PRICE_SNAPSHOT_VERSION
            or not isinstance(equity,pd.DataFrame) or equity.empty
            or equity.columns.duplicated().any()
            or not {'date','equity_rial','cash_rial','positions','stale_positions',
                    'stale_fractional_exposures','noncash_receivable_rial'}<=set(equity)
            or equity.date.duplicated().any() or not equity.date.is_monotonic_increasing
            or not all(is_canonical_iso_date(d) for d in equity.date)
            or metrics.get('status')!='complete'):
        raise ValueError('A complete synthetic portfolio and immutable price source are required')
    buy_fee,sell_fee = finite_number(buy_fee),finite_number(sell_fee)
    initial = finite_number(metrics['initial_capital_rial'])
    if not (0<=buy_fee<1 and 0<=sell_fee<1 and initial>0):
        raise ValueError('Explicit finite fees and positive initial capital are required')
    if (not isinstance(executions,pd.DataFrame) or executions.columns.duplicated().any()
            or (not executions.empty and (not {'date','symbol','side','quantity','price_rial','fee_rial'}<=set(executions)
                or not set(executions.date)<=set(equity.date)))):
        raise ValueError('Executions must belong to the preserved curve')
    trades = {} if executions.empty else {
        d:g.to_dict('records') for d,g in executions.groupby('date',sort=False)}
    positions,fractions,actions = {},{},[]
    cash,total_fees = initial,0.
    for curve in equity.to_dict('records'):
        day = curve['date']
        session = prepared.sessions.get(day,{})
        for symbol in sorted(set(positions)|set(fractions)):
            row = session.get(symbol)
            if row is None:
                continue
            factor = row['action']
            if not row['mark_valid'] or not math.isfinite(factor) or factor<=0:
                raise ValueError('Unknown held mark/reference in a supposedly complete curve')
            pos,claim = positions.get(symbol),fractions.get(symbol)
            if abs(factor-1)>1e-6:
                before_whole = 0 if pos is None else pos['quantity']
                before_fraction = 0. if claim is None else claim['quantity']
                if claim is not None:
                    claim['quantity'] /= factor
                created,allocated = 0.,0.
                if pos is not None:
                    exact = pos['quantity']/factor
                    whole = math.floor(exact+1e-9)
                    created = max(0.,exact-whole)
                    allocated = pos['cost']*created/exact
                    if created>0:
                        claim = fractions.setdefault(symbol,dict(quantity=0.,cost=0.,mark=None,mark_date=None))
                        claim['quantity'] += created
                        claim['cost'] += allocated
                    pos['cost'] -= allocated
                    pos['quantity'] = whole
                    if whole==0:
                        del positions[symbol]
                        pos = None
                after_whole = 0 if pos is None else pos['quantity']
                after_fraction = 0. if claim is None else claim['quantity']
                gap = row['ref_price_raw']-row['previous_official']
                actions.append(dict(date=day,symbol=symbol,previous_price_date=row['previous_date'],
                    previous_official_rial=row['previous_official'],reference_rial=row['ref_price_raw'],
                    official_mark_rial=row['mark'],reference_ratio=factor,reference_gap_rial=gap,
                    whole_before=before_whole,whole_after=after_whole,fraction_before=before_fraction,
                    fraction_after=after_fraction,fraction_created=created,
                    fraction_cost_allocated_rial=allocated,
                    modeled_post_action_exposure_rial=(after_whole+after_fraction)*row['mark']*(1-sell_fee),
                    fraction_only=before_whole==0,one_rial_reference_change=abs(gap)<=1,
                    actual_entitlement_verified=False))
            if claim is not None:
                claim.update(mark=row['mark'],mark_date=day)
            if pos is not None:
                pos.update(mark=row['mark'],mark_date=day)
        for trade in trades.get(day,[]):
            symbol,side = trade['symbol'],trade['side']
            quantity,price,fee = (finite_number(trade[k]) for k in ('quantity','price_rial','fee_rial'))
            if side not in ('buy','sell') or quantity<=0 or not quantity.is_integer() or price<=0 or fee<0:
                raise ValueError('Unknown execution side or invalid whole-share execution')
            _near(fee,quantity*price*(buy_fee if side=='buy' else sell_fee),'execution fee')
            row = session.get(symbol)
            if row is None or not row['valid'] or not row['low_raw']<=price<=row['high_raw']:
                raise ValueError('Execution lacks observed execution data')
            total_fees += fee
            if side=='buy':
                if symbol in positions:
                    raise ValueError('Multiple credited lots require an explicit corporate-action book')
                cash -= quantity*price+fee
                positions[symbol] = dict(quantity=quantity,cost=quantity*price+fee,
                    mark=row['mark'],mark_date=day)
            else:
                pos = positions.get(symbol)
                if pos is None or quantity>pos['quantity']:
                    raise ValueError('Sale exceeds the reconstructed whole-share holding')
                sold_cost = pos['cost']*quantity/pos['quantity']
                if trade.get('realized_net_rial') is not None:
                    _near(trade['realized_net_rial'],quantity*price-fee-sold_cost,'realized net')
                cash += quantity*price-fee
                pos['cost'] -= sold_cost
                pos['quantity'] -= quantity
                if pos['quantity']==0:
                    del positions[symbol]
        claim_value = sum(c['quantity']*c['mark']*(1-sell_fee) for c in fractions.values())
        whole_value = sum(p['quantity']*p['mark']*(1-sell_fee) for p in positions.values())
        for actual,expected,label in (
            (cash,curve['cash_rial'],'cash'),(cash+whole_value+claim_value,curve['equity_rial'],'NAV'),
            (claim_value,curve['noncash_receivable_rial'],'retained noncash valuation')):
            _near(actual,expected,label+' at '+day)
        if (len(positions)!=curve['positions']
                or sum(p['mark_date']!=day for p in positions.values())!=curve['stale_positions']
                or sum(c['mark_date']!=day for c in fractions.values())!=curve['stale_fractional_exposures']):
            raise ValueError('Whole/fraction counts or mark ages do not reconcile at '+day)
    _near(total_fees,metrics['fees_rial'],'total fees')
    if len(actions)!=metrics['corporate_action_events'] or len(executions)!=metrics['executions']:
        raise ValueError('Event/execution denominator differs from the preserved engine')
    expected = {c['symbol']:c for c in metrics['fractional_exposures']}
    if set(expected)!=set(fractions) or len(positions)!=metrics['open_positions']:
        raise ValueError('Final holdings/claim universe differs')
    for symbol,claim in fractions.items():
        for field,source in (('quantity','quantity'),('cost','cost_rial'),('mark','mark')):
            _near(claim[field],expected[symbol][source],symbol+' final '+field)
        if claim['mark_date']!=expected[symbol]['mark_date']:
            raise ValueError('Final fractional mark date differs')
    frame = pd.DataFrame(actions,columns=ACTION_COLUMNS)
    return ExposureCensus(frame,dict(version=VERSION,sessions_reconciled=len(equity),
        cash_holdings_marks_and_claims_reconciled=True,action_occurrences=len(actions),
        fraction_only_occurrences=int(frame.fraction_only.sum()),
        one_rial_reference_occurrences=int(frame.one_rial_reference_change.sum()),
        original_executions_unchanged=True,new_financial_scenarios=0,
        actual_corporate_entitlements_verified=False,source_correctness_verified=False,
        accepted_for_signals=False,economic_profit_verified=False))
