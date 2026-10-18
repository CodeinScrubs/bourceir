"""کالبدشکافی حسابداری DIP محفوظ؛ همراهی کندل و زیان، علت اقتصادی قطعی نیست."""

import math

import pandas as pd

from core import market_rules

VERSION = 'dip-diagnostics-v1'


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def diagnose_dip_fold(prices, calendar, executions, decisions, picks, metrics, *, hold=1):
    """تمام اجرای فروش شامل خروج دیرهنگام/جزئی می‌ماند؛ خریدها به نتیجه آینده فیلتر نمی‌شوند."""
    if type(hold) is not int or hold < 1 or prices.duplicated(['symbol', 'date']).any():
        raise ValueError('افق و قیمت یکتا لازم است')
    days = list(calendar)
    if not days or days != sorted(set(days)):
        raise ValueError('تقویم مرتب و یکتا لازم است')
    position = {day: i for i, day in enumerate(days)}
    raw = prices.copy()
    flags = market_rules.lock_flags(raw)
    raw['locked_down'] = flags.locked_down
    rows = {(r['symbol'], r['date']): r for r in raw.to_dict('records')}
    entry_rows, exit_rows, latest = [], [], {}
    for record in executions.to_dict('records'):
        symbol, day = record['symbol'], record['date']
        if day not in position or record['side'] not in ('buy', 'sell'):
            raise ValueError('اجرای خارج تقویم یا سمت نامعتبر است')
        if record['side'] == 'buy':
            row = rows.get((symbol, day), {})
            low, high, last = (row.get(k) for k in ('low_raw', 'high_raw', 'close_raw'))
            close_position = None
            if all(_finite(v) for v in (low, high, last)) and 0 < low < high and low <= last <= high:
                close_position = (last - low) / (high - low)
            category = ('unknown_or_one_price' if close_position is None else 'close_bottom_quartile' if close_position <= .25
                        else 'close_top_quartile' if close_position >= .75 else 'close_middle_half')
            due_position = position[day] + hold
            due_day = days[due_position] if due_position < len(days) else None
            due_row = rows.get((symbol, due_day), {})
            due_ref = due_row.get('ref_price_raw')
            due_low, due_high = due_row.get('low_raw'), due_row.get('high_raw')
            lock_known = all(_finite(v) and v > 0 for v in (due_ref, due_low, due_high)) and due_low <= due_high
            entry = dict(symbol=symbol, entry_date=day, category=category, close_position=close_position,
                         entry_quantity=record['quantity'], entry_fee_rial=record['fee_rial'], planned_exit=due_day,
                         planned_exit_missing_row=not bool(due_row),
                         locked_down_on_planned_exit=bool(due_row.get('locked_down')) if lock_known else None)
            entry_rows.append(entry)
            latest[symbol] = entry
        else:
            if symbol not in latest:
                raise ValueError('فروش بدون خرید متناظر در دفتر برش است')
            if not _finite(record.get('realized_net_rial')):
                raise ValueError('سود محقق‌شده فروش نامعلوم است؛ قابل جمع نیست')
            entry = latest[symbol]
            due = entry['planned_exit']
            exit_rows.append(dict(**record, entry_date=entry['entry_date'], entry_category=entry['category'],
                planned_exit=due, delay_sessions=None if due is None else max(0, position[day] - position[due]),
                locked_down_on_planned_exit=entry['locked_down_on_planned_exit']))
    realized = sum(r['realized_net_rial'] for r in exit_rows)
    if not math.isclose(realized, metrics['realized_net_rial'], rel_tol=1e-9, abs_tol=.01):
        raise ValueError('جمع خروج‌ها با حساب محقق‌شده آشتی ندارد')
    total = metrics['total_return']
    total_pnl = None if total is None else total * metrics['initial_capital_rial']
    groups = {}
    for field in ('exit_kind', 'entry_category'):
        result = {}
        for row in exit_rows:
            key = row.get(field) or 'unknown'
            value = result.setdefault(key, dict(sell_executions=0, realized_net_rial=0., sell_fees_rial=0.))
            value['sell_executions'] += 1
            value['realized_net_rial'] += row['realized_net_rial']
            value['sell_fees_rial'] += row['fee_rial']
        groups[field] = result
    return dict(version=VERSION, selected_opportunities=len(picks), filled_entries=len(entry_rows),
        entry_categories=pd.Series([r['category'] for r in entry_rows], dtype='object').value_counts().to_dict(),
        decision_statuses=decisions['status'].value_counts().to_dict() if 'status' in decisions else {},
        realized_net_rial=realized, total_fold_proxy_pnl_rial=total_pnl,
        nonrealized_proxy_residual_rial=None if total_pnl is None else total_pnl - realized,
        all_execution_fees_rial=metrics['fees_rial'], groups=groups,
        time_exit_net_rial=sum(r['realized_net_rial'] for r in exit_rows if r.get('exit_kind') in ('time', 'time_late')),
        delayed_sell_executions=sum((r['delay_sessions'] or 0) > 0 for r in exit_rows),
        corporate_action_events=metrics['corporate_action_events'], open_positions=metrics['open_positions'],
        entries=entry_rows, exits=exit_rows, equity_known=total is not None,
        causal_distribution_claim_verified=False, category_and_exit_decompositions_overlap=True,
        limitations=['entry-day close is an ex-post diagnostic, never an entry feature',
                     'daily candle cannot identify a collapsing order queue or cause of selling',
                     'realized net already includes allocated buy fee; do not subtract fees twice',
                     'sell executions may be partial; they are not independent completed trades'])


def witness_summary(evidence, *, all_possible_days):
    """کرانِ همه روزهای ممکن؛ نمونه بررسی‌شده جای کل جامعه جا زده نمی‌شود."""
    if type(all_possible_days) is not int or all_possible_days < len(evidence):
        raise ValueError('مخرج روزهای ممکن ناسازگار است')
    confirmed = sum(e.get('complete') is True and e.get('event_confirmed') is True for e in evidence)
    disproved = sum(e.get('complete') is True and e.get('event_confirmed') is False for e in evidence)
    unknown = all_possible_days - confirmed - disproved
    return dict(all_possible_days=all_possible_days, inspected_days=len(evidence), confirmed_complete_witnesses=confirmed,
                disproved_complete_paths=disproved, unknown_days=unknown,
                confirmed_fraction_of_all=None if not all_possible_days else confirmed / all_possible_days,
                possible_fraction_upper=None if not all_possible_days else (confirmed + unknown) / all_possible_days,
                confidence_interval=False, own_order_fill_verified=False)
