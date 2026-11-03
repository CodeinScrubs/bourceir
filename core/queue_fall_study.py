"""Retrospective, non-trading study of quoted upper-limit demand and later trades.

The source is a historical quote feed, not a recording of every exchange state.
An observed event is conditional on that feed and is never proof of a fill.
"""

import hashlib
import json
from bisect import bisect_left, bisect_right
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from core.legacy_quotes import quote_request
from core.query_screener import trade_evidence
from core.quote_context import context_requests

VERSION = 'queue-fall-observation-v1'
MAX_BOOK_BYTES = 32 * 1024 * 1024
MAX_THRESHOLD_BYTES = 128 * 1024
MAX_BOOK_ROWS = 500_000


def book_request(ins_code, day):
    code, _ = quote_request(ins_code)
    parsed = date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError('Expected canonical YYYY-MM-DD')
    return f'https://cdn.tsetmc.com/api/BestLimits/{code}/{day.replace("-", "")}'


def trade_request(ins_code, day):
    code, _ = quote_request(ins_code)
    parsed = date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError('Expected canonical YYYY-MM-DD')
    return f'https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{code}/{day.replace("-", "")}/false'


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('Duplicate JSON key')
        out[key] = value
    return out


def _bad_constant(_value):
    raise ValueError('Non-finite JSON value')


def _aware(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Missing or invalid observation time') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Observation time needs a timezone')
    return parsed


def _payload(observation, code, day, url, limit):
    if not isinstance(observation, dict):
        raise ValueError('Missing source observation')
    body = observation.get('content')
    if (observation.get('source') != 'tsetmc' or observation.get('ins_code') != code
            or observation.get('url') != url or observation.get('http_status') != 200
            or not isinstance(body, bytes) or not 0 < len(body) <= limit
            or observation.get('response_bytes') != len(body)
            or observation.get('body_sha256') != hashlib.sha256(body).hexdigest()):
        raise ValueError('Source identity, address, status, length or hash mismatch')
    requested, received = (_aware(observation.get(k)) for k in ('requested_at', 'received_at'))
    if received < requested or requested.date() < date.fromisoformat(day):
        raise ValueError('Observation time contradicts the requested day')
    try:
        result = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_unique,
                            parse_constant=_bad_constant, parse_float=Decimal)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid source JSON') from exc
    if not isinstance(result, dict):
        raise ValueError('Source JSON must be an object')
    return result


def _integer(value, *, maximum=2**53):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Invalid integer')
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid integer') from exc
    if (not number.is_finite() or number < 0 or number > maximum
            or number != number.to_integral_value()):
        raise ValueError('Invalid integer')
    return int(number)


def _clock(value):
    n = _integer(value, maximum=235959)
    h, m, s = n // 10000, n // 100 % 100, n % 100
    if h > 23 or m > 59 or s > 59:
        raise ValueError('Invalid clock')
    return n


def _identity(row, code, day, *, allow_previous_day=False):
    raw_code, raw_day = row.get('insCode'), row.get('dEven')
    if raw_code not in (None, '', 0, '0') and str(raw_code) != code:
        raise ValueError('Payload instrument does not match request')
    if raw_day not in (None, '', 0, '0'):
        day_string = str(raw_day)
        if len(day_string) != 8 or not day_string.isascii() or not day_string.isdigit():
            raise ValueError('Invalid payload day')
        parsed = date(int(day_string[:4]), int(day_string[4:6]), int(day_string[6:]))
        if parsed != date.fromisoformat(day) and not (allow_previous_day and parsed < date.fromisoformat(day)):
            raise ValueError('Payload day does not match request')
    return raw_code not in (None, '', 0, '0'), raw_day not in (None, '', 0, '0')


def _limits(payload, code, day):
    rows = payload.get('staticThreshold')
    if not isinstance(rows, list) or not 0 < len(rows) <= 64:
        raise ValueError('Missing or oversized threshold history')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid threshold row')
        _identity(row, code, day, allow_previous_day=True)
        if str(row.get('dEven')) != day.replace('-', ''):
            continue
        stamp = _clock(row.get('hEven'))
        lower, upper = (_integer(row.get(key)) for key in ('psGelStaMin', 'psGelStaMax'))
        if lower <= 0 or lower > upper or stamp in result:
            raise ValueError('Invalid or ambiguous threshold')
        result[stamp] = (lower, upper)
    if not result:
        raise ValueError('No dated threshold for the requested day')
    return sorted(result.items())


