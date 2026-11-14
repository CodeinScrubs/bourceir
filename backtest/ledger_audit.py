"""Independent inventory/valuation reconciliation for the synthetic daily model.

No portfolio-engine bookkeeping or fraction-book methods are used here. This
checks the declared research contract, not real entitlements or order fills.
"""

import hashlib
import json
import math

import numpy as np

from config import settings
from core.instrument_history import receipt_time
from core.no_trade_valuation import parse_no_trade_publication
from core.timeutils import TEHRAN_TZ

VERSION = 'synthetic-inventory-audit-v2'


def _equal(left, right, *, money=False):
    try:
        return (math.isfinite(left) and math.isfinite(right)
                and math.isclose(left, right, rel_tol=1e-10, abs_tol=.01 if money else 1e-7))
    except TypeError:
        return False


def _flat_marks(observations, days):
    marks = {}
    for observation in observations or ():
        code, day = observation['ins_code'], observation['requested_date']
        proof = parse_no_trade_publication(observation, code, day)
        received = receipt_time(observation['received_at']).astimezone(TEHRAN_TZ).date().isoformat()
        if (observation.get('http_status') != 200 or day not in days
                or day >= settings.RESEARCH_CONFIG['holdout_start'] or received <= day
                or proof['status'] != 'observed_flat_mark' or (code, day) in marks):
            raise ValueError('A unique, dated, complete native flat-mark publication is required')
        marks[code, day] = proof['official_close_rial']
    return marks


def _price(event, row, lot, config, dip):
    """Compute the declared price independently from raw traded totals/OHLC."""
    slip = config['slippage_bps'] / 10000
    low, high = row['low_raw'], row['high_raw']
    vwap = row['value'] / row['volume']
    if dip is not None and (event['side'] == 'buy' or event.get('exit_kind') != 'loss_floor'):
        opening = row.get('open_raw')
        if not isinstance(opening, (int, float)) or not math.isfinite(opening) or not 0 < low <= opening <= high:
            return None
    if event['side'] == 'buy':
        if dip is None:
            return high if config['stress'] else min(vwap * (1 + slip), high)
        limit, opening = row['ref_price_raw'] * (1 - dip['dip']), row['open_raw']
        if opening <= limit:
            price = opening
        elif low <= limit * (1 - .001 if dip['conservative'] else 1):
            price = limit
        else:
            return None
        return min(limit, high) if config['stress'] else price
    if config['stress']:
        return low
    if dip is None or event.get('exit_kind') == 'loss_floor':
        return max(vwap * (1 - slip), low)
    if lot is None:
        return None
    if event.get('exit_kind') == 'target':
        if high < lot['target']:
            return None
        return lot['target'] if event['date'] == lot['entry'] else max(row['open_raw'], lot['target'])
    return max(row['close_raw'] * (1 - slip), low)


