"""Typed dated publisher observations; observed zero activity is not an executable candle."""
import hashlib
import json
import math
from datetime import datetime

import pandas as pd

from core.price_quality import price_quality
from core.price_revision import SOURCE_FIELDS, daily_request
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'dated-daily-publication-v1'
MAX_BYTES = 131072
CLIENT_FIELDS = {f'{side}_{party.lower()}_{field.lower()}': f'{side}_{party}_{field}'
                 for side in ('buy', 'sell') for party in ('I', 'N') for field in ('Volume', 'Value', 'Count')}


def publication_request(ins_code, day, kind):
    code, url = daily_request(ins_code, day)
    if kind not in ('price', 'client'):
        raise ValueError('نوع مشاهده روزانه باید price یا client باشد')
    if kind == 'client':
        url = f'https://cdn.tsetmc.com/api/ClientType/GetClientTypeHistory/{code}/{day.replace("-", "")}'
    return code, url


def _clock(value):
    if not isinstance(value, str):
        raise ValueError('زمان دریافت باید ISO با منطقه زمانی باشد')
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError('naive')
        return result.astimezone(TEHRAN_TZ)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('زمان دریافت باید ISO با منطقه زمانی باشد') from exc


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('کلید تکراری در JSON منبع')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('عدد نامتناهی در JSON منبع')


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('عدد نامتناهی در JSON منبع')
    return result


def _numeric(value, *, integer=False, optional=False):
    if optional and value is None:
        return None
    if (type(value) not in (int, float) or not 0 <= value < 2**53 or not math.isfinite(value)
            or integer and value % 1):
        raise ValueError('مقدار عددی روزانه نامعتبر است')
    return value


def parse_publication(observation, ins_code, day, kind, *, requested_after=None, received_by=None):
    """Native identity/date are mandatory. Publication time and independent truth stay unverified."""
    code, url = publication_request(ins_code, day, kind)
    if not isinstance(observation, dict):
        raise ValueError('شاهد بومی روزانه موجود نیست')
    body = observation.get('content')
    if (observation.get('source') != 'tsetmc' or observation.get('price_unit') != 'Rial'
            or observation.get('ins_code') != code or observation.get('url') != url
            or observation.get('requested_date') != day or observation.get('kind') != kind
            or not isinstance(body, bytes) or not 0 < len(body) <= MAX_BYTES
            or type(observation.get('response_bytes')) is not int or observation['response_bytes'] != len(body)
            or observation.get('body_sha256') != hashlib.sha256(body).hexdigest()):
        raise ValueError('هویت، نشانی، واحد یا بایت شاهد روزانه ناسازگار است')
    requested, received = (_clock(observation.get(k)) for k in ('requested_at', 'received_at'))
    if received < requested or not is_canonical_iso_date(day) or day > received.date().isoformat():
        raise ValueError('تاریخ روزانه یا ترتیب دریافت نامعتبر است')
    if (requested_after is None) != (received_by is None):
        raise ValueError('هر دو مرز واقعی دریافت روزانه لازم‌اند')
    if requested_after is not None and not _clock(requested_after) <= requested <= received <= _clock(received_by):
        raise ValueError('زمان منبع بیرون بازه واقعی دریافت است')
    try:
        payload = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_unique,
                             parse_constant=_constant, parse_float=_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('بایت روزانه JSON معتبر نیست') from exc
    native = payload.get('closingPriceDaily' if kind == 'price' else 'clientType') if isinstance(payload, dict) else None
    if not isinstance(native, dict):
        raise ValueError('ردیف بومی روزانه موجود نیست')
    native_day = native.get('dEven' if kind == 'price' else 'recDate')
    if (type(native.get('insCode')) not in (str, int) or str(native['insCode']) != code
            or type(native_day) not in (str, int) or str(native_day) != day.replace('-', '')):
        raise ValueError('هویت یا تاریخ داخل پاسخ با درخواست برابر نیست')
    base = dict(version=VERSION, kind=kind, ins_code=code, date=day, native_identity_verified=True,
        native_date_verified=True, first_publication_time_verified=False,
        independent_source_correctness_verified=False, full_session_coverage_verified=False,
        executable=False, accepted_for_signals=False)
    fields = SOURCE_FIELDS if kind == 'price' else CLIENT_FIELDS
    if kind == 'price':
        quantity = {k: _numeric(native.get(SOURCE_FIELDS[k]), integer=k != 'value')
                    for k in ('volume', 'value', 'trade_count')}
        if quantity['volume'] == quantity['value'] == quantity['trade_count'] == 0:
            return dict(base, status='observed_zero_activity', row=None)
    row = dict(date=day)
    for column, field in fields.items():
        row[column] = _numeric(native.get(field), integer=(column in ('volume', 'trade_count')
            or column.endswith(('_volume', '_count'))), optional=kind == 'client')
    if kind == 'price':
        if any(row[k] <= 0 for k in SOURCE_FIELDS) or price_quality(pd.DataFrame([row]))[1].iloc[0].any():
            raise ValueError('کندل بومی با قرارداد عددی قیمت روزانه سازگار نیست')
        return dict(base, status='valid_price', row=row)
    return dict(base, status='observed_client', row=row)
