"""Fixed reserved-budget DIP targets with integer capacity and partial disposals.

These are independent-lot research targets, not shared-account returns or proof
of actual fills, corporate rights, historical fees or a guaranteed loss limit.
"""

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, _calendar, _checked_window
from backtest.prepared import PRICE_SNAPSHOT_VERSION, PreparedPortfolioPrices, prepare_portfolio_prices
from config import settings
from core.timeutils import is_canonical_iso_date
from research.dip import THROUGH_MARGIN
from research.dip_outcomes import RULE, _opening_valid
from research.expectancy import _keys

VERSION = 'sized-dip-attempt-outcomes-v1'
TARGET_POLICY = 'fixed-budget-normal-and-min-normal-actual-stress-v1'


def budget_value(value):
    if type(value) is not int or not 1 <= value < 2**53:
        raise ValueError('A positive exactly representable whole-rial research budget is required')
    return value


def execution_contract(budget_rial, *, fees=None, participation=.05, slippage_bps=10):
    budget_value(budget_rial)
    return dict(version=VERSION, portfolio_version=PORTFOLIO_VERSION,
        price_snapshot_version=PRICE_SNAPSHOT_VERSION, rule=asdict(RULE), budget_rial=budget_rial,
        target_policy=TARGET_POLICY, unit='net cash change / fixed reserved budget',
        known_no_fill_return=0., entry_day_exit=False, global_account_interactions_modeled=False,
        account_loss_floor_modeled=False, unspent_budget_in_denominator=True,
        missing_bar_policy='unknown',post_entry_reference_change_policy='unknown_economic_entitlement',
        fees=dict(settings.FEES) if fees is None else dict(fees), participation=participation,
        slippage_bps=slippage_bps, cutoff=settings.RESEARCH_CONFIG['holdout_start'],
        probability_of_profit=None, accepted_for_signals=False)


def _quantity_evidence(row):
    volume, value = row['volume'], row['value']
    return (not isinstance(volume,(bool,np.bool_)) and math.isfinite(volume)
        and 0 < volume < 2**53 and float(volume).is_integer()
        and math.isfinite(value) and value > 0)


def _one(prepared, symbol, days, decision_index, decision, budget, fees, participation, slip, stress):
    unknown = dict(state='unknown',reason=None,resolved_at=None,net=None,entry_filled=None,
        entry_date=None,exit_date=None,entry_price=None,entry_quantity=None,entry_cost_rial=None,
        remaining_quantity=None,proceeds_rial=None,exit_rows=0,last_partial_exit_date=None)
    entry_index = decision_index+1
    if entry_index >= len(days):
        return unknown | dict(reason='censored_entry')
    entry_day = days[entry_index]
    entry = prepared.sessions.get(entry_day,{}).get(symbol)
    if entry is None:
        return unknown | dict(reason='missing_entry_bar',entry_date=entry_day)
    if not entry['valid'] or not _opening_valid(entry) or not _quantity_evidence(entry):
        return unknown | dict(reason='invalid_entry_execution',entry_date=entry_day)
    no_fill = unknown | dict(state='entry_blocked',resolved_at=entry_day,net=0.,entry_filled=False,
        entry_date=entry_day,entry_quantity=0,remaining_quantity=0,entry_cost_rial=0.,proceeds_rial=0.)
    if entry['up']:
        return no_fill | dict(reason='locked_buy')
    limit = entry['ref_price_raw']*(1-RULE.dip)
    if not math.isfinite(limit) or limit <= 0:
        return unknown | dict(reason='invalid_entry_limit',entry_date=entry_day)
    if entry['open_raw'] <= limit:
        price = entry['open_raw']
    elif entry['low_raw'] <= limit*(1-THROUGH_MARGIN):
        price = limit
    else:
        return no_fill | dict(reason='limit_not_filled')
    if stress:
        price = min(limit,entry['high_raw'])
    requested = math.floor(min(decision['volume']*participation,
        decision['value']*participation/limit,budget/(limit*(1+fees['buy']))))
    quantity = math.floor(min(requested,min(entry['volume'],decision['volume'])*participation,
        min(entry['value'],decision['value'])*participation/price,budget/(price*(1+fees['buy']))))
    if quantity < 1:
        return no_fill | dict(reason='insufficient_capacity_or_budget')
    if quantity >= 2**53:
        return unknown | dict(reason='unrepresentable_integer_quantity',entry_date=entry_day)
    notional = quantity*price
    cost = notional+notional*fees['buy']
    if not math.isfinite(cost) or cost > budget+1e-6:
        return unknown | dict(reason='invalid_entry_cash',entry_date=entry_day)
    held = unknown | dict(entry_filled=True,entry_date=entry_day,entry_price=price,
        entry_quantity=quantity,entry_cost_rial=cost,remaining_quantity=quantity,proceeds_rial=0.)
    remaining,proceeds,exit_rows,last_exit = quantity,0.,0,None
    target,due = price*(1+RULE.take_profit),entry_index+RULE.hold
    for index in range(entry_index+1,len(days)):
        day = days[index]
        known = held | dict(remaining_quantity=remaining,proceeds_rial=proceeds,
            exit_rows=exit_rows,last_partial_exit_date=last_exit)
        row = prepared.sessions.get(day,{}).get(symbol)
        if row is None:
            return known | dict(reason='missing_holding_bar')
        if not row['mark_valid'] or not math.isfinite(row['action']):
            return known | dict(reason='unknown_holding_reference')
        if abs(row['action']-1)>1e-6:
            return known | dict(reason='unknown_economic_entitlement')
        if not row['valid'] or not _opening_valid(row) or not _quantity_evidence(row):
            return known | dict(reason='invalid_exit_execution')
        if row['down']:
            continue
        if stress:
            if index < due:
                continue
            exit_price = row['low_raw']
        elif index <= due and row['high_raw'] >= target:
            exit_price = max(row['open_raw'],target)
        elif index >= due:
            last = row['close_raw']
            if not math.isfinite(last) or not row['low_raw'] <= last <= row['high_raw']:
                return known | dict(reason='unknown_time_exit')
            exit_price = max(last*(1-slip),row['low_raw'])
        else:
            continue
        sold = min(remaining,math.floor(min(row['volume']*participation,row['value']*participation/exit_price)))
        if sold < 1:
            continue
        sale = sold*exit_price
        proceeds += sale-sale*fees['sell']
        if not math.isfinite(proceeds):
            return known | dict(reason='nonfinite_exit_cash')
        remaining -= sold
        exit_rows += 1
        last_exit = day
        if remaining == 0:
            return held | dict(state='complete',reason='complete',resolved_at=day,net=(proceeds-cost)/budget,
                exit_date=day,remaining_quantity=0,proceeds_rial=proceeds,
                exit_rows=exit_rows,last_partial_exit_date=day)
    return held | dict(reason='censored_remaining_quantity',remaining_quantity=remaining,
        proceeds_rial=proceeds,exit_rows=exit_rows,last_partial_exit_date=last_exit)


