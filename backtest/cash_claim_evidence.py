"""Independent fixed-share cash/claim checks; no synthetic share substitution."""
import math
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from backtest.prepared import PreparedPortfolioPrices, prepare_portfolio_prices

VERSION = 'fixed-share-unpaid-claims-audit-v1'


def audit_unpaid_cash_claim_replay(result, prices, selections, calendar, sessions):
    """Audit cash separately from outstanding dividends, with no payment inputs.

    Only fully observed, fully liquidated stock paths can have a cash-component
    return. That return excludes unpaid claims and is never the total NAV return.
    This narrow auditor rejects payment/share-credit contracts it cannot check.
    """
    a, m = result.assumptions, result.metrics
    if (a.get('corporate_action_model') not in ('explicit_cash_dividend_fixed_shares_unvalued_receivables',
                'documented_capital_fixed_original_shares_and_unpaid_claims')
            or a.get('source_payments') or m.get('cash_dividend_income_rial') != 0):
        raise ValueError('Only the explicit fixed-share, no-payment contract is supported')
    days, cal = list(sessions), list(calendar)
    if not days or days != sorted(set(days)) or cal != sorted(set(cal)) or not set(days) <= set(cal):
        raise ValueError('Declared sorted session windows are required')
    prepared = prices if isinstance(prices, PreparedPortfolioPrices) else prepare_portfolio_prices(prices)
    curve, trades = result.equity, result.executions
    if curve.empty or curve.date.tolist() != days[:len(curve)]:
        raise ValueError('The observed curve must be a nonempty prefix of the declared window')
    cfg, fees = a['config'], a['fees']
    violations = []
    positions = {day:i for i,day in enumerate(cal)}
    decisions = set(selections[['date', 'symbol']].itertuples(index=False, name=None))
    by_day = defaultdict(list)
    for trade in trades.to_dict('records'):
        by_day[trade['date']].append(trade)
        row = prepared.sessions.get(trade['date'], {}).get(trade['symbol'])
        q, p, f = trade['quantity'], trade['price_rial'], trade['fee_rial']
        if (trade['side'] not in ('buy', 'sell') or any(not math.isfinite(v) for v in (q,p,f))
                or q <= 0 or q != int(q) or p <= 0 or f < 0):
            violations.append('invalid_execution')
            continue
        if row is None or not row['low_raw'] <= p <= row['high_raw']:
            violations.append('execution_outside_observed_bar')
        if not math.isclose(f, q*p*fees[trade['side']], rel_tol=1e-10, abs_tol=.01):
            violations.append('execution_fee_mismatch')
        i = positions.get(trade['date'], 0)
        if trade['side']=='buy' and (i==0 or (cal[i-1], trade['symbol']) not in decisions):
            violations.append('entry_without_prior_session_decision')
    cash, fee_total = float(cfg['initial_capital_rial']), 0.
    inventory, snapshots = defaultdict(int), {}
    source_events = a['source_events']
    capital_events = a.get('source_capital_events', [])
    for mark in curve.to_dict('records'):
        day = mark['date']
        own = by_day.get(day, [])
        purchases = sum(t['quantity']*t['price_rial']+t['fee_rial'] for t in own if t['side']=='buy')
        if purchases > cash+.01:
            violations.append('same_day_sale_cash_reused_or_external_funding')
        for trade in own:
            sign = 1 if trade['side']=='buy' else -1
            inventory[trade['symbol']] += sign*trade['quantity']
            cash -= sign*trade['quantity']*trade['price_rial']+trade['fee_rial']
            fee_total += trade['fee_rial']
            if inventory[trade['symbol']] < 0:
                violations.append('unfunded_or_synthetic_share_sale')
        if not math.isclose(cash, mark['cash_rial'], rel_tol=1e-10, abs_tol=.01) or cash < -.01:
            violations.append('cash_not_reconciled')
        if sum(q>0 for q in inventory.values()) != mark['positions']:
            violations.append('fixed_share_position_count_mismatch')
        for symbol in {t['symbol'] for t in own}:
            row = prepared.sessions.get(day, {}).get(symbol)
            selected = [t for t in own if t['symbol']==symbol]
            if row is not None and (sum(t['quantity'] for t in selected)>row['volume']*cfg['participation']+1e-6
                    or sum(t['quantity']*t['price_rial'] for t in selected)>row['value']*cfg['participation']+.01):
                violations.append('daily_capacity_exceeded')
        for event in source_events+capital_events:
            i = bisect_right(cal, event['record_date'])-1
            if i>=0 and cal[i]==day:
                snapshots[event['event_id']] = inventory[event['symbol']]
    claims = {c['event_id']:c for c in m['cash_dividend_claims']}
    expected_face = Decimal(0)
    for event in source_events:
        if event['ex_date'] > curve.date.iloc[-1]:
            continue
        quantity = snapshots.get(event['event_id'], 0)
        claim = claims.get(event['event_id'])
        if quantity:
            expected_face += Decimal(str(quantity))*Decimal(event['dps_rial'])
            if claim is None or claim['entitled_quantity'] != quantity or Decimal(claim['paid_per_share_rial']) != 0:
                violations.append('claim_not_equal_to_record_date_inventory')
        elif claim is not None:
            violations.append('claim_without_record_date_ownership')
    if not math.isclose(float(expected_face), m['cash_dividend_receivable_face_rial'], rel_tol=1e-10, abs_tol=.01):
        violations.append('outstanding_claim_face_not_reconciled')
    capital_claims = m.get('capital_claims', [])
    uncommitted_terminal_events = []
    if len({c['event_id'] for c in capital_claims}) != len(capital_claims):
        violations.append('duplicate_capital_claim')
    by_id = {c['event_id']: c for c in capital_claims}
    known_events = {e['event_id'] for e in capital_events}
    if set(by_id)-known_events:
        violations.append('capital_claim_without_documented_event')
    for event in capital_events:
        if event['ex_date'] > curve.date.iloc[-1]:
            continue
        quantity = snapshots.get(event['event_id'], 0)
        claim = by_id.get(event['event_id'])
        if (event['ex_date'] == curve.date.iloc[-1] and m['status'] == 'invalid_data'
                and claim is None):
            # Valuation can reject another issuer before begin_day creates
            # any of today's claims. A failed partial day is not evidence of
            # an omitted committed claim; it still cannot yield a cash return.
            uncommitted_terminal_events.append(event['event_id'])
            continue
        if not quantity:
            if claim is not None:
                violations.append('capital_claim_without_record_date_ownership')
            continue
        bonus = Fraction(int(quantity)*(event['retained_earnings_rial']+event['revaluation_rial']+event.get('other_reserves_rial',0)),
                         event['capital_before_rial'])
        rights = Fraction(int(quantity)*event['cash_and_claims_rial'], event['capital_before_rial'])
        try:
            actual_bonus = Fraction(**claim['pending_bonus_units']) if claim else None
            actual_rights = Fraction(**claim['pending_rights_units']) if claim else None
        except (ValueError, TypeError, KeyError, ZeroDivisionError):
            actual_bonus = actual_rights = None
        if (claim is None or claim.get('eligible_original_units') != quantity
                or actual_bonus != bonus or actual_rights != rights):
            violations.append('documented_capital_units_not_reconciled')
        if claim is not None and any(claim.get(k) is not None for k in
                ('holder_credit_date', 'rights_available_date', 'rights_sale_proceeds_rial', 'fair_value_rial')):
            violations.append('unsupported_credit_sale_or_claim_valuation')
    if (capital_events and (m.get('new_saleable_capital_shares') != 0
            or m.get('rights_sale_income_rial') != 0 or m.get('external_cash_rial') != 0)):
        violations.append('unsupported_capital_cash_or_saleable_credit')
    unvalued_capital=bool(m.get('capital_claims_unvalued'))
    if (expected_face>0 or unvalued_capital) and (m.get('total_return') is not None or m.get('final_equity_rial') is not None):
        violations.append('unpaid_claim_silently_valued_in_total_nav')
    if not math.isclose(fee_total, m['fees_rial'], rel_tol=1e-10, abs_tol=.01):
        violations.append('total_fees_not_reconciled')
    complete = (curve.date.tolist()==days and m['status'] in ('complete','unvalued_receivable_path','unvalued_documented_capital_path')
                and not violations)
    liquidated = complete and all(q==0 for q in inventory.values())
    return dict(version=VERSION, invariant_violations=sorted(set(violations)),
        full_execution_window_observed=complete, stock_inventory_liquidated=liquidated,
        cash_component_rial=cash if liquidated else None,
        cash_component_return=cash/cfg['initial_capital_rial']-1 if liquidated else None,
        outstanding_dividend_face_rial=str(expected_face), claim_fair_value_rial=None,
        uncommitted_terminal_capital_events=uncommitted_terminal_events,
        total_NAV_rial=None if expected_face>0 or unvalued_capital else (cash if liquidated else None),
        total_return=None if expected_face>0 or unvalued_capital else (cash/cfg['initial_capital_rial']-1 if liquidated else None),
        external_cash_rial=0, synthetic_share_adjustments=0, fees_reconciled_rial=fee_total,
        historical_entitlement_independently_verified=False, actual_fills_verified=False,
        accepted_for_signals=False, economic_profit_verified=False)
