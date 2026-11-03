"""One fixed as-of basket reconstruction, not a probability model or an unseen test."""

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest.portfolio import OffCalendarPriceError, PortfolioConfig, _checked_window, replay_portfolio
from core import market_rules
from core.opportunity_radar import RAW, scope_rows
from core.price_quality import numeric_prices
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date
from research.baselines import STRATEGIES, eligible_strategy_score, pick_top
from research.labels import market_calendar

VERSION = 'anniversary-replay-v1'
RULE = 'momentum_20d'


def _rial_units(frame):
    if 'price_unit' in frame and not frame.price_unit.eq('Rial').fillna(False).all():
        raise ValueError('Explicit price_unit must be Rial; unknown units cannot be guessed')


def scoped_prices(prices, registry, *, through):
    """Bind optional native identities/units before using the ordinary-stock calendar.

    Legacy snapshots without metadata remain unverified; supplied metadata may not
    contradict the frozen registry. Neither foreign instruments nor future rows
    may change the as-of universe, calendar or validity of the selection.
    """
    if not isinstance(prices,pd.DataFrame) or prices.columns.duplicated().any() or not {'symbol','date',*RAW}<=set(prices):
        raise ValueError('Raw dated price panel required')
    if not is_canonical_iso_date(through) or any(not is_canonical_iso_date(d) for d in prices.date.unique()):
        raise ValueError('Canonical dates required')
    identities=scope_rows(registry)
    names={item['symbol_fa'] for item in identities}
    metadata=[name for name in ('ins_code','price_unit') if name in prices]
    past=prices.loc[prices.date.le(through),['symbol','date',*RAW,*metadata]].copy()
    past['symbol']=past.symbol.map(normalize_symbol)
    past=past.loc[past.symbol.isin(names)].copy()
    _rial_units(past)
    if 'ins_code' in past:
        codes={}
        for item in identities:
            codes.setdefault(item['symbol_fa'],set()).add(item['ins_code'])
        for name,code in zip(past.symbol,past.ins_code):
            # Floating-point identifiers can already have lost significant digits.
            if (isinstance(code,(bool,np.bool_)) or not isinstance(code,(str,int,np.integer))
                    or str(code) not in codes[name]):
                raise ValueError('Price identity differs from the frozen native registry')
    return past


def asof_selection(prices, registry, *, requested_start, k, min_value):
    """Future numerical fields cannot enter selection, even when present in the file.

    The old daily feature path rolls over a stock's rows; here the same momentum
    definition is constrained to twenty actual inferred market sessions. A halt
    cannot compress a month into twenty observations spread over several months.
    """
    if (not is_canonical_iso_date(requested_start) or type(k) is not int or k < 1
            or isinstance(min_value,bool) or not math.isfinite(min_value) or min_value <= 0):
        raise ValueError('Explicit date, positive basket size and liquidity bound required')
    # Slice dates before touching any prices, values, future labels or adjusted fields.
    past=scoped_prices(prices,registry,through=requested_start)
    if past.duplicated(['symbol','date']).any():
        raise ValueError('Duplicate normalized symbol/date')
    past[RAW]=numeric_prices(past)
    calendar=market_calendar(past).tolist()
    if len(calendar)<21:
        raise ValueError('At least twenty-one inferred market sessions are required')
    days=calendar[-21:]
    decision=days[-1]
    grouped={name:g for name,g in past.groupby('symbol')}
    panel=[]
    for identity in scope_rows(registry):
        name=identity['symbol_fa']
        row=dict(symbol=name,ins_code=identity['ins_code'],date=decision,ret_20d=None,value=None,
            status='missing_history',probability_of_multibagger=None)
        panel.append(row)
        own=grouped.get(name)
        if identity.get('symbol_identity_ambiguous'):
            row['status']='ambiguous_identity'
            continue
        if own is None:
            continue
        window=own.set_index('date').reindex(days)
        row['last_observed_date']=own.date.max()
        if decision not in set(own.date):
            row['status']='stale_decision'
            continue
        row['value']=float(window.value.iloc[-1]) if np.isfinite(window.value.iloc[-1]) else None
        valid=(window[RAW].notna().all(axis=1)&window[RAW].gt(0).all(axis=1)
            &window.volume.mod(1).eq(0)&window.low_raw.le(window.high_raw)
            &window.open_raw.between(window.low_raw,window.high_raw)
            &window.close_raw.between(window.low_raw,window.high_raw)&market_rules.daily_vwap(window).notna())
        off=own.loc[own.date.between(days[0],decision)&~own.date.isin(days)]
        if not valid.all() or not off.empty:
            row['status']='incomplete_or_invalid_21_session_window'
            continue
        if row['value'] < min_value:
            row['status']='below_fixed_liquidity_bound'
            continue
        returns=market_rules.pit_daily_returns(window).iloc[1:]
        score=float(np.expm1(np.log1p(returns).sum()))
        if not np.isfinite(score):
            row['status']='unknown_score'
            continue
        row.update(status='eligible',ret_20d=score)
    full=pd.DataFrame(panel)
    eligible=full.loc[full.status.eq('eligible')].copy()
    rule=next(s for s in STRATEGIES if s.name==RULE)
    selected=pick_top(eligible,eligible_strategy_score(eligible,rule),k).drop(columns='_score').reset_index(drop=True)
    selected['rank']=np.arange(1,len(selected)+1)
    return dict(decision_date=decision,calendar=calendar,panel=full,selected=selected)