def book_precheck(book, threshold, code, day):
    """Conservative tape-fetch screen, not a market event or a negative label.

    The union of all upper limits is intentional: a possibly out-of-order
    threshold change must result in an extra tape fetch rather than exclusion.
    """
    result = dict(state='unknown', candidate_rows=0, top_rows=0, issues=[],
                  source_completeness_verified=False)
    try:
        code = str(code)
        payload = _payload(book, code, day, book_request(code, day), MAX_BOOK_BYTES)
        limits_payload = _payload(threshold, code, day, context_requests(code, day)[1], MAX_THRESHOLD_BYTES)
        uppers = {pair[1] for _, pair in _limits(limits_payload, code, day)}
        rows = payload.get('bestLimitsHistory')
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_BOOK_ROWS:
            raise ValueError('Missing or oversized book history')
        seen = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid book row')
            _identity(row, code, day)
            if _integer(row.get('number'), maximum=100) != 1:
                continue
            result['top_rows'] += 1
            clock = _clock(row.get('hEven'))
            ref = _integer(row.get('refID'), maximum=2**63 - 1)
            values = tuple(_integer(row.get(k)) for k in
                           ('pMeDem', 'qTitMeDem', 'zOrdMeDem', 'pMeOf', 'qTitMeOf', 'zOrdMeOf'))
            if (clock, ref) in seen and seen[(clock, ref)] != values:
                raise ValueError('Conflicting top-of-book rows at the same source revision')
            seen[(clock, ref)] = values
            if values[0] in uppers and values[1] > 0 and values[2] > 0:
                result['candidate_rows'] += 1
        if not result['top_rows']:
            raise ValueError('No top-of-book records')
        result['state'] = 'tape_needed' if result['candidate_rows'] else 'no_upper_bid_observed'
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result['issues'].append(str(exc))
    return result


def trade_observation(observation, code, day):
    """Bind a preserved raw trade response to the existing trade-tape contract."""
    code = str(code)
    url = trade_request(code, day)
    response = _payload(observation, code, day, url, MAX_BOOK_BYTES)
    if not isinstance(response.get('tradeHistory'), list):
        raise ValueError('Missing trade history')
    return dict(source='tsetmc', price_unit='Rial', ins_code=code, requested_date=day,
                endpoint_is_grouped=False, captured_at=observation['received_at'],
                source_url=url, response=response, completeness_verified=False,
                body_sha256=observation['body_sha256'])


def _trade_rows(observation):
    by_number = {}
    for row in observation['response']['tradeHistory']:
        if row['canceled'] == 0:
            by_number[int(row['nTran'])] = (int(row['hEven']), _integer(row['pTran']), int(row['nTran']))
    return sorted(by_number.values())


def _witness(trades, clocks, queue_time, price):
    """Find a strictly later print; equal-second order is not observable."""
    first = bisect_right(clocks, queue_time)
    for clock, trade_price, number in trades[first:]:
        if trade_price * 100 <= price * 98:
            return dict(time=clock, price_rial=trade_price, trade_number=number)
    return None


