"""Dated public exchange market totals, used for descriptive instrument coverage."""

import hashlib
import json
import re
from datetime import date

from core.instrument_history import receipt_time
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, now_tehran

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 12_000


def market_day_request(day):
    if not is_canonical_iso_date(day) or day >= now_tehran().date().isoformat():
        raise ValueError('آمار یک روز کامل گذشته لازم است')
    return 'https://cdn.tsetmc.com/api/ClosingPrice/GetInstrmentsHistoryInDay/' + day.replace('-', '')


def _nonfinite(_value):
    raise ValueError('عدد نامتناهی در آمار بازار')


def _unique(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('کلید تکراری در آمار بازار')
    return data


def parse_market_day(day, observation):
    """Accept only a bound receipt. Null native dates are kept distinct from proof."""
    url = market_day_request(day)
    content = observation.get('content') if isinstance(observation, dict) else None
    if (not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES
            or observation.get('source') != 'tsetmc' or observation.get('url') != url
            or observation.get('requested_date') != day or observation.get('http_status') != 200
            or observation.get('response_bytes') != len(content)
            or observation.get('body_sha256') != hashlib.sha256(content).hexdigest()):
        raise ValueError('رسید آمار بازار با تاریخ، نشانی یا هش پاسخ سازگار نیست')
    received = receipt_time(observation.get('received_at')).astimezone(TEHRAN_TZ)
    if received.date() <= date.fromisoformat(day) or not receipt_time(observation.get('requested_at')) <= received <= now_tehran():
        raise ValueError('زمان مشاهده آمار بازار نامعتبر است')
    body = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique, parse_constant=_nonfinite)
    native = body.get('closingPriceDailyHistoryWithInstDetails') if isinstance(body, dict) else None
    if not isinstance(native, list) or len(native) > MAX_ROWS:
        raise ValueError('فهرست بومی آمار روز بازار نامعتبر است')
    rows, date_count = {}, 0
    requested_native = day.replace('-', '')
    for row in native:
        if not isinstance(row, dict):
            raise ValueError('قالب ردیف آمار بازار نامعتبر است')
        code, name = row.get('insCode'), row.get('lVal18AFC')
        if (type(code) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,29}', str(code))
                or not isinstance(name, str) or not normalize_symbol(name) or len(name) > 128):
            raise ValueError('شناسه یا نام نماد در آمار بازار نامعتبر است')
        code = str(code)
        native_day = row.get('dEven')
        if native_day not in (None, 0, '0', '') or type(native_day) is bool:
            if str(native_day) != requested_native:
                raise ValueError('تاریخ بومی آمار بازار با تاریخ درخواست متفاوت است')
            date_count += 1
        if code in rows:
            raise ValueError('کد ابزار تکراری در آمار یک روز بازار')
        volume, count = row.get('qTotTran5J'), row.get('zTotTran')
        if any(type(v) not in (int, float) or not 0 <= v < 2**53 or v % 1 for v in (volume, count)) or bool(volume) != bool(count):
            raise ValueError('تعداد یا حجم معامله در آمار بازار نامعتبر است')
        isin = row.get('instrumentID')
        if not isinstance(isin, str) or not re.fullmatch(r'IR[A-Z0-9]{10}', isin):
            isin = None
        rows[code] = dict(ins_code=code, symbol=normalize_symbol(name), isin=isin,
                          volume_shares=int(volume), trade_count=int(count), raw=row)
    return rows, dict(native_date_rows=date_count, native_date_verified=bool(rows) and date_count == len(rows),
                      received_at=observation['received_at'], row_count=len(rows), url=url,
                      body_sha256=observation['body_sha256'])