def _unknown_calendar_sleeve(symbol, decision, days, config, *, explicit_actions):
    """Preserve an unreplayable sleeve without creating executions or known cash."""
    summary=dict(symbol=symbol,status='off_calendar_path',net_model_return=None,
        initial_capital_rial=config.initial_capital_rial,invested_rial=None,
        allocation_filled_except_integer_rounding=None,entry_date=None,entry_price_rial=None,
        entry_quantity=None,exit_date=None,exit_price_rial=None,fees_rial=None,
        corporate_action_events=None,open_positions=None,stale_mark_days=None,fractional_exposures=None,
        model_final_value_rial=None,cash_final_rial=None,decision_statuses=['off_calendar_path'],
        fully_liquidated=None,economic_profit_verified=False,own_fills_verified=False,
        accepted_for_signals=False,config=asdict(config),engine_assumptions=dict(
            replay_performed=False,calendar_price_conflict=True,
            limitations=['No cash, execution or corporate-action accounting inferred from conflicting dates']))
    if explicit_actions:
        summary['corporate_accounting']=dict(status='not_replayed',model=None,pending_bonus_units=None,
            cash_dividend_receivable_face_rial=None,cash_dividend_income_rial=None,
            bonus_claims=None,bonus_ledger=None,cash_dividend_claims=None,cash_dividend_ledger=None)
    curve=pd.DataFrame(dict(date=[d for d in days if d>=decision],equity_rial=np.nan,cash_rial=np.nan))
    decisions=pd.DataFrame([dict(date=decision,symbol=symbol,status='off_calendar_path')])
    return summary,curve,pd.DataFrame(),decisions


