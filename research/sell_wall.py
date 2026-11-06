"""Historical observations of lower-limit ask depletion, with post-event tape diagnostics."""

from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass

from config import settings
from core.minute_candles import _active_prints, reconcile_session
from core.queue_fall_study import (
    MAX_BOOK_BYTES,
    MAX_BOOK_ROWS,
    MAX_THRESHOLD_BYTES,
    _clock,
    _identity,
    _integer,
    _limits,
    _payload,
    book_request,
)
from core.quote_context import context_requests
from core.timeutils import is_canonical_iso_date

VERSION = 'sell-wall-observation-v1'
WALL_MIN_SHARES = 5_000_000
REMAINING_MAX_SHARES = 1_000_000
MIN_FLOOR_TRADE_SHARES = 1_000_000
ANCHOR_SECONDS = 120
EARLIEST_CLOCK = 90500
LATEST_CLOCK = 112500
AUDIT_LATENCIES = (5, 15)
AUDIT_SECONDS = 60


@dataclass(frozen=True)
class Quote:
    bid_price: int
    bid_shares: int
    bid_orders: int
    ask_price: int
    ask_shares: int
    ask_orders: int


def _seconds(stamp):
    return stamp // 10000 * 3600 + stamp // 100 % 100 * 60 + stamp % 100


def _hms(seconds):
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def _coherent_depth(depth, lower, upper):
    for side in ('bid', 'ask'):
        active = []
        empty_seen = False
        for row in depth:
            price, shares, orders = (getattr(row, f'{side}_{field}') for field in ('price', 'shares', 'orders'))
            if price or shares or orders:
                if empty_seen or not lower <= price <= upper or not 1 <= orders <= shares:
                    return False
                active.append(price)
            else:
                empty_seen = True
        if active != sorted(set(active), reverse=side == 'bid'):
            return False
    top = depth[0]
    return not (top.bid_price and top.ask_price and top.bid_price >= top.ask_price)


def _book_seconds(observation, threshold, code, day):
    limits_url = context_requests(code, day)[1]
    limits = _limits(_payload(threshold, code, day, limits_url, MAX_THRESHOLD_BYTES), code, day)
    native = _payload(observation, code, day, book_request(code, day), MAX_BOOK_BYTES).get('bestLimitsHistory')
    if not isinstance(native, list) or not 0 < len(native) <= MAX_BOOK_ROWS:
        raise ValueError('Missing or oversized best-limit history')
    revisions = defaultdict(dict)
    identity_verified = date_verified = True
    for row in native:
        if not isinstance(row, dict):
            raise ValueError('Invalid best-limit row')
        have_identity, have_date = _identity(row, code, day)
        identity_verified &= have_identity
        date_verified &= have_date
        level = _integer(row.get('number'), maximum=100)
        if not 1 <= level <= 5:
            continue
        stamp = _clock(row.get('hEven'))
        revision = _integer(row.get('refID'), maximum=2**63 - 1)
        state = Quote(*(_integer(row.get(k)) for k in
                        ('pMeDem', 'qTitMeDem', 'zOrdMeDem', 'pMeOf', 'qTitMeOf', 'zOrdMeOf')))
        depth = revisions[stamp, revision]
        if level in depth and depth[level] != state:
            raise ValueError('Conflicting levels at one best-limit revision')
        depth[level] = state

    times = [stamp for stamp, _ in limits]
    by_second = defaultdict(list)
    for (stamp, _), depth in revisions.items():
        if 1 not in depth:
            continue
        j = bisect_left(times, stamp)
        same_second_limit = j < len(times) and times[j] == stamp
        if not j or same_second_limit:
            by_second[stamp].append(None)
            continue
        lower, upper = limits[j-1][1]
        ordered = [row for _, row in sorted(depth.items())]
        state = (j-1, lower, upper, depth[1], len(depth) == 5) if _coherent_depth(ordered, lower, upper) else None
        by_second[stamp].append(state)
    if not by_second:
        raise ValueError('No explicit top-of-book rows')

    result = []
    for stamp, states in sorted(by_second.items()):
        usable = all(state is not None for state in states)
        if not usable or any(state[:-1] != states[0][:-1] for state in states):
            result.append((_seconds(stamp), None))
        else:
            result.append((_seconds(stamp), (*states[0][:-1], any(state[-1] for state in states))))
    return result, identity_verified, date_verified


