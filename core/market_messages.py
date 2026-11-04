"""Bounded pre-holdout projection of public TSETMC instrument messages.

Native date is inspected before any row text. A dated message is a source claim,
not proof of issuer identity, an exhaustive feed, or a completed trading session.
"""

import json
import re
from datetime import datetime

from config import settings
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'market-message-projection-v1'
MAX_BYTES = 2_000_000
MAX_ROWS = 20_000
FIELDS = {'tseMsgIdn', 'dEven', 'hEven', 'tseTitle', 'tseDesc', 'flow'}
FALSE_FLAGS = ('native_identity_verified', 'historical_publication_verified',
               'complete_history_verified', 'trading_end_verified', 'no_reopening_verified',
               'source_correctness_verified', 'accepted_for_features', 'accepted_for_signals')


def message_request(ins_code, start, end_exclusive):
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if type(ins_code) is not str or not re.fullmatch(r'[1-9][0-9]{0,29}', ins_code):
        raise ValueError('A canonical instrument code is required')
    if (not all(is_canonical_iso_date(x) for x in (start, end_exclusive, cutoff))
            or not start < end_exclusive <= cutoff):
        raise ValueError('A bounded, pre-holdout date window is required')
    return f'https://cdn.tsetmc.com/api/Msg/GetMsgByInsCode/{ins_code}'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate source JSON key')
        result[key] = value
    return result


def _invalid(_value):
    raise ValueError('Non-finite source JSON number')


def _date(raw):
    if type(raw) is not int or not 10_000_000 <= raw <= 99_999_999:
        raise ValueError('Every native row needs a definite numeric date before selection')
    value = str(raw)
    day = f'{value[:4]}-{value[4:6]}-{value[6:]}'
    if not is_canonical_iso_date(day):
        raise ValueError('Invalid native message date')
    return day


def _receipt_day(received_at):
    try:
        stamp = datetime.fromisoformat(received_at)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('naive receipt')
        return stamp.astimezone(TEHRAN_TZ).date().isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('A timezone-aware message receipt is required') from exc


def _selected_row(row):
    if set(row) != FIELDS:
        raise ValueError('Unsupported message row schema')
    identity, clock, flow = row['tseMsgIdn'], row['hEven'], row['flow']
    if type(identity) is not int or not 0 < identity < 2**53:
        raise ValueError('A positive native message identifier is required')
    if (type(clock) is not int or not 0 <= clock <= 235959 or clock // 100 % 100 > 59
            or clock % 100 > 59):
        raise ValueError('Invalid native message clock')
    if type(flow) is not int or not 0 <= flow <= 99:
        raise ValueError('Invalid native market flow')
    for key, limit in (('tseTitle', 4000), ('tseDesc', 50000)):
        value = row[key]
        if (not isinstance(value, str) or len(value) > limit
                or any(0xD800 <= ord(c) <= 0xDFFF or c == '\x00' for c in value)):
            raise ValueError('Invalid native message text')
    return {key: row[key] for key in sorted(FIELDS)}


def project_messages(body, ins_code, start, end_exclusive, *, received_at):
    """Read source JSON in memory and expose only rows inside the declared window."""
    message_request(ins_code, start, end_exclusive)
    received_day = _receipt_day(received_at)
    if type(body) is not bytes or not 0 < len(body) <= MAX_BYTES:
        raise ValueError('A bounded native response is required')
    try:
        payload = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=_invalid)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid source response') from exc
    if not isinstance(payload, dict) or set(payload) != {'msg'} or not isinstance(payload['msg'], list):
        raise ValueError('Unsupported message response')
    if len(payload['msg']) > MAX_ROWS:
        raise ValueError('Message count exceeds bound')
    # First pass inspects only the native dates, never titles or descriptions.
    dated = []
    for row in payload['msg']:
        if not isinstance(row, dict):
            raise ValueError('Message row must be an object')
        day = _date(row.get('dEven'))
        if day > received_day:
            raise ValueError('Native message date is later than the source receipt day')
        if start <= day < end_exclusive:
            dated.append(row)
    rows = [_selected_row(row) for row in dated]
    ids = [row['tseMsgIdn'] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate selected message identifiers')
    return dict(version=VERSION, instrument_code=ins_code, start=start,
                end_exclusive=end_exclusive, cutoff=settings.RESEARCH_CONFIG['holdout_start'],
                messages=rows, **dict.fromkeys(FALSE_FLAGS, False))


def validate_projection(data, ins_code, start, end_exclusive, *, received_at):
    """Revalidate the selected rows, without claiming replay from discarded bytes."""
    message_request(ins_code, start, end_exclusive)
    received_day = _receipt_day(received_at)
    if (not isinstance(data, dict) or set(data) != {'version', 'instrument_code', 'start',
            'end_exclusive', 'cutoff', 'messages', *FALSE_FLAGS}
            or data['version'] != VERSION or data['instrument_code'] != ins_code
            or data['start'] != start or data['end_exclusive'] != end_exclusive
            or data['cutoff'] != settings.RESEARCH_CONFIG['holdout_start']
            or any(data[key] is not False for key in FALSE_FLAGS)
            or not isinstance(data['messages'], list) or len(data['messages']) > MAX_ROWS):
        raise ValueError('Invalid selected message projection')
    ids = []
    for row in data['messages']:
        if not isinstance(row, dict):
            raise ValueError('Selected message row is invalid')
        day = _date(row.get('dEven'))
        if not start <= day < end_exclusive or day > received_day:
            raise ValueError('Selected message date is outside the window')
        _selected_row(row)
        ids.append(row['tseMsgIdn'])
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate selected message identifiers')
    return data
