"""Replay selected pre-holdout public history without inferring rights membership."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from config import settings
from core.instrument_history import receipt_time
from core.price_revision import daily_request
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'rights-selected-history-v1'
MAX_ROWS = 10000
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def assess_history(code, start, end, observation):
    if (not is_canonical_iso_date(start) or not is_canonical_iso_date(end)
            or start < settings.RESEARCH_CONFIG['dev_start'] or end > settings.RESEARCH_CONFIG['holdout_start']
            or start >= end):
        raise ValueError('Historical scope must be entirely pre-holdout')
    code, _ = daily_request(code, start)
    result = {'version': VERSION, 'ins_code': code, 'start': start, 'end_exclusive': end,
              'status': 'unavailable', 'selected_rows': None, 'positive_volume_rows': None,
              'zero_volume_rows': None, 'invalid_volume_rows': None, 'invalid_traded_rows': None,
              'inconsistent_zero_volume_rows': None, 'duplicate_dates': None,
              'original_source_body_archived': False, 'historical_identity_verified': False,
              'historical_completeness_verified': False, 'accepted_for_signals': False}
    if observation is None:
        return result
    url = f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{code}/0'
    payload = observation.get('payload') if isinstance(observation, dict) else None
    rows = payload.get('closingPriceDaily') if isinstance(payload, dict) else None
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc'
            or observation.get('price_unit') != 'Rial' or observation.get('ins_code') != code
            or observation.get('url') != url or observation.get('start_date') != start
            or observation.get('end_date_exclusive') != end or observation.get('http_status') != 200
            or type(observation.get('response_bytes')) is not int
            or not 1 <= observation['response_bytes'] <= MAX_SOURCE_BYTES
            or not re.fullmatch(r'[0-9a-f]{64}', str(observation.get('body_sha256')))
            or observation.get('response_scope') != 'selected_native_rows_pre_holdout'
            or observation.get('source_body_archived') is not False
            or observation.get('unselected_numeric_rows_not_archived') is not True
            or not isinstance(payload, dict) or set(payload) != {'closingPriceDaily'}
            or not isinstance(rows, list) or len(rows) > MAX_ROWS
            or type(observation.get('selected_row_count')) is not int
            or observation['selected_row_count'] != len(rows)):
        raise ValueError('Selected history receipt or scope differs')
    received_at = receipt_time(observation.get('received_at'))
    if received_at < receipt_time(observation.get('requested_at')):
        raise ValueError('Selected history receipt order differs')
    # Dates and identities are validated before any price or volume is inspected.
    days = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('dEven')) not in (str, int):
            raise ValueError('Selected history requires a native date')
        raw = str(row['dEven'])
        if not re.fullmatch(r'[0-9]{8}', raw):
            raise ValueError('Selected history date differs')
        day = f'{raw[:4]}-{raw[4:6]}-{raw[6:]}'
        if not is_canonical_iso_date(day) or not start <= day < end:
            raise ValueError('Selected history contains an out-of-scope date')
        # The source may have been requested before midnight and received after
        # it. Only the Tehran receipt date bounds a dated row; same-day receipt
        # does not establish the final bar was complete or available intraday.
        if date.fromisoformat(day) > received_at.astimezone(TEHRAN_TZ).date():
            raise ValueError('Selected history date is after the Tehran receipt date')
        if type(row.get('insCode')) not in (str, int) or str(row['insCode']) != code:
            raise ValueError('Selected history has a different native identity')
        days.append(day)
    try:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError('Selected history cannot be serialized') from error
    if hashlib.sha256(body).hexdigest() != observation.get('selected_payload_sha256'):
        raise ValueError('Selected history payload digest differs')
    positive, zero, invalid, invalid_traded, invalid_zero = 0, 0, 0, 0, 0

    def number(value):
        if isinstance(value, bool):
            raise ValueError('Boolean is not a market number')
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError('Finite nonnegative market numbers required')
        return parsed

    for row in rows:
        volume = row.get('qTotTran5J')
        try:
            amount = number(volume)
            if amount != amount.to_integral_value():
                raise ValueError('Invalid volume')
            if amount == 0:
                zero += 1
                try:
                    if number(row.get('zTotTran')) != 0 or number(row.get('qTotCap')) != 0:
                        raise ValueError('Zero-volume totals conflict')
                except (ValueError, InvalidOperation, TypeError):
                    invalid_zero += 1
            else:
                positive += 1
                try:
                    first, low, high, last, ref, count, value = (number(row.get(key)) for key in (
                        'priceFirst', 'priceMin', 'priceMax', 'pDrCotVal', 'priceYesterday', 'zTotTran', 'qTotCap'))
                    if (min(first, low, high, last, ref, count, value) <= 0 or count != count.to_integral_value()
                            or not low <= first <= high or not low <= last <= high):
                        raise ValueError('Traded row values conflict')
                except (ValueError, InvalidOperation, TypeError):
                    invalid_traded += 1
        except (ValueError, InvalidOperation, TypeError):
            invalid += 1
    duplicates = len(days) - len(set(days))
    result.update(status='observed_with_gaps' if duplicates or invalid or invalid_traded or invalid_zero else 'observed',
                  selected_rows=len(rows),
                  positive_volume_rows=positive, zero_volume_rows=zero, invalid_volume_rows=invalid,
                  invalid_traded_rows=invalid_traded, inconsistent_zero_volume_rows=invalid_zero,
                  duplicate_dates=duplicates)
    return result


def candidate_starts(code, start, end, observation, *, min_gap_calendar_days=90):
    """Find starts of observed trading clusters; these are not confirmed rights issues."""
    if type(min_gap_calendar_days) is not int or not 1 <= min_gap_calendar_days <= 730:
        raise ValueError('A bounded integer calendar-day gap is required')
    checked = assess_history(code, start, end, observation)
    out = {'version': VERSION, 'ins_code': checked['ins_code'], 'status': checked['status'],
           'min_gap_calendar_days': min_gap_calendar_days, 'candidate_starts': [],
           'history_assessment': checked, 'issue_identity_verified': False,
           'calendar_completeness_verified': False, 'accepted_for_signals': False}
    if observation is None:
        return out
    # Duplicate dates or unknown volumes could hide a previous traded day.
    if checked['duplicate_dates'] or checked['invalid_volume_rows']:
        return out | {'status': 'unusable_for_gap_discovery'}
    traded = []
    for row in observation['payload']['closingPriceDaily']:
        if Decimal(str(row['qTotTran5J'])) <= 0:
            continue
        raw = str(row['dEven'])
        day = date.fromisoformat(f'{raw[:4]}-{raw[4:6]}-{raw[6:]}')
        traded.append(day)
    traded.sort()
    if not traded:
        return out
    out['candidate_starts'].append({'date': traded[0].isoformat(), 'previous_trade_date': None,
                                    'gap_calendar_days': None, 'left_censored': True})
    for previous, current in zip(traded, traded[1:]):
        gap = (current - previous).days
        if gap >= min_gap_calendar_days:
            out['candidate_starts'].append({'date': current.isoformat(), 'previous_trade_date': previous.isoformat(),
                                            'gap_calendar_days': gap, 'left_censored': False})
    return out