def replay_sleeve(prices, *, symbol, decision, calendar, initial_capital_rial, fees, slippage_bps,
                  participation=.05, stress=False, corporate_actions=None):
    """Use the existing execution engine; reject off-calendar and stale terminal marks.

    A missing selection is never replaced by a future winner. Known blocked buys
    remain cash. An unknown execution/path never becomes zero or drops from a mean.
    """
    from config import settings

    if dict(settings.FEES)!=fees:
        raise ValueError('Frozen fee scenario differs from current engine fees')
    days=list(calendar)
    if (not days or decision not in days or days!=sorted(set(days))
            or any(not is_canonical_iso_date(d) for d in days)):
        raise ValueError('Sorted unique evaluation calendar containing decision required')
    next_index=days.index(decision)+1
    if len(days)-next_index<2:
        raise ValueError('An entry session and a later terminal session are required')
    own=prices.loc[prices.symbol.eq(symbol)&prices.date.le(days[-1])].copy()
    # Only one predecessor is needed for the first action factor. Hundreds of
    # unused warmup sessions must not be regrouped for every annual sleeve.
    own=pd.concat([own.loc[own.date.lt(decision)].sort_values('date').tail(1),
                   own.loc[own.date.ge(decision)]],ignore_index=True)
    _rial_units(own)
    own[RAW]=numeric_prices(own)
    config=PortfolioConfig(initial_capital_rial=initial_capital_rial,max_positions=1,
        horizon=len(days)-1-next_index,slippage_bps=slippage_bps,participation=participation,stress=stress)
    picks=pd.DataFrame([dict(symbol=symbol,date=decision,rank=1)])
    if corporate_actions is not None:
        from backtest.corporate_actions import BonusDividendBook

        if not isinstance(corporate_actions,BonusDividendBook):
            raise ValueError('دفتر تازه و صریح اقدامات شرکتی برای بازسازی لازم است')
        if corporate_actions.credit_exit_session not in (None,days[-1]):
            raise ValueError('خروج سهم اعتباری در بازسازی سالانه باید همان جلسه پایان باشد')
    try:
        _checked_window(own,picks,days,decision,None)
    except OffCalendarPriceError:
        return _unknown_calendar_sleeve(symbol,decision,days,config,explicit_actions=corporate_actions is not None)
    if corporate_actions is None:
        engine=replay_portfolio(own,picks,days,config,start=decision)
    else:
        from copy import deepcopy

        corporate_actions=deepcopy(corporate_actions)
        corporate_actions.credit_exit_session=days[-1]
        engine=replay_portfolio(own,picks,days,config,start=decision,bonus_shares=corporate_actions)
    transactions=engine.executions
    buys=transactions.loc[transactions.side.eq('buy')] if not transactions.empty else transactions
    sells=transactions.loc[transactions.side.eq('sell')] if not transactions.empty else transactions
    statuses=engine.decisions.status.tolist() if not engine.decisions.empty else []
    value=engine.metrics['total_return']
    status=engine.metrics['status']
    curve=engine.equity.copy()
    if not buys.empty:
        last=curve.iloc[-1]
        if (last.date!=days[-1] or last.stale_positions>0 or last.stale_fractional_exposures>0):
            status,value='stale_terminal_mark',None
            curve.loc[curve.date.eq(days[-1]),'equity_rial']=np.nan
    elif any(s in ('invalid_decision','unknown_entry_execution') for s in statuses):
        status,value='unknown_entry',None
        curve['equity_rial']=np.nan
    else:
        status='cash_unfilled' if status=='complete' else status
    invested=float((buys.quantity*buys.price_rial+buys.fee_rial).sum()) if not buys.empty else 0.
    lot_cost=float(buys.price_rial.iloc[0]*(1+fees['buy'])) if not buys.empty else None
    summary=dict(symbol=symbol,status=status,net_model_return=value,
        initial_capital_rial=initial_capital_rial,invested_rial=invested,
        allocation_filled_except_integer_rounding=bool(lot_cost is not None and initial_capital_rial-invested<lot_cost+1e-6),
        entry_date=str(buys.date.iloc[0]) if not buys.empty else None,
        entry_price_rial=float(buys.price_rial.iloc[0]) if not buys.empty else None,
        entry_quantity=int(buys.quantity.iloc[0]) if not buys.empty else 0,
        exit_date=str(sells.date.iloc[-1]) if not sells.empty else None,
        exit_price_rial=float(sells.price_rial.iloc[-1]) if not sells.empty else None,
        fees_rial=engine.metrics['fees_rial'],corporate_action_events=engine.metrics['corporate_action_events'],
        open_positions=engine.metrics['open_positions'],stale_mark_days=engine.metrics['stale_mark_days'],
        fractional_exposures=engine.metrics['fractional_exposures'],
        model_final_value_rial=None if value is None else initial_capital_rial*(1+value),
        cash_final_rial=float(curve.cash_rial.iloc[-1]),decision_statuses=sorted(set(statuses)),
        fully_liquidated=bool(value is not None and engine.metrics['open_positions']==0
                              and not engine.metrics['fractional_exposures']),
        economic_profit_verified=False,own_fills_verified=False,accepted_for_signals=False,
        config=asdict(config),engine_assumptions=engine.assumptions)
    if corporate_actions is not None:
        summary.update(corporate_accounting=dict(
            model=engine.assumptions['corporate_action_model'],
            pending_bonus_units=engine.metrics['pending_bonus_units'],
            cash_dividend_receivable_face_rial=engine.metrics['cash_dividend_receivable_face_rial'],
            cash_dividend_income_rial=engine.metrics['cash_dividend_income_rial'],
            bonus_claims=engine.metrics['bonus_claims'],bonus_ledger=engine.metrics['bonus_ledger'],
            cash_dividend_claims=engine.metrics['cash_dividend_claims'],
            cash_dividend_ledger=engine.metrics['cash_dividend_ledger']))
    return summary,curve,transactions,engine.decisions