def study_session(book, threshold, trades, daily):
    """Classify one symbol-day; errors and missing observations stay unknown.

    The two clock buckets are not a verified market phase, particularly after
    an intraday reopening. A session with no observation is not a negative.
    """
    result = dict(version=VERSION, state='unknown', observed=[], provisional=[], issues=[],
                  pre_09_clock_observed=False, post_09_clock_observed=False,
                  pre_09_clock_provisional=False, post_09_clock_provisional=False,
                  no_event_proven=False, book_completeness_verified=False,
                  phase_verified=False, execution_proven=False, accepted_for_signals=False)
    try:
        if not isinstance(daily, dict):
            raise ValueError('Missing daily candle')
        code = str(daily.get('ins_code'))
        day = daily.get('date')
        book_url = book_request(code, day)
        threshold_url = context_requests(code, day)[1]
        book_payload = _payload(book, code, day, book_url, MAX_BOOK_BYTES)
        threshold_payload = _payload(threshold, code, day, threshold_url, MAX_THRESHOLD_BYTES)
        limits = _limits(threshold_payload, code, day)
        rows = book_payload.get('bestLimitsHistory')
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_BOOK_ROWS:
            raise ValueError('Missing or oversized book history')
        expected_trade_url = trade_request(code, day)
        if not isinstance(trades, dict) or trades.get('source_url') != expected_trade_url:
            raise ValueError('Trade history URL mismatch')
        if _aware(trades.get('captured_at')).date() < date.fromisoformat(day):
            raise ValueError('Trade capture predates requested day')
        reconciliation = trade_evidence(trades, daily)
        result['trade_reconciliation'] = reconciliation
        if not reconciliation['complete'] or reconciliation['contradictory']:
            raise ValueError('Trade tape does not reconcile to the daily candle')
        parsed_trades = _trade_rows(trades)
        trade_clocks = [r[0] for r in parsed_trades]
        limit_clocks = [r[0] for r in limits]
        # The earliest quote for each (limit, quality, time bucket) is sufficient
        # to demonstrate existence; retain later quotes when quality changes.
        candidates = {}
        native_code = native_date = True
        seen = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid book row')
            has_code, has_date = _identity(row, code, day)
            native_code &= has_code
            native_date &= has_date
            if _integer(row.get('number'), maximum=100) != 1:
                continue
            stamp = _clock(row.get('hEven'))
            ref_id = _integer(row.get('refID'), maximum=2**63 - 1)
            values = tuple(_integer(row.get(k)) for k in
                           ('pMeDem', 'qTitMeDem', 'zOrdMeDem', 'pMeOf', 'qTitMeOf', 'zOrdMeOf'))
            key = (stamp, ref_id)
            if key in seen and seen[key] != values:
                raise ValueError('Conflicting top-of-book rows at the same source revision')
            seen[key] = values
            bid, bid_quantity, bid_orders, ask, ask_quantity, ask_orders = values
            if bid_quantity == 0 or bid_orders == 0:
                continue
            boundary = bisect_left(limit_clocks, stamp)
            same_second = boundary < len(limits) and limit_clocks[boundary] == stamp
            indexes = ([boundary - 1] if boundary else []) + ([boundary] if same_second else [])
            for index in indexes:
                lower, upper = limits[index][1]
                if bid != upper:
                    continue
                issues = []
                if same_second:
                    issues.append('threshold_change_at_same_second')
                if not 84500 <= stamp <= 123000:
                    issues.append('outside_current_regular_clock_window')
                if (ask, ask_quantity, ask_orders) != (0, 0, 0):
                    issues.append('nonempty_or_inconsistent_ask_at_upper_bid')
                if ask and not lower <= ask <= upper:
                    issues.append('ask_outside_daily_price_limits')
                bucket = 'pre_09_clock' if stamp < 90000 else 'post_09_clock'
                quality = 'provisional' if issues else 'observed'
                group = (upper, bucket, quality, tuple(issues))
                candidate = dict(queue_time=stamp, queue_price_rial=upper, bid_quantity=bid_quantity,
                                 bid_orders=bid_orders, ask_price_rial=ask, ask_quantity=ask_quantity,
                                 ask_orders=ask_orders, ref_id=ref_id, bucket=bucket, issues=issues)
                if group not in candidates or stamp < candidates[group]['queue_time']:
                    candidates[group] = candidate
        result['book_payload_identity_verified'] = native_code
        result['book_payload_date_verified'] = native_date
        result['top_book_candidates'] = len(candidates)
        for (upper, bucket, quality, _), candidate in candidates.items():
            witness = _witness(parsed_trades, trade_clocks, candidate['queue_time'], upper)
            if witness is not None:
                item = dict(candidate, later_trade=witness,
                            fall_percent=str((Decimal(upper - witness['price_rial']) * 100) / Decimal(upper)))
                result[quality].append(item)
                result[bucket + '_' + quality] = True
        for quality in ('observed', 'provisional'):
            result[quality].sort(key=lambda r: (r['queue_time'], r['later_trade']['time']))
        result['state'] = ('observed' if result['observed'] else
                           'provisional' if result['provisional'] else 'not_observed')
        result['issues'].append('Historical source completeness, market phase and intrasecond order are unverified')
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result['issues'].append(str(exc))
    return result


def summarize_sessions(rows):
    """One count per instrument-day; incomplete coverage cannot prove ranks."""
    groups, seen = {}, set()
    for row in rows:
        code, day = str(row['ins_code']), row['date']
        key = (code, day)
        if key in seen:
            raise ValueError('Duplicate instrument-day')
        seen.add(key)
        target = groups.setdefault(code, dict(ins_code=code, symbol=row.get('symbol'), attempted=0,
                                               observed=0, provisional=0, unknown=0, not_observed=0,
                                               pre_09_clock=0, post_09_clock=0,
                                               pre_09_provisional=0, post_09_provisional=0))
        state = row['result']['state']
        if state not in ('observed', 'provisional', 'unknown', 'not_observed'):
            raise ValueError('Invalid session state')
        target['attempted'] += 1
        target[state] += 1
        for bucket in ('pre_09_clock', 'post_09_clock'):
            target[bucket] += bool(row['result'][bucket + '_observed'])
        target['pre_09_provisional'] += bool(row['result']['pre_09_clock_provisional'])
        target['post_09_provisional'] += bool(row['result']['post_09_clock_provisional'])
    return sorted(groups.values(), key=lambda r: (-r['observed'], -r['provisional'], r['ins_code']))