def _first_depletion(quotes):
    anchors = deque()
    last_band = None
    wall_seen = False
    for at, quote in quotes:
        if not _seconds(EARLIEST_CLOCK) <= at <= _seconds(LATEST_CLOCK):
            continue
        if quote is None:
            anchors.clear()
            last_band = None
            continue
        band, lower, upper, top, complete_depth = quote
        if band != last_band:
            anchors.clear()
        last_band = band
        while anchors and at - anchors[0][0] > ANCHOR_SECONDS:
            anchors.popleft()
        remaining = top.ask_shares if top.ask_price == lower else 0
        if anchors and remaining <= REMAINING_MAX_SHARES:
            # Prefer the largest observed floor wall, then the latest in a tie.
            start, size, anchor_complete = max(anchors, key=lambda row: (row[1], row[0]))
            if remaining * 5 <= size:  # At least an 80% fall, without float threshold rounding.
                return (start, at, size, remaining, lower, upper, top, anchor_complete, complete_depth), wall_seen
        if top.bid_price == 0 and top.ask_price == lower and top.ask_shares >= WALL_MIN_SHARES:
            wall_seen = True
            anchors.append((at, top.ask_shares, complete_depth))
    return None, wall_seen


def _post_event_window(quotes, prints, at, lag):
    start, end = at + lag, at + lag + AUDIT_SECONDS
    observed = [(t, price, size) for t, price, size in prints if start <= t < end]
    ask = next(((t, q[3]) for t, q in quotes if start <= t < end and q is not None and q[3].ask_price > 0), None)
    shares = sum(size for _, _, size in observed)
    prices = [price for _, price, _ in observed]
    return dict(latency_seconds=lag, start=_hms(start), end_exclusive=_hms(end),
                observed_trades=len(observed), observed_shares=shares, low_rial=min(prices, default=None),
                high_rial=max(prices, default=None), first_trade_rial=prices[0] if prices else None,
                last_trade_rial=prices[-1] if prices else None,
                vwap_rial=sum(price*size for _, price, size in observed)/shares if shares else None,
                first_visible_ask=None if ask is None else dict(time=_hms(ask[0]), price_rial=ask[1].ask_price,
                                                               displayed_shares=ask[1].ask_shares))


def study_sell_wall(book, threshold, tape, daily, code, day):
    """Inspect one completed pre-holdout day without inferring order execution or feed coverage."""
    if not is_canonical_iso_date(day) or day >= settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('Sell-wall research requires a pre-holdout date')
    result = dict(version=VERSION, date=day, ins_code=code, status='unknown', event=None,
                  wall_observed=None, daily=None, phase_verified=False, complete_order_book_verified=False,
                  execution_verified=False, accepted_for_signals=False)
    try:
        _, daily_check = reconcile_session(code, day, daily, tape)
        result['daily'] = daily_check
        if daily_check['status'] != 'reconciled':
            result['status'] = daily_check['status']
            return result
        quotes, book_identity, book_date = _book_seconds(book, threshold, code, day)
        result.update(book_payload_identity_verified=book_identity, book_payload_date_verified=book_date,
                      usable_top_seconds=sum(quote is not None for _, quote in quotes),
                      ambiguous_or_invalid_top_seconds=sum(quote is None for _, quote in quotes))
        if not any(_seconds(EARLIEST_CLOCK) <= at <= _seconds(LATEST_CLOCK) and quote is not None for at, quote in quotes):
            return result | dict(status='no_usable_top_quotes_in_clock_window')
        picked, wall_seen = _first_depletion(quotes)
        result.update(status='depletion_observed' if picked is not None else 'depletion_not_observed', wall_observed=wall_seen)
        if picked is None:
            return result
        start, at, initial, remaining, lower, upper, top, anchor_complete, event_complete = picked
        active, _ = _active_prints(tape['response']['tradeHistory'], code, day)
        prints = [(t.hour*3600+t.minute*60+t.second, price, size) for t, price, size in active]
        floor_volume = sum(size for t, price, size in prints if start < t < at and price == lower)
        result['event'] = dict(anchor_time=_hms(start), time=_hms(at), elapsed_seconds=at-start,
            floor_rial=lower, ceiling_rial=upper, initial_wall_shares=initial, remaining_floor_ask_shares=remaining,
            observed_floor_trade_shares=floor_volume, min_floor_volume_met=floor_volume >= MIN_FLOOR_TRADE_SHARES,
            depletion_beyond_floor_trade_volume_shares=max(0, initial-remaining-floor_volume),
            five_levels_at_anchor=anchor_complete, five_levels_at_event=event_complete,
            event_ask_rial=top.ask_price or None, event_ask_shares=top.ask_shares,
            windows=[_post_event_window(quotes, prints, at, lag) for lag in AUDIT_LATENCIES])
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return result | dict(status='unknown', issue=str(error), event=None)
    return result
