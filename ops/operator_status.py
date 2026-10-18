"""نمای فقط‌خواندنی بهره‌بردار؛ داده حساب فقط محلی و مستقل از پژوهش بازار است."""

import json
import math

import pandas as pd

from config import settings
from core import market_rules
from core.account_risk import intraday_account_state, reserved_position_risk
from core.flow_filters import V4_FILTERS, filter_diagnostics_v4
from core.journal import summarize_executions
from core.market_summary import summarize_daily_market
from core.risk import RiskProfile, plan_trade
from core.screening import current_daily_row, expected_completed_session
from core.timeutils import now_tehran
from database.broker import broker_report
from database.paper import PaperRepository
from database.workbench import WorkbenchRepository


def _holding_scope(positions):
    counts = dict(in_scope=0, outside_scope=0, unknown_scope=0, micro_positions=0)
    rows = []
    for p in positions:
        eligible = p.get('research_scope_eligible')
        key = 'in_scope' if eligible is True else 'outside_scope' if eligible is False else 'unknown_scope'
        counts[key] += 1
        weight = p.get('weight_pct')
        micro = weight is not None and 0 <= weight <= .1
        counts['micro_positions'] += int(micro)
        rows.append(dict(symbol=p['symbol_fa'], instrument_type=p.get('instrument_type_code'), scope=key,
                         micro_position=micro, weight_pct=weight))
    return dict(**counts, positions=rows, micro_definition='reported portfolio weight <= 0.1%; descriptive, not a disposal recommendation')