def aggregate_sleeves(summaries, curves, *, k, initial_capital_rial):
    if len(summaries)!=k or len(curves)!=k:
        raise ValueError('Every original sleeve must remain in the denominator')
    dates=curves[0].date.tolist()
    if any(c.date.tolist()!=dates for c in curves):
        # An invalid path may end early. Reindex retains the unknown future, not stale wealth.
        dates=sorted(set(d for c in curves for d in c.date))
    nav=pd.DataFrame({i:c.set_index('date').equity_rial.reindex(dates) for i,c in enumerate(curves)})
    total=nav.sum(axis=1,min_count=k)
    values=[s['net_model_return'] for s in summaries]
    known=all(v is not None and np.isfinite(v) for v in values)
    all_curve_known=known and total.notna().all()
    augmented=pd.concat([pd.Series([initial_capital_rial]),total.reset_index(drop=True)],ignore_index=True)
    def known_sum(field):
        items=[s[field] for s in summaries]
        return None if any(v is None for v in items) else sum(items)
    summary=dict(sleeves=k,known_sleeves=sum(v is not None for v in values),
        unknown_sleeves=sum(v is None for v in values),net_model_return=float(np.mean(values)) if known else None,
        final_model_value_rial=initial_capital_rial*(1+float(np.mean(values))) if known else None,
        cash_unfilled_sleeves=sum(s['status']=='cash_unfilled' for s in summaries),
        fully_sized_sleeves=known_sum('allocation_filled_except_integer_rounding'),
        fully_liquidated_sleeves=known_sum('fully_liquidated'),
        net_cash_return=(known_sum('cash_final_rial')/initial_capital_rial-1
                         if all(s['fully_liquidated'] for s in summaries) else None),
        corporate_action_events=known_sum('corporate_action_events'),
        fees_rial=known_sum('fees_rial'),
        max_drawdown=float(-(augmented/augmented.cummax()-1).min()) if all_curve_known else None,
        max_loss_from_initial=max(0.,float(1-total.min()/initial_capital_rial)) if all_curve_known else None,
        economic_profit_verified=False,probability_calibrated=False,accepted_for_signals=False)
    if known and total.notna().iloc[-1] and not np.isclose(total.iloc[-1],summary['final_model_value_rial'],rtol=1e-9):
        raise ValueError('Sleeve mean and total capital do not reconcile')
    return summary,pd.DataFrame(dict(date=dates,equity_rial=total.to_numpy()))
