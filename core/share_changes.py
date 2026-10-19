"""تغییر تعداد سهام در منبع؛ تاریخ بومی، تعداد و استحقاق دارنده یکی نیستند."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'share-change-evidence-v1'
MAX_BYTES = 131072
MAX_ROWS = 512
FALSE_FLAGS = ('historical_membership_verified', 'historical_publication_verified', 'complete_history_verified',
               'holder_credit_verified', 'actual_entitlements_verified', 'feature_eligible',
               'accepted_for_signals', 'profitability_proven')
OBSERVATION_KEYS = {'source', 'ins_code', 'url', 'requested_at', 'received_at', 'response_bytes', 'body_sha256'}
SOURCE_FIELDS = {'idn','insCode','dEven','numberOfShareOld','numberOfShareNew','lVal30','lVal18AFC'}


def share_change_request(ins_code):
    code = str(ins_code) if type(ins_code) in (str, int) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,29}', code):
        raise ValueError('شناسه عددی مثبت بومی لازم است')
    return code, 'https://cdn.tsetmc.com/api/Instrument/GetInstrumentShareChange/' + code


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError('زمان دارای منطقه زمانی لازم است') from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('زمان دارای منطقه زمانی لازم است')
    return result.astimezone(TEHRAN_TZ)


def window(start, end_exclusive, cutoff):
    if (any(not is_canonical_iso_date(d) for d in (start, end_exclusive, cutoff))
            or not start < end_exclusive <= cutoff):
        raise ValueError('پنجره مرتب پیش از مرز مهروموم لازم است')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('کلید تکراری در پاسخ JSON')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('عدد نامتناهی در JSON مجاز نیست')


def _day(value):
    if type(value) not in (str, int) or not re.fullmatch(r'[0-9]{8}', str(value)):
        return None
    raw = str(value)
    day = raw[:4] + '-' + raw[4:6] + '-' + raw[6:]
    return day if is_canonical_iso_date(day) else None


def _count(value):
    if (type(value) not in (str, int) or len(str(value)) > 80 or not re.fullmatch(
            r'[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?', str(value))):
        return None
    try:
        number = Decimal(str(value))
        return int(number) if 0 < number <= 10**18 and number == number.to_integral_value() else None
    except (InvalidOperation, ValueError):
        return None


def _rational(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def assess_share_changes(observation, content, ins_code, *, start, end_exclusive, cutoff):
    """ابتدا تاریخ هر ردیف؛ رقم‌های بیرون پنجره/مهروموم حتی به Decimal تبدیل نمی‌شوند."""
    code, url = share_change_request(ins_code)
    window(start, end_exclusive, cutoff)
    result = dict(version=VERSION, status='unavailable', records=[], events=[], row_count=None,
        excluded_sealed_rows=0, excluded_outside_window_rows=0, invalid_date_rows=0,
        duplicate_rows=0, conflicting_dates=[], original_publication_time=None, **dict.fromkeys(FALSE_FLAGS, False))
    if observation is None:
        if content is not None:
            raise ValueError('بدنه بدون رسید بومی مجاز نیست')
        return result
    if (not isinstance(observation, dict) or set(observation) != OBSERVATION_KEYS
            or observation['source'] != 'tsetmc' or observation['ins_code'] != code or observation['url'] != url
            or type(observation['response_bytes']) is not int or not isinstance(content, bytes)
            or not 0 < len(content) <= MAX_BYTES or observation['response_bytes'] != len(content)
            or hashlib.sha256(content).hexdigest() != observation['body_sha256']):
        raise ValueError('بدنه، هویت یا هش رسید بومی ناسازگار است')
    if stamp(observation['received_at']) < stamp(observation['requested_at']):
        raise ValueError('دریافت پیش از درخواست است')
    # ارقام JSON به متن حفظ می‌شوند؛ تفسیر مالی فقط پس از کنترل تاریخ همان ردیف است.
    try:
        payload = json.loads(content.decode('utf-8'), parse_int=str, parse_float=str,
            parse_constant=_constant, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError('ساختار JSON پاسخ معتبر نیست') from error
    if not isinstance(payload,dict) or set(payload)!={'instrumentShareChange'}:
        raise ValueError('قرارداد ریشه پاسخ پشتیبانی نشده است')
    rows = payload['instrumentShareChange']
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('فهرست محدود تغییر تعداد سهام لازم است')
    result['row_count'] = len(rows)
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        day = _day(row.get('dEven')) if isinstance(row, dict) else None
        if day is None:
            result['invalid_date_rows'] += 1
            result['records'].append(dict(source_index=index, status='unknown_native_date'))
            continue
        if day >= cutoff:
            result['excluded_sealed_rows'] += 1
            continue
        if not start <= day < end_exclusive:
            result['excluded_outside_window_rows'] += 1
            continue
        identity = row.get('insCode')
        record = dict(source_index=index, source_change_date=day)
        if set(row)-SOURCE_FIELDS:
            record['status'] = 'unsupported_native_fields'
        elif type(identity) not in (str, int) or str(identity) != code:
            record['status'] = 'native_identity_conflict'
        else:
            before, after = _count(row.get('numberOfShareOld')), _count(row.get('numberOfShareNew'))
            if before is None or after is None:
                record['status'] = 'unknown_share_counts'
            else:
                record.update(status='coherent_observed', source_old_shares=before, source_new_shares=after,
                    share_multiplier=_rational(Fraction(after, before)),
                    share_change_ratio=_rational(Fraction(after-before, before)))
                groups[day].append(record)
        result['records'].append(record)
    for day, records in sorted(groups.items()):
        facts = {(r['source_old_shares'], r['source_new_shares']) for r in records}
        if len(facts) > 1:
            result['conflicting_dates'].append(day)
            continue
        before, after = next(iter(facts))
        result['duplicate_rows'] += len(records)-1
        result['events'].append(dict(source_change_date=day, source_old_shares=before, source_new_shares=after,
            share_multiplier=_rational(Fraction(after, before)), share_change_ratio=_rational(Fraction(after-before, before)),
            source_indices=[r['source_index'] for r in records]))
    counts = Counter(r['status'] for r in result['records'])
    if result['conflicting_dates'] or counts['native_identity_conflict']:
        result['status'] = 'conflicting_response'
    elif result['invalid_date_rows'] or counts['unknown_share_counts'] or counts['unsupported_native_fields']:
        result['status'] = 'incomplete_response'
    elif result['events']:
        result['status'] = 'coherent_observed'
    else:
        result['status'] = 'no_observed_event_in_window'
    return result


def compare_capital_ratio(assessment, facts, source_date):
    """تطبیق نسبت دو منبع؛ هم‌هویتی حقوقی رخداد، طبقه سهم و اعتبار دارنده تأیید نمی‌شوند."""
    if not is_canonical_iso_date(source_date):
        raise ValueError('تاریخ بومی صریح برای مقایسه لازم است')
    result = dict(status='not_comparable', source_change_date=source_date, share_multiplier=None,
        capital_multiplier=None, same_corporate_event_verified=False,
        ordinary_share_class_verified=False, **dict.fromkeys(FALSE_FLAGS, False))
    if assessment['status'] != 'coherent_observed' or facts.get('status') != 'supported_primary_fact':
        return result
    matches = [e for e in assessment['events'] if e['source_change_date'] == source_date]
    if len(matches) != 1:
        result['status'] = 'missing_explicit_native_event'
        return result
    ratio = facts['capital_multiplier']
    if (set(ratio) != {'numerator', 'denominator'} or any(type(v) is not int or v <= 0 for v in ratio.values())):
        raise ValueError('نسبت دقیق سرمایه معتبر نیست')
    share = matches[0]['share_multiplier']
    same = Fraction(**share) == Fraction(**ratio)
    result.update(status='source_ratios_agree' if same else 'source_ratios_differ',
        share_multiplier=share, capital_multiplier=ratio)
    return result