def reconcile_synthetic_inventory(result, prepared, days, *, no_trade_valuation_observations=None):
    """Rebuild every whole/fractional holding, cash balance and marked NAV.

    Stale and unknown marks remain distinct. A native no-trade mark is accepted
    only from independently supplied publisher bytes, never from the replay's
    own decision log. Unknown terminal rows cannot certify inventory or NAV.
    """
    config, fees = result.assumptions['config'], result.assumptions['fees']
    dip = result.assumptions.get('dip_rule')
    supported = {'bounded_daily_vwap_proxy', 'buy_high_sell_low_stress', 'dip_limit_daily_proxy',
                 'dip_no_open_improvement_no_target_sell_low_stress'}
    if result.assumptions['execution_model'] not in supported:
        raise ValueError('Unsupported execution-price contract')
    proofs = _flat_marks(no_trade_valuation_observations, set(days))
    strict = 'no_trade_valuation_version' in result.assumptions
    events = {day: [] for day in days}
    violations, blockers, ledger = set(), set(), []
    for event in result.executions.to_dict('records'):
        values = [event.get(k) for k in ('quantity', 'price_rial', 'fee_rial')]
        if (event.get('date') not in events or event.get('side') not in {'buy', 'sell'}
                or any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.number))
                       or not math.isfinite(v) for v in values) or values[0] <= 0 or values[1] <= 0):
            continue  # The parent auditor reports malformed executions.
        events[event['date']].append(event)
    lots, fractions = {}, {}
    cash, action_count = float(config['initial_capital_rial']), 0
    observed_nav_days, stale_days, previous_day = 0, 0, None
    previous_curve_day = None
    for point in result.equity.to_dict('records'):
        day = point['date']
        if day not in events:
            continue
        raw = prepared.sessions.get(day, {})
        known_nav = isinstance(point['equity_rial'], (int, float)) and math.isfinite(point['equity_rial'])
        # Actions affect existing inventory before any current-session trade.
        for symbol in sorted(set(lots) | set(fractions)):
            row = raw.get(symbol)
            if row is None:
                flat = proofs.get((symbol, day))
                exposures = [book[symbol] for book in (lots, fractions) if symbol in book]
                if flat is not None and all(p['mark_date'] == previous_day and _equal(p['mark'], flat) for p in exposures):
                    for exposure in exposures:
                        exposure.update(mark=flat, mark_date=day)
                elif strict and known_nav:
                    blockers.add('independent_valuation_evidence_missing')
                continue
            try:
                reference, prior = row['ref_price_raw'], row['previous_official']
                first = not isinstance(row['previous_date'], str)
                ratio = 1. if first else reference / prior
                mark = row['close_final_raw']
                if not all(math.isfinite(v) and v > 0 for v in (ratio, mark, reference)):
                    raise ValueError('Unknown reference or official mark')
            except (TypeError, ZeroDivisionError, ValueError):
                if known_nav:
                    violations.add('inventory_mark_not_observed')
                continue
            if abs(ratio - 1) > 1e-6:
                action_count += 1
                if symbol in fractions:
                    fractions[symbol]['quantity'] /= ratio
                if symbol in lots:
                    exact = lots[symbol]['quantity'] / ratio
                    whole = math.floor(exact + 1e-9)
                    remainder = max(0., exact - whole)
                    fraction_cost = lots[symbol]['cost'] * remainder / exact
                    if remainder:
                        claim = fractions.setdefault(symbol, dict(quantity=0., cost=0., mark=mark, mark_date=day))
                        claim['quantity'] += remainder
                        claim['cost'] += fraction_cost
                    lots[symbol]['quantity'] = whole
                    lots[symbol]['cost'] -= fraction_cost
                    if dip:
                        lots[symbol]['target'] *= ratio
                    if not whole:
                        del lots[symbol]
            for book in (lots, fractions):
                if symbol in book:
                    book[symbol].update(mark=mark, mark_date=day)
        sales_seen = False
        for event in events[day]:
            symbol, side = event['symbol'], event['side']
            qty, price, fee = (event[k] for k in ('quantity', 'price_rial', 'fee_rial'))
            row, lot = raw.get(symbol), lots.get(symbol)
            if row is not None and row['valid']:
                expected = _price(event, row, lot, config, dip)
                if expected is None or not _equal(price, expected):
                    violations.add('execution_price_policy_mismatch')
            if side == 'buy':
                if sales_seen:
                    violations.add('execution_order_policy_mismatch')
                if lot is not None:
                    violations.add('duplicate_open_inventory_purchase')
                cost = qty * price + fee
                cash -= cost
                lots[symbol] = dict(quantity=qty, cost=cost, mark=row['close_final_raw'] if row else price,
                    mark_date=day if row else None, entry=day,
                    target=price * (1 + dip['take_profit']) if dip else None)
            else:
                sales_seen = True
                cash += qty * price - fee
                if lot is None or qty > lot['quantity'] + 1e-8:
                    violations.add('sale_exceeds_independent_inventory')
                    continue
                cost = lot['cost'] * qty / lot['quantity']
                if not _equal(event.get('realized_net_rial'), qty * price - fee - cost, money=True):
                    violations.add('realized_profit_not_reconciled')
                lot['quantity'] -= qty
                lot['cost'] -= cost
                if lot['quantity'] <= 1e-8:
                    del lots[symbol]
        if cash < -.01:
            violations.add('independent_cash_negative')
        if known_nav:
            fraction_value = sum(p['quantity'] * p['mark'] * (1 - fees['sell']) for p in fractions.values())
            nav = cash + fraction_value + sum(p['quantity'] * p['mark'] * (1 - fees['sell']) for p in lots.values())
            stale_whole = sum(p['mark_date'] != day for p in lots.values())
            stale_fraction = sum(p['mark_date'] != day for p in fractions.values())
            if not _equal(point['equity_rial'], nav, money=True):
                violations.add('inventory_nav_not_reconciled')
            if not _equal(point.get('noncash_receivable_rial'), fraction_value, money=True):
                violations.add('fractional_nav_not_reconciled')
            if point['positions'] != len(lots):
                violations.add('independent_position_count_mismatch')
            if point['stale_positions'] != stale_whole or point['stale_fractional_exposures'] != stale_fraction:
                violations.add('independent_stale_count_mismatch')
            if stale_whole or stale_fraction:
                stale_days += 1
                blockers.add('independent_stale_valuation')
            observed_nav_days += 1
            ledger.append(dict(date=day, cash_rial=cash, whole_nav_rial=nav-cash-fraction_value,
                fractional_nav_rial=fraction_value, inventory={s: p['quantity'] for s, p in sorted(lots.items())},
                fractional_units={s: p['quantity'] for s, p in sorted(fractions.items())}))
            previous_curve_day = day
        previous_day = day
    terminal_known = bool(len(result.equity)) and previous_curve_day == result.equity.iloc[-1]['date']
    if terminal_known:
        if result.metrics['open_positions'] != len(lots):
            violations.add('terminal_inventory_not_reconciled')
        if result.metrics['corporate_action_events'] != action_count:
            violations.add('corporate_action_count_not_reconciled')
        records = result.metrics.get('fractional_exposures', [])
        reported = {p['symbol']: p for p in records}
        if len(records) != len(reported) or set(reported) != set(fractions) or any(
                not _equal(reported[s]['quantity'], p['quantity']) or not _equal(reported[s]['cost_rial'], p['cost'], money=True)
                or not _equal(reported[s]['mark'], p['mark']) or reported[s]['mark_date'] != p['mark_date']
                for s, p in fractions.items() if s in reported):
            violations.add('terminal_fractional_inventory_not_reconciled')
    return dict(version=VERSION, violations=sorted(violations), blockers=sorted(blockers),
        reconstructed_nav_sessions=observed_nav_days, independently_stale_sessions=stale_days,
        terminal_inventory_checked=terminal_known, independently_reconstructed_open_positions=len(lots),
        ledger_sha256=hashlib.sha256(json.dumps(ledger, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode()).hexdigest(), actual_entitlements_verified=False)