def sized_dip_attempt_outcomes(prices, decisions, calendar, *, budget_rial, participation=.05,
                               slippage_bps=10, fees=None, end_exclusive=None):
    """No open residual, missing bar or corporate event is converted into zero return.

    Capacity uses the preceding decision's totals and the actual execution bar.
    Budget is reserved before opening improvement; unused budget remains in the
    denominator. Independent labels do not reproduce competition for account
    cash, a portfolio loss-floor exit, queue rank or actual broker execution.
    """
    budget_value(budget_rial)
    _keys(decisions)
    if set(decisions)!={'date','symbol'}:
        raise ValueError('Only decision keys may enter sized outcomes')
    cutoff = settings.RESEARCH_CONFIG['holdout_start'] if end_exclusive is None else end_exclusive
    days = _calendar(calendar).tolist()
    if (not is_canonical_iso_date(cutoff) or cutoff>settings.RESEARCH_CONFIG['holdout_start']
            or any(d>=cutoff for d in days) or decisions.date.ge(cutoff).any()):
        raise ValueError('Sized DIP inputs must precede the sealed boundary')
    if (type(participation) not in (int,float) or not math.isfinite(participation) or not 0<participation<=.05
            or type(slippage_bps) not in (int,float) or not math.isfinite(slippage_bps) or not 0<=slippage_bps<10000):
        raise ValueError('Finite capped participation and bounded slippage are required')
    fees = dict(settings.FEES) if fees is None else fees
    if (not isinstance(fees,dict) or set(fees)!={'buy','sell'} or any(type(v) not in (int,float)
            or not math.isfinite(v) or not 0<=v<1 for v in fees.values())):
        raise ValueError('Explicit finite buy and sell fees are required')
    if not isinstance(prices,PreparedPortfolioPrices):
        _keys(prices)
        if prices.date.ge(cutoff).any():
            raise ValueError('Sized DIP prices reach the sealed boundary')
        prices = prepare_portfolio_prices(prices)
    if (prices.version!=PRICE_SNAPSHOT_VERSION or not prices.has_open
            or any(not is_canonical_iso_date(d) or d>=cutoff for d in prices.sessions)):
        raise ValueError('A supported pre-cutoff immutable price view with opens is required')
    _checked_window(prices, decisions, days, None, cutoff)
    positions = {d:i for i,d in enumerate(days)}
    if not set(decisions.date)<=set(positions):
        raise ValueError('Every sized decision date must belong to the declared calendar')
    records = []
    for day,symbol in decisions[['date','symbol']].itertuples(index=False,name=None):
        decision = prices.sessions.get(day,{}).get(symbol)
        if decision is None or not decision['valid'] or not _quantity_evidence(decision):
            cases = [dict(state='unknown',reason='invalid_decision',resolved_at=None,net=None,
                entry_filled=None,entry_date=None,exit_date=None,entry_quantity=None,
                entry_price=None,entry_cost_rial=None,remaining_quantity=None,proceeds_rial=None,
                exit_rows=0,last_partial_exit_date=None)]*2
        else:
            cases = [_one(prices,symbol,days,positions[day],decision,budget_rial,fees,
                participation,slippage_bps/10000,stress) for stress in (False,True)]
        known = all(c['state']!='unknown' for c in cases)
        record = dict(date=day,symbol=symbol,state=('entry_blocked' if all(c['state']=='entry_blocked' for c in cases)
            else 'complete') if known else 'unknown',resolved_at=max(c['resolved_at'] for c in cases) if known else None,
            normal_net=cases[0]['net'] if known else np.nan,
            floor_net=min(c['net'] for c in cases) if known else np.nan)
        for prefix,case in zip(('normal','stress'),cases,strict=True):
            record.update({prefix+'_'+('observed_net' if key=='net' else key):v for key,v in case.items()})
        records.append(record)
    out = pd.DataFrame(records) if records else pd.DataFrame(columns=['date','symbol','state','resolved_at','normal_net','floor_net'])
    out.attrs.update(execution_contract(budget_rial,fees=fees,participation=participation,slippage_bps=slippage_bps),
        cutoff=cutoff)
    return out
