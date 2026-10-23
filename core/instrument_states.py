"""Dated state observations, not reconstructed sessions or executable trading states.

The endpoint can return zero native identity/date and a sentinel-like hEven=1
beside a different realHeven. Neither a dated URL nor a plausible clock proves
when a state became effective, why it changed, or whether trading was possible.
"""

import hashlib
import json
import re
from datetime import datetime

from core.price_revision import daily_request
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'instrument-state-observation-v1'
MAX_BYTES = 131072
MAX_ROWS = 4096
MAX_JSON_DEPTH = 8
SOURCE_FIELDS = {'idn', 'dEven', 'hEven', 'insCode', 'lVal18AFC', 'lVal30',
                 'cEtaval', 'realHeven', 'underSupervision', 'cEtavalTitle'}
FALSE_FLAGS = ('historical_availability_verified', 'forward_availability_verified',
    'first_publication_time_verified', 'historical_membership_verified', 'complete_history_verified',
    'full_session_coverage_verified', 'precise_transition_clock_verified', 'transition_order_verified',
    'state_semantics_verified', 'suspension_cause_verified', 'tradability_verified',
    'independent_source_correctness_verified', 'usable_for_session_reconstruction', 'executable',
    'feature_eligible', 'accepted_for_features', 'accepted_for_signals', 'economic_profit_verified',
    'profitability_proven')


def state_request(ins_code, day):
    """Validate identity/date before constructing the one supported public URL."""
    code, _ = daily_request(ins_code, day)
    return code, f'https://cdn.tsetmc.com/api/MarketData/GetInstrumentState/{code}/{day.replace("-", "")}'


def _clock(value):
    if not isinstance(value, str):
        raise ValueError('زمان دریافت باید ISO با منطقه زمانی باشد')
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError('naive')
        return result.astimezone(TEHRAN_TZ)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('زمان دریافت باید ISO با منطقه زمانی باشد') from exc


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('کلید تکراری در JSON وضعیت نماد')
        result[key] = value
    return result


def _non_integer(_):
    raise ValueError('اعداد پاسخ وضعیت باید صحیح و متناهی باشند')


def _json_integer(value):
    # Native instrument identifiers can exceed binary64 precision; retain exact ints.
    if len(value.lstrip('-')) > 30:
        raise ValueError('عدد پاسخ وضعیت بیش از محدوده قرارداد است')
    return int(value)


def _structure(value, depth=0):
    if depth > MAX_JSON_DEPTH or type(value) is bool:
        raise ValueError('عمق یا مقدار بولی JSON وضعیت مجاز نیست')
    if isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        # json.loads accepts escaped lone surrogates; UTF-8 evidence files do not.
        raise ValueError('رشته JSON وضعیت شامل نویسه یونیکد ناقص است')
    if isinstance(value, dict):
        for key, child in value.items():
            _structure(key, depth + 1)
            _structure(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _structure(child, depth + 1)


def _native_identity(value, expected):
    if value is None:
        return 'request_url_only'
    if type(value) not in (str, int) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,29})', str(value)):
        raise ValueError('شناسه بومی وضعیت نامعتبر است')
    if str(value) == '0':
        return 'request_url_only'
    return 'response_matches_request' if str(value) == expected else 'source_conflict'


def _native_date(value, expected):
    if value is None or type(value) in (str, int) and str(value) == '0':
        return 'request_url_only'
    if type(value) not in (str, int) or not re.fullmatch(r'[0-9]{8}', str(value)):
        raise ValueError('تاریخ بومی وضعیت نامعتبر است')
    raw = str(value)
    day = f'{raw[:4]}-{raw[4:6]}-{raw[6:]}'
    if not is_canonical_iso_date(day):
        raise ValueError('تاریخ بومی وضعیت نامعتبر است')
    return 'response_matches_request' if day == expected else 'source_conflict'


def _integer(value):
    if value is not None and (type(value) is not int or not 0 <= value < 2**53):
        raise ValueError('عدد بومی وضعیت نامعتبر است')


def _text(value, *, required=False, limit=256):
    if value is None and not required:
        return
    if (not isinstance(value, str) or len(value) > limit or required and not value.strip()
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)):
        raise ValueError('متن بومی وضعیت نامعتبر است')


