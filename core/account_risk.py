"""Whole-journal risk snapshots. Quotes are observations, never guaranteed liquidation prices."""

from config import settings
from core.journal import summarize_executions
from core.risk import finite_number
from core.screening import current_snapshot
from core.timeutils import is_market_open, now_tehran


def reserved_position_risk(state, plans):
    """Reserve the loss from the *same* marks to linked stop scenarios, including costs."""
    if state['equity_rial'] is None:
        return None
    by_id = {p['id']: p for p in plans}
    reserved = 0.0
    slip = settings.BACKTEST_CONFIG['default_slippage_bps'] / 10000
    for position in state['positions'].values():
        linked = [by_id.get(key) for key in position['plan_ids']]
        stop = min(p['stop_rial'] for p in linked) if linked and all(linked) else None
        stop_value = position['quantity'] * stop * (1 - settings.FEES['sell']) * (1 - slip) if stop else 0
        reserved += max(0, position['liquidation_value_rial'] - stop_value)
    return reserved


def intraday_account_state(events, profile, plans, snapshots, *, at=None):
    """Value every open holding using fresh last/bid observations from one evaluation clock.

    The lower of last and best bid is a conservative mark, not a fill for the entire position.
    Missing/stale/inconsistent observations block new risk; yesterday's mark is never a fallback.
    A separate book clock, trading-state feed and actual queue priority remain unverified.
    """
    at = at or now_tehran()
    if at.tzinfo is None:
        raise ValueError('زمان باید دارای منطقه زمانی باشد')
    holdings = summarize_executions(events, profile, {})['positions']
    marks, observations, blockers = {}, {}, []
    if not is_market_open(at):
        blockers.append('بازار خارج از ساعات معاملات است')
    for symbol, position in holdings.items():
        snapshot = snapshots.get(symbol)
        problems = []
        if not snapshot or any(not current_snapshot(snapshot.get(key), at)
                               for key in ('provider_timestamp', 'captured_at')):
            problems.append('قیمت تازه پوزیشن موجود تأیید نشده است')
        else:
            try:
                last, bid, lower, upper = [finite_number(snapshot.get(key)) for key in
                    ('last_price', 'bid_price_1', 'price_limit_down', 'price_limit_up')]
                finite_number(snapshot.get('bid_vol_1'))
                finite_number(snapshot.get('bid_count_1'))
                if not lower <= last <= upper or not lower <= bid <= upper:
                    problems.append('قیمت یا تقاضای پوزیشن موجود با دامنه امروز سازگار نیست')
                if finite_number(snapshot.get('sell_queue_volume'), positive=False) > 0:
                    problems.append('صف فروش پوزیشن موجود؛ امکان خروج تأیید نشده است')
                ask = finite_number(snapshot.get('ask_price_1'), positive=False)
                if ask > 0 and (bid > ask or not lower <= ask <= upper):
                    problems.append('مظنه‌های پوزیشن موجود ناسازگارند')
                if not problems:
                    marks[symbol] = min(last, bid)
            except ValueError:
                problems.append('قیمت، تقاضا یا دامنه پوزیشن موجود نامعلوم است')
        blockers.extend(f'{symbol}: {problem}' for problem in problems)
        observations[symbol] = dict(quantity=position['quantity'], snapshot=dict(snapshot) if snapshot else None,
                                    mark_rial=marks.get(symbol), blockers=problems)
    state = summarize_executions(events, profile, marks)
    reserved = reserved_position_risk(state, plans)
    evidence = dict(version='intraday-account-v1', evaluated_at=at.isoformat(), observations=observations,
                    mark_basis='min_last_and_best_bid_net_sell_fee', equity_rial=state['equity_rial'],
                    reserved_risk_rial=reserved, equity_floor_rial=profile.equity_floor_rial,
                    loss_limit_reached=state['loss_limit_reached'], blockers=blockers,
                    independent_book_timestamp_verified=False, trading_state_verified=False,
                    whole_position_liquidation_proven=False)
    return dict(state=state, reserved_risk_rial=reserved, current=not blockers, evidence=evidence)
