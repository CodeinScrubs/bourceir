"""Execution-aligned one-share DIP outcomes; unknown evidence never becomes idle cash."""

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, _calendar, _checked_window
from backtest.prepared import PRICE_SNAPSHOT_VERSION, PreparedPortfolioPrices, prepare_portfolio_prices
from config import settings
from core.timeutils import is_canonical_iso_date
from research.dip import THROUGH_MARGIN, DipRule
from research.expectancy import _keys

VERSION = 'dip-attempt-outcomes-v1'
TARGET_POLICY = 'normal-and-min-normal-actual-stress-per-one-share-attempt-v1'
RULE = DipRule(dip=.02, take_profit=.03, hold=1, conservative=True)


def execution_contract():
    return dict(version=VERSION, portfolio_version=PORTFOLIO_VERSION, price_snapshot_version=PRICE_SNAPSHOT_VERSION,
        rule=asdict(RULE), entry_day_exit=False, unit='one whole share', target_policy=TARGET_POLICY,
        missing_bar_policy='unknown', post_entry_reference_change_policy='unknown_economic_entitlement',
        known_no_fill_return=0., fees=dict(settings.FEES),participation=.05,slippage_bps=10,
        cutoff=settings.RESEARCH_CONFIG['holdout_start'], probability_of_profit=None, accepted_for_signals=False)


def _opening_valid(row):
    return math.isfinite(row['open_raw']) and 0 < row['low_raw'] <= row['open_raw'] <= row['high_raw']


def _one(prepared, symbol, days, decision_index, decision_row, rule, participation, slip, keep, stress):
    unknown = dict(state='unknown', reason=None, resolved_at=None, net=None, entry_filled=None,
        entry_date=None, exit_date=None, entry_price=None, exit_price=None, exit_kind=None)
    entry_index = decision_index + 1
    if entry_index >= len(days):
        return dict(unknown, reason='censored_entry')
    entry_day = days[entry_index]
    entry = prepared.sessions.get(entry_day, {}).get(symbol)
    if entry is None:
        return dict(unknown, reason='missing_entry_bar', entry_date=entry_day)
    if not entry['valid'] or not _opening_valid(entry):
        return dict(unknown, reason='invalid_entry_execution', entry_date=entry_day)
    no_fill = dict(unknown, state='entry_blocked', resolved_at=entry_day, net=0., entry_filled=False,
        entry_date=entry_day)
    if entry['up']:
        return dict(no_fill, reason='locked_buy')
    limit = entry['ref_price_raw'] * (1-rule.dip)
    if entry['open_raw'] <= limit:
        price = entry['open_raw']
    elif entry['low_raw'] <= limit * (1-THROUGH_MARGIN if rule.conservative else 1):
        price = limit
    else:
        return dict(no_fill, reason='limit_not_filled')
    if stress:
        price = min(limit, entry['high_raw'])
    # The portfolio fixes requested quantity at the buy limit before opening improvement.
    cap = min(decision_row['volume']*participation, decision_row['value']*participation/limit,
        min(entry['volume'], decision_row['volume'])*participation,
        min(entry['value'], decision_row['value'])*participation/price)
    if math.floor(cap) < 1:
        return dict(no_fill, reason='insufficient_capacity')
    held = dict(unknown, entry_filled=True, entry_date=entry_day, entry_price=price)
    target, due = price*(1+rule.take_profit), entry_index+rule.hold
    # Entry-day disposal is deliberately excluded in this research family.
    for index in range(entry_index+1,len(days)):
        day = days[index]
        row = prepared.sessions.get(day,{}).get(symbol)
        if row is None:
            return dict(held, reason='missing_holding_bar')
        if not row['mark_valid'] or not math.isfinite(row['action']):
            return dict(held, reason='unknown_holding_reference')
        if abs(row['action']-1)>1e-6:
            return dict(held, reason='unknown_economic_entitlement')
        if not row['valid'] or not _opening_valid(row):
            return dict(held, reason='invalid_exit_execution')
        if row['down']:
            continue
        if stress:
            if index < due:
                continue
            exit_price, kind = row['low_raw'], 'time_stress'
        elif index <= due and row['high_raw'] >= target:
            exit_price, kind = max(row['open_raw'],target), 'target'
        elif index >= due:
            last = row['close_raw']
            if not math.isfinite(last) or not row['low_raw'] <= last <= row['high_raw']:
                return dict(held, reason='unknown_time_exit')
            exit_price, kind = max(last*(1-slip),row['low_raw']), 'time' if index==due else 'time_late'
        else:
            continue
        if math.floor(min(row['volume']*participation,row['value']*participation/exit_price)) < 1:
            continue
        return dict(held, state='complete', reason='complete', resolved_at=day,
            exit_date=day, exit_price=exit_price, exit_kind=kind, net=exit_price*keep/price-1)
    return dict(held, reason='censored_exit')