def _native_clock(value):
    _integer(value)
    if value is not None and (value > 235959 or value // 100 % 100 > 59 or value % 100 > 59):
        raise ValueError('ساعت عددی وضعیت بیرون محدوده HHMMSS است')
    return dict(raw=value, missing=value is None, zero=value == 0, sentinel_candidate=value == 1,
                plausible_hhmmss=value is not None and value not in (0, 1),
                precise_transition_clock_verified=False, transition_timestamp=None)


def _row(native, index, code, day):
    if not isinstance(native, dict) or set(native) - SOURCE_FIELDS:
        raise ValueError('ساختار ردیف بومی وضعیت پشتیبانی نشده است')
    _text(native.get('cEtaval'), required=True, limit=32)
    for key in ('lVal18AFC', 'lVal30', 'cEtavalTitle'):
        _text(native.get(key))
    for key in ('idn', 'underSupervision'):
        _integer(native.get(key))
    identity = _native_identity(native.get('insCode'), code)
    date = _native_date(native.get('dEven'), day)
    grade = ('source_conflict' if 'source_conflict' in (identity, date)
             else 'request_url_only' if 'request_url_only' in (identity, date) else 'response_matches_request')
    clocks = {key: _native_clock(native.get(key)) for key in ('hEven', 'realHeven')}
    left, right = (clocks[key]['raw'] for key in ('hEven', 'realHeven'))
    return dict(source_index=index, raw=dict(native), evidence_grade=grade,
        identity_attestation=identity, date_attestation=date,
        native_identity_verified=identity == 'response_matches_request',
        native_date_verified=date == 'response_matches_request',
        state_code_raw=native['cEtaval'], state_code=native['cEtaval'].strip(),
        code_recognition='unmapped', state_meaning=None, state_semantics_verified=False,
        clocks=clocks, clocks_differ=None if left is None or right is None else left != right,
        transition_timestamp=None, tradability_verified=False)


def parse_state_observation(observation, ins_code, day, *, requested_after=None, received_by=None):
    """Parse exact public bytes and preserve source order; no effective-state inference.

    Missing/zero native identity or date means request-URL-only evidence. Explicit
    well-formed mismatches invalidate the aggregate while retaining diagnostic rows.
    Malformed input raises ValueError instead of becoming an empty/open session.
    """
    code, url = state_request(ins_code, day)
    if not isinstance(observation, dict):
        raise ValueError('شاهد بومی وضعیت موجود نیست')
    body = observation.get('content')
    if (observation.get('source') != 'tsetmc' or observation.get('ins_code') != code
            or observation.get('url') != url or observation.get('requested_date') != day
            or 'observation_kind' in observation and observation['observation_kind'] != 'instrument_state'
            or not isinstance(body, bytes) or not 0 < len(body) <= MAX_BYTES
            or type(observation.get('response_bytes')) is not int or observation['response_bytes'] != len(body)
            or observation.get('body_sha256') != hashlib.sha256(body).hexdigest()):
        raise ValueError('هویت، نشانی یا بایت شاهد وضعیت ناسازگار است')
    requested, received = (_clock(observation.get(k)) for k in ('requested_at', 'received_at'))
    if received < requested or day > received.date().isoformat():
        raise ValueError('تاریخ درخواست یا ترتیب دریافت وضعیت نامعتبر است')
    if (requested_after is None) != (received_by is None):
        raise ValueError('هر دو مرز واقعی دریافت وضعیت لازم‌اند')
    if requested_after is not None and not _clock(requested_after) <= requested <= received <= _clock(received_by):
        raise ValueError('زمان منبع بیرون بازه واقعی دریافت است')
    try:
        payload = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_unique,
                             parse_constant=_non_integer, parse_float=_non_integer, parse_int=_json_integer)
        _structure(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise ValueError('بایت وضعیت JSON معتبر نیست') from exc
    if not isinstance(payload, dict) or set(payload) != {'instrumentState'}:
        raise ValueError('ریشه بومی وضعیت پشتیبانی نشده است')
    native_rows = payload['instrumentState']
    if not isinstance(native_rows, list) or len(native_rows) > MAX_ROWS:
        raise ValueError('فهرست محدود وضعیت نماد لازم است')
    rows = [_row(native, index, code, day) for index, native in enumerate(native_rows)]
    native_identity = bool(rows) and all(row['native_identity_verified'] for row in rows)
    native_date = bool(rows) and all(row['native_date_verified'] for row in rows)
    conflicts = [dict(source_index=row['source_index'], identity=row['identity_attestation'],
                      date=row['date_attestation']) for row in rows if row['evidence_grade'] == 'source_conflict']
    status = ('source_conflict' if conflicts else 'observed_empty' if not rows
              else 'observed_states' if native_identity and native_date else 'request_url_only')
    seen, duplicates = set(), []
    for row in rows:
        key = json.dumps(row['raw'], sort_keys=True, ensure_ascii=False)
        if key in seen:
            duplicates.append(row['source_index'])
        seen.add(key)
    return dict(version=VERSION, status=status, ins_code=code, requested_date=day,
        requested_date_basis='request_url', source='tsetmc', url=url, body_sha256=observation['body_sha256'],
        response_bytes=len(body), requested_at=requested.isoformat(), received_at=received.isoformat(),
        evidence_grade='source_conflict' if conflicts else 'response_matches_request'
            if native_identity and native_date else 'request_url_only',
        row_count=len(rows), rows=rows, source_conflicts=conflicts, duplicate_source_indices=duplicates,
        source_order_preserved=True, native_identity_verified=native_identity, native_date_verified=native_date,
        native_record_verified=native_identity and native_date, session_state=None, suspension_cause=None,
        original_publication_time=None, **dict.fromkeys(FALSE_FLAGS, False))