def operator_status(db, metrics, market_frame, *, at=None):
    """هیچ هدف/حد زیان/بودجه خودساخته از روی نامزد ساخته نمی‌شود."""
    at = at or now_tehran()
    repo = WorkbenchRepository(db)
    calendar = db.get_market_calendar()
    expected = expected_completed_session(at, calendar=calendar)
    latest = None if metrics.empty else str(metrics.date.max())
    minimum_value = settings.RESEARCH_CONFIG['min_daily_value_rial']
    matches = []
    for name in V4_FILTERS:
        if metrics.empty:
            continue
        diagnostic = filter_diagnostics_v4(metrics, name)
        matched = diagnostic.loc[diagnostic.match].merge(
            metrics.reindex(columns=['symbol', 'date', 'value']), on=['symbol', 'date'], validate='one_to_one')
        for row in matched.to_dict('records'):
            value = pd.to_numeric(row['value'], errors='coerce')
            value = float(value) if pd.notna(value) and math.isfinite(value) and value >= 0 else None
            matches.append(dict(symbol=row['symbol'], date=row['date'], hypothesis=name,
                daily_value_rial=value, research_min_daily_value_rial=minimum_value,
                research_universe_eligible=None if value is None else value >= minimum_value))
    profile = repo.profile()
    account = broker_report(db)
    plans = repo.plans('actual')
    plan_by_symbol = {}
    for plan in plans:
        plan_by_symbol.setdefault(plan['symbol_fa'], plan)
    manual_rows = repo.market_rows()
    raw_by_symbol = manual_rows.set_index('symbol_fa').to_dict('index') if not manual_rows.empty else {}
    events = repo.executions('actual')
    risk = None
    if profile:
        risk_profile = RiskProfile(profile['capital_rial'], profile['max_loss_rial'])
        state = summarize_executions(events, risk_profile, {s: r['close_final_raw'] for s, r in raw_by_symbol.items()})
        snapshots = repo.latest_snapshots({m['symbol'] for m in matches} | set(state['positions']))
        risk = intraday_account_state(events, risk_profile, plans, snapshots, at=at, calendar=calendar)
    for match in matches:
        blockers, caps = ['فرضیه پژوهشی است؛ روش پذیرفته‌شده برای سیگنال نداریم'], None
        if match['research_universe_eligible'] is None:
            blockers.append('ارزش معاملات روز برای شرط حداقل جامعه پژوهش نامعلوم است')
        elif not match['research_universe_eligible']:
            blockers.append('ارزش معاملات روز کمتر از حداقل جامعه پژوهش است')
        plan = plan_by_symbol.get(match['symbol'])
        if latest != expected:
            blockers.append('روز فیلتر آخرین جلسه کامل مورد انتظار نیست')
        if not profile:
            blockers.append('پروفایل ریسک ثبت نشده است')
        elif not plan:
            blockers.append('برنامه دستی با ورود، حد زیان، هدف و بودجه ثبت نشده است')
        else:
            saved = json.loads(plan['calculation_json'])
            budget = saved.get('operator_inputs', {}).get('trade_loss_budget_rial')
            if budget is None:
                blockers.append('بودجه اولیه این برنامه قدیمی محفوظ نیست؛ محاسبه تازه لازم است')
            else:
                row = raw_by_symbol.get(match['symbol'], {})
                intraday = plan['horizon'] == 'intraday'
                current_state = risk['state'] if intraday else state
                reserved = risk['reserved_risk_rial'] if intraday else reserved_position_risk(state, plans)
                data_current = current_daily_row(row.get('date'), at=at, calendar=calendar)
                for symbol in current_state['positions']:
                    data_current &= current_daily_row(raw_by_symbol.get(symbol, {}).get('date'), at=at, calendar=calendar)
                complete = bool(profile['actual_history_complete']) and account is None
                lock = market_rules.lock_flags(pd.DataFrame([row])).iloc[0] if row else None
                try:
                    calculated = plan_trade(profile=risk_profile, entry_rial=plan['entry_rial'], stop_rial=plan['stop_rial'],
                        target_rial=plan['target_rial'], trade_loss_budget_rial=budget,
                        available_cash_rial=current_state['cash_rial'], equity_rial=current_state['equity_rial'],
                        reserved_risk_rial=reserved, daily_value_rial=row.get('value'), account_complete=complete,
                        data_current=bool(data_current and (risk['current'] if intraday else True)),
                        locked_up=bool(lock.locked_up) if lock is not None else False,
                        locked_down=bool(lock.locked_down) if lock is not None else False,
                        intraday=intraday, intraday_snapshot=snapshots.get(match['symbol']), at=at, calendar=calendar)
                    blockers.extend(calculated['blockers'])
                    # سقف دفتر ناقص، سقف حساب کارگزاری نیست.
                    caps = calculated['caps'] if complete else {k: None for k in calculated['caps']}
                except ValueError as error:
                    blockers.append(str(error))
        match.update(blockers=blockers, caps=caps, signal_quantity=None, accepted_for_signals=False,
                     linked_manual_plan_id=None if plan is None else plan['id'])
    papers = []
    paper_repo = PaperRepository(db)
    for p in paper_repo.portfolios():
        try:
            report = paper_repo.report(p['id'])
            papers.append(dict(name=p['name'], id=p['id'], cash_rial=report['ledger']['cash_rial'], equity_rial=report['equity_rial'],
                pnl_rial=report['total_net_rial'], return_pct=report['return_pct'],
                open_positions=len(report['ledger']['positions']), passive_positions=len((report['opening'] or {}).get('passive', [])),
                runtime_matches=report['runtime_matches'], integrity_unknown=any(report['integrity'].values()) or report['equity_rial'] is None,
                evaluated_at=report['evaluated_at'], error=None))
        except (ValueError, OSError) as error:
            papers.append(dict(name=p['name'], id=p['id'], error=str(error), equity_rial=None, pnl_rial=None))
    count = len(db.get_universe_symbols())
    pulse = None if market_frame.empty else summarize_daily_market(market_frame, expected_count=count)
    return dict(evaluated_at=at.isoformat(), expected_session=expected, filter_date=latest,
                filter_data_current=latest == expected, market=pulse, candidates=matches,
                accepted_candidates=0, paper_portfolios=papers,
                broker=None if account is None else dict(captured_at=account['captured_at'],
                    scope=_holding_scope(account['positions']), account_history_reconciled=False),
                account_risk_blockers=[] if risk is None else risk['evidence']['blockers'],
                accepted_for_signals=False, automatic_orders=False)