def dip_attempt_outcomes(prices, decisions, calendar, *, rule=RULE, participation=.05,
                         slippage_bps=10, fees=None, end_exclusive=None):
    """Normal and actual stress may cross; fit their per-attempt minimum, not a false ordering.

    One-share outcomes model the attempt before capital coupling. Full portfolio replay must
    independently test shared cash, integer size, loss-floor exits and the owner's order capacity.
    Missing bars are less certain than an evidenced failed order, even when the daily portfolio
    proxy calls them a delay. A reference change after entry is not an economic share/cash ledger.
    """
    _keys(decisions)
    if set(decisions)!={'date','symbol'} or not isinstance(rule,DipRule) or rule.stop_loss is not None:
        raise ValueError('Decision keys and a DIP rule without a stop are required')
    cutoff = settings.RESEARCH_CONFIG['holdout_start'] if end_exclusive is None else end_exclusive
    days = _calendar(calendar).tolist()
    if (not is_canonical_iso_date(cutoff) or cutoff>settings.RESEARCH_CONFIG['holdout_start']
            or any(day>=cutoff for day in days) or decisions.date.ge(cutoff).any()):
        raise ValueError('DIP research inputs must precede the sealed boundary')
    if (type(participation) not in (int,float) or not math.isfinite(participation) or not 0<participation<=.05
            or type(slippage_bps) not in (int,float) or not math.isfinite(slippage_bps) or not 0<=slippage_bps<10000):
        raise ValueError('Finite participation at most 5% and bounded slippage are required')
    fees = dict(settings.FEES) if fees is None else fees
    if (not isinstance(fees,dict) or set(fees)!={'buy','sell'} or any(type(v) not in (int,float)
            or not math.isfinite(v) or not 0<=v<1 for v in fees.values())):
        raise ValueError('Explicit finite buy/sell fees are required')
    if not isinstance(prices,PreparedPortfolioPrices):
        _keys(prices)
        if prices.date.ge(cutoff).any():
            raise ValueError('Price rows enter the sealed boundary')
        prices = prepare_portfolio_prices(prices)
    if (prices.version!=PRICE_SNAPSHOT_VERSION or not prices.has_open
            or any(not is_canonical_iso_date(day) or day>=cutoff for day in prices.sessions)):
        raise ValueError('A supported pre-cutoff immutable price view with opening prices is required')
    _checked_window(prices, decisions, days, None, cutoff)
    positions = {day:i for i,day in enumerate(days)}
    if not set(decisions.date)<=set(positions):
        raise ValueError('Every decision date must be in the declared market calendar')
    keep = (1-fees['sell'])/(1+fees['buy'])
    records = []
    for day,symbol in decisions[['date','symbol']].itertuples(index=False,name=None):
        decision = prices.sessions.get(day,{}).get(symbol)
        if decision is None or not decision['valid']:
            cases = [dict(state='unknown',reason='invalid_decision',resolved_at=None,net=None,
                entry_filled=None,entry_date=None,exit_date=None,entry_price=None,exit_price=None,exit_kind=None)]*2
        else:
            cases = [_one(prices,symbol,days,positions[day],decision,rule,participation,
                slippage_bps/10000,keep,stress) for stress in (False,True)]
        normal, stress = cases
        known = normal['state']!='unknown' and stress['state']!='unknown'
        record = dict(date=day,symbol=symbol,state=('entry_blocked' if all(c['state']=='entry_blocked' for c in cases)
            else 'complete') if known else 'unknown',resolved_at=max(c['resolved_at'] for c in cases) if known else None,
            normal_net=normal['net'] if known else np.nan,
            floor_net=min(c['net'] for c in cases) if known else np.nan)
        for prefix,case in zip(('normal','stress'),cases,strict=True):
            record.update({prefix+'_'+('observed_net' if key=='net' else key):value for key,value in case.items()})
        records.append(record)
    columns = ['date','symbol','state','resolved_at','normal_net','floor_net']
    out = pd.DataFrame(records) if records else pd.DataFrame(columns=columns)
    out.attrs.update(execution_contract(),rule=asdict(rule),fees=dict(fees),participation=participation,
        slippage_bps=slippage_bps,cutoff=cutoff)
    return out
