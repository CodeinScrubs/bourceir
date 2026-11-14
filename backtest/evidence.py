"""Independent execution checks for the daily, synthetic-accounting replay contract.

An internally consistent replay is still a model. In particular, a carried mark
or a synthetic reference adjustment does not establish an economic profit.
"""

import math

import numpy as np
import pandas as pd

from backtest.ledger_audit import reconcile_synthetic_inventory
from backtest.prepared import PreparedPortfolioPrices, prepare_portfolio_prices

VERSION = 'daily-replay-evidence-v3'


def audit_daily_replay(result, prices, selections, sessions, *, no_trade_valuation_observations=None):
    """Check trades against independent raw bars and reconcile shared cash.

    This adapter deliberately supports the synthetic accounting path only. Cash
    dividends, subscription payments and share credits require their own ledger
    reconciliations; silently accepting them here would hide cash movements.
    Unknown/shortened curves remain in the denominator with no audited return.
    """
    if result.assumptions.get('corporate_action_model') != 'synthetic_whole_shares_plus_noncash_fraction':
        raise ValueError('This auditor requires the synthetic daily replay contract')
    prepared = prices if isinstance(prices, PreparedPortfolioPrices) else prepare_portfolio_prices(prices)
    days = list(sessions)
    if not days or days != sorted(set(days)):
        raise ValueError('Sorted unique expected sessions are required')
    config, fees = result.assumptions['config'], result.assumptions['fees']
    curve, trades = result.equity, result.executions
    required = {'date', 'equity_rial', 'cash_rial', 'positions', 'stale_positions', 'stale_fractional_exposures'}
    if not required <= set(curve) or curve.empty:
        raise ValueError('A nonempty daily curve with both stale-exposure counts is required')
    if not {'date', 'symbol'} <= set(selections):
        raise ValueError('Decision keys are required')
    if not trades.empty and not {'date', 'symbol', 'side', 'quantity', 'price_rial', 'fee_rial'} <= set(trades):
        raise ValueError('Execution fields are incomplete')
    violations, blockers = [], []
    actual_days = curve.date.tolist()
    if actual_days != days:
        blockers.append('incomplete_calendar')
        if actual_days != days[:len(actual_days)]:
            violations.append('curve_calendar_not_expected_prefix')
    if result.metrics['status'] != 'complete':
        blockers.append('engine_unknown_or_invalid')
    nav = pd.to_numeric(curve.equity_rial, errors='coerce')
    if not np.isfinite(nav).all():
        blockers.append('unknown_nav')
    stale = curve.stale_positions.gt(0) | curve.stale_fractional_exposures.gt(0)
    if stale.any():
        blockers.append('stale_valuation')
    if result.metrics.get('corporate_action_events', 0):
        blockers.append('synthetic_corporate_entitlements')
    if result.metrics.get('open_positions', 0) or result.metrics.get('noncash_receivable_rial', 0):
        blockers.append('terminal_assets_not_cash')
    daily = {day: [] for day in days}
    positions = {day: i for i, day in enumerate(days)}
    decisions = set(selections[['date', 'symbol']].itertuples(index=False, name=None))
    for event in trades.to_dict('records'):
        day, symbol, side = event['date'], event['symbol'], event['side']
        qty, price, fee = (event[key] for key in ('quantity', 'price_rial', 'fee_rial'))
        if (day not in positions or side not in {'buy', 'sell'} or any(
                isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.number)) or not math.isfinite(v)
                for v in (qty, price, fee)) or qty <= 0 or qty != math.floor(qty) or price <= 0 or fee < 0):
            violations.append('invalid_execution')
            continue
        row = prepared.sessions.get(day, {}).get(symbol)
        if row is None or not row['low_raw'] - 1e-7 <= price <= row['high_raw'] + 1e-7:
            violations.append('execution_outside_observed_range')
        if row is not None:
            if not row['valid']:
                violations.append('execution_from_unusable_raw_bar')
            if (side == 'buy' and row['up']) or (side == 'sell' and row['down']):
                violations.append('execution_in_assumed_locked_queue')
        if not math.isclose(fee, qty * price * fees[side], rel_tol=1e-10, abs_tol=1e-6):
            violations.append('execution_fee_mismatch')
        if side == 'buy' and (positions[day] == 0 or (days[positions[day]-1], symbol) not in decisions):
            violations.append('entry_without_previous_session_decision')
        daily[day].append(event)
    cash, total_fees = float(config['initial_capital_rial']), 0.
    for mark in curve.to_dict('records'):
        own = daily.get(mark['date'], [])
        purchase_cost = sum(e['quantity'] * e['price_rial'] + e['fee_rial'] for e in own if e['side'] == 'buy')
        if purchase_cost > cash + .01:
            violations.append('same_session_sale_proceeds_reused')
        cash += sum((-1 if e['side'] == 'buy' else 1) * e['quantity'] * e['price_rial'] - e['fee_rial'] for e in own)
        total_fees += sum(e['fee_rial'] for e in own)
        if (not np.isfinite(mark['cash_rial']) or mark['cash_rial'] < -1e-5
                or not math.isclose(mark['cash_rial'], cash, rel_tol=1e-10, abs_tol=.01)):
            violations.append('shared_cash_not_reconciled')
        if not 0 <= mark['positions'] <= config['max_positions']:
            violations.append('position_limit_exceeded')
        for symbol in {e['symbol'] for e in own}:
            row = prepared.sessions.get(mark['date'], {}).get(symbol)
            events = [e for e in own if e['symbol'] == symbol]
            if row is not None and (sum(e['quantity'] for e in events) > row['volume'] * config['participation'] + 1e-6
                    or sum(e['quantity'] * e['price_rial'] for e in events) > row['value'] * config['participation'] + .01):
                violations.append('shared_daily_participation_exceeded')
    if not math.isclose(result.metrics['fees_rial'], total_fees, rel_tol=1e-10, abs_tol=.01):
        violations.append('total_fees_not_reconciled')
    if result.metrics['executions'] != len(trades):
        violations.append('execution_count_mismatch')
    reported = result.metrics.get('total_return')
    if reported is not None and (not np.isfinite(reported) or not np.isfinite(nav.iloc[-1]) or not math.isclose(
            reported, nav.iloc[-1] / config['initial_capital_rial'] - 1, rel_tol=1e-10, abs_tol=1e-10)):
        violations.append('terminal_return_not_reconciled')
    inventory = reconcile_synthetic_inventory(result, prepared, days,
        no_trade_valuation_observations=no_trade_valuation_observations)
    violations.extend(inventory['violations'])
    blockers.extend(inventory['blockers'])
    # Preserve a known marked model return separately from liquidation and
    # economic claims. Any stale point also invalidates path-dependent metrics.
    model_blockers = {'incomplete_calendar', 'engine_unknown_or_invalid', 'unknown_nav', 'stale_valuation'}
    model_blockers.update({'independent_stale_valuation', 'independent_valuation_evidence_missing'})
    model_return = reported if not violations and not model_blockers.intersection(blockers) else None
    values = pd.concat([pd.Series([config['initial_capital_rial']]), nav], ignore_index=True)
    return dict(version=VERSION, inventory_audit=inventory,
        invariant_violations=sorted(set(violations)), blockers=blockers,
        expected_sessions=len(days), recorded_sessions=len(curve), stale_sessions=int(stale.sum()),
        unknown_nav_sessions=int((~np.isfinite(nav)).sum()), executions=len(trades),
        fees_rial=total_fees, reported_model_return=reported, observed_path_model_return=model_return,
        maximum_observed_drawdown=float(-(values / values.cummax() - 1).min()) if model_return is not None else None,
        buy_executions=int(trades.side.eq('buy').sum()) if not trades.empty else 0,
        sell_executions=int(trades.side.eq('sell').sum()) if not trades.empty else 0,
        gross_turnover_multiple=float((trades.quantity * trades.price_rial).sum() / config['initial_capital_rial'])
            if not trades.empty else 0.,
        modeled_cash_return=model_return if not set(blockers) - {'synthetic_corporate_entitlements'} else None,
        economic_return=None, actual_fills_verified=False, economic_profit_verified=False,
        accepted_for_signals=False)
