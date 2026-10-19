"""مظنه با دامنه هم‌روز و هویت متقاطع؛ شاهد محلی، نه تضمین اجرا یا ساعت مستقل عمق."""

import base64
import hashlib
import json
import math
from datetime import datetime

from core.legacy_quotes import (
    MAX_INSTRUMENTS as MAX_INSTRUMENTS,
)
from core.legacy_quotes import (
    VERSION as LEGACY_VERSION,
)
from core.legacy_quotes import (
    _clock,
    archive_observation,
    parse_quote,
    quote_request,
    restore_observation,
)
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time, is_canonical_iso_date

VERSION = 'dated-quote-context-v1'
MAX_JSON_BYTES = 131072
MAX_ASSEMBLY_SECONDS = 30
QUOTE_SOURCES = ('market-watch', 'dated-quote', 'dated-quote-context')
SOURCE_CONTRACTS = {'market-watch': None, 'dated-quote': LEGACY_VERSION, 'dated-quote-context': VERSION}


def context_requests(ins_code, day):
    code, _ = quote_request(ins_code)
    if not is_canonical_iso_date(day):
        raise ValueError('تاریخ دامنه مجاز باید میلادی استاندارد باشد')
    return (f'https://cdn.tsetmc.com/api/Instrument/GetInstrumentInfo/{code}',
            f'https://cdn.tsetmc.com/api/MarketData/GetStaticThreshold/{code}/{day.replace("-", "")}')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('کلید تکراری در پاسخ مشخصات/دامنه مجاز است')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('عدد نامتناهی در پاسخ مشخصات/دامنه مجاز است')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('عدد نامتناهی در پاسخ مشخصات/دامنه مجاز است')
    return value


def _payload(observation, code, url):
    if not isinstance(observation, dict):
        raise ValueError('یکی از پاسخ‌های لازم برای تطبیق دامنه موجود نیست')
    content = observation.get('content')
    if (observation.get('source') != 'tsetmc' or observation.get('ins_code') != code
            or observation.get('url') != url or not isinstance(content, bytes)
            or not 0 < len(content) <= MAX_JSON_BYTES or observation.get('response_bytes') != len(content)
            or observation.get('body_sha256') != hashlib.sha256(content).hexdigest()):
        raise ValueError('هویت، نشانی یا بایت مشخصات/دامنه مجاز ناسازگار است')
    try:
        payload = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique,
                             parse_constant=_constant, parse_float=_finite_float)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('پاسخ مشخصات/دامنه JSON معتبر نیست') from exc
    if not isinstance(payload, dict):
        raise ValueError('ریشه پاسخ مشخصات/دامنه باید شیء باشد')
    return payload


def _number(value, *, integer=False):
    if (type(value) not in (int, float) or value <= 0 or value > 2**53 or not math.isfinite(value)
            or integer and not float(value).is_integer()):
        raise ValueError('عدد مثبت معتبر در دامنه/حجم مبنا لازم است')
    return int(value) if integer else value


def _native_identity(value, code, *, optional):
    if optional and type(value) in (int, str, type(None)) and value in (None, 0, '0'):
        return
    if type(value) not in (int, str) or str(value) != code:
        raise ValueError('شناسه بومی مشخصات/دامنه با درخواست برابر نیست')


def _native_day(value):
    if type(value) not in (int, str) or len(str(value)) != 8 or not str(value).isascii() or not str(value).isdigit():
        raise ValueError('تاریخ بومی مشخصات/دامنه نامعلوم است')
    day = deven_to_iso(value)
    if not is_canonical_iso_date(day):
        raise ValueError('تاریخ بومی مشخصات/دامنه نامعتبر است')
    return day


def _latest_limits(payload, code, day, quote_time):
    rows = payload.get('staticThreshold')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError('فهرست محدود دامنه مجاز موجود نیست')
    stamps, candidates = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('ردیف دامنه مجاز باید شیء باشد')
        _native_identity(row.get('insCode'), code, optional=True)
        native_day = _native_day(row.get('dEven'))
        value = row.get('hEven')
        if (type(value) not in (int, str) or not 1 <= len(str(value)) <= 6
                or not str(value).isascii() or not str(value).isdigit()):
            raise ValueError('ساعت بومی دامنه نامعلوم است')
        stamp = datetime.fromisoformat(native_day + 'T' + heven_to_time(value)).replace(tzinfo=TEHRAN_TZ)
        if stamp in stamps:
            raise ValueError('دامنه با ساعت بومی تکراری/مبهم پذیرفته نیست')
        stamps.add(stamp)
        if native_day > day:
            raise ValueError('دامنه روز بعد در پاسخ روز درخواست‌شده است')
        # قیمت ردیف روز قبل مصرف نمی‌شود و جایگزین دامنه غایب امروز نمی‌شود.
        if native_day != day:
            continue
        if stamp > quote_time:
            raise ValueError('دامنه پس از زمان قیمت تغییر کرده؛ پیوند زمانی تأیید نیست')
        low, high = _number(row.get('psGelStaMin')), _number(row.get('psGelStaMax'))
        if low > high:
            raise ValueError('کمینه و بیشینه دامنه مجاز وارونه‌اند')
        candidates.append((stamp, low, high))
    if not candidates:
        raise ValueError('ردیف هم‌روز دامنه مجاز موجود نیست؛ ردیف قبل جایگزین نمی‌شود')
    return max(candidates, key=lambda item: item[0])[1:]


def parse_context(observation, symbol):
    if not isinstance(observation, dict) or observation.get('version') != VERSION:
        raise ValueError('قرارداد مظنه همراه دامنه شناخته‌شده نیست')
    code, _ = quote_request(observation.get('ins_code'))
    quote = observation.get('quote')
    if not isinstance(quote, dict):
        raise ValueError('مظنه اصلی موجود نیست')
    snapshot = parse_quote(quote, symbol)
    if quote['ins_code'] != code or observation.get('requested_at') != quote['requested_at']:
        raise ValueError('هویت یا شروع مجموعه با مظنه برابر نیست')
    day = snapshot['provider_timestamp'][:10]
    info_url, limits_url = context_requests(code, day)
    info_observation, limits_observation = observation.get('instrument'), observation.get('limits')
    info_payload = _payload(info_observation, code, info_url)
    limits_payload = _payload(limits_observation, code, limits_url)
    clocks = [_clock(observation['requested_at']), _clock(quote['received_at']),
        _clock(info_observation.get('requested_at')), _clock(info_observation.get('received_at')),
        _clock(limits_observation.get('requested_at')), _clock(limits_observation.get('received_at')),
        _clock(observation.get('received_at'))]
    if clocks != sorted(clocks) or (clocks[-1]-clocks[0]).total_seconds() > MAX_ASSEMBLY_SECONDS:
        raise ValueError('ترتیب یا مدت گردآوری مظنه/دامنه معتبر نیست')
    if clocks[0].date().isoformat() != day or clocks[-1].date().isoformat() != day:
        raise ValueError('این منبع جاری، مظنه روز قبل را با مشخصات امروز ترکیب نمی‌کند')
    info = info_payload.get('instrumentInfo')
    if not isinstance(info, dict):
        raise ValueError('مشخصات هویت ابزار موجود نیست')
    _native_identity(info.get('insCode'), code, optional=False)
    if _native_day(info.get('dEven')) != day:
        raise ValueError('تاریخ مشخصات ابزار با مظنه هم‌روز نیست')
    if (type(info.get('yVal')) not in (int, str) or str(info['yVal']) not in ('300', '303')
            or not isinstance(info.get('lVal18AFC'), str)
            or normalize_symbol(info['lVal18AFC']) != normalize_symbol(symbol)
            or normalize_symbol(symbol)[-1:].isdigit()):
        raise ValueError('نام یا نوع سهام عادی در بدنه مشخصات منطبق نیست')
    low, high = _latest_limits(limits_payload, code, day, _clock(snapshot['provider_timestamp']))
    stated = info.get('staticThreshold')
    if (not isinstance(stated, dict) or _number(stated.get('psGelStaMin')) != low
            or _number(stated.get('psGelStaMax')) != high):
        raise ValueError('دامنه تاریخ‌دار با مشخصات بومی هم‌روز برابر نیست')
    _native_identity(stated.get('insCode'), code, optional=True)
    if type(stated.get('dEven')) is bool or type(stated.get('hEven')) is bool:
        raise ValueError('بولی، تاریخ یا ساعت دامنه نیست')
    if stated.get('dEven') not in (None, 0, '0') and _native_day(stated['dEven']) != day:
        raise ValueError('تاریخ داخلی دامنه مشخصات ناسازگار است')
    snapshot.update(captured_at=observation['received_at'], price_limit_down=low, price_limit_up=high,
        base_volume=None if info.get('baseVol') is None else _number(info['baseVol'], integer=True))
    if not low <= snapshot['last_price'] <= high:
        raise ValueError('آخرین معامله با دامنه متقاطع سازگار نیست')
    for level in range(1, 6):
        for side in ('bid', 'ask'):
            price = snapshot.get(f'{side}_price_{level}')
            if price is not None and price != 0 and not low <= price <= high:
                raise ValueError('قیمت دفتر سفارش بیرون از دامنه متقاطع است')
    for side in ('bid', 'ask'):
        previous, ended = None, False
        for level in range(1, 6):
            price = snapshot.get(f'{side}_price_{level}')
            if price is None or price == 0:
                ended = True
                continue
            if ended or previous is not None and (price >= previous if side == 'bid' else price <= previous):
                raise ValueError('ترتیب سطوح عمق نامعتبر است؛ بهترین مظنه یا حجم مرز تأیید نیست')
            previous = price
    bid, ask = snapshot.get('bid_price_1'), snapshot.get('ask_price_1')
    if bid is not None and ask is not None and bid > 0 and ask > 0 and bid > ask:
        raise ValueError('بهترین تقاضا از عرضه بیشتر است؛ دفتر سفارش متقاطع است')
    for side, boundary, target in (('bid', high, 'buy_queue_volume'), ('ask', low, 'sell_queue_volume')):
        price, volume = (snapshot.get(f'{side}_{field}_1') for field in ('price', 'vol'))
        snapshot[target] = None if price is None or volume is None else volume if price == boundary else 0
    return snapshot


def _archive_json(observation):
    if observation is None:
        return None
    if not isinstance(observation, dict):
        raise ValueError('مشاهده مشخصات/دامنه باید شیء باشد')
    content = observation.get('content')
    if not isinstance(content, bytes) or len(content) > MAX_JSON_BYTES:
        raise ValueError('بایت مشخصات/دامنه از حد حجم گذشت')
    return {**{key: observation[key] for key in ('source', 'ins_code', 'url', 'requested_at', 'received_at',
        'response_bytes', 'body_sha256')}, 'body_base64': base64.b64encode(content).decode('ascii')}


def archive_source(observation, quote_source):
    if quote_source == 'dated-quote':
        return archive_observation(observation)
    if quote_source != 'dated-quote-context' or observation.get('version') != VERSION:
        raise ValueError('منبع بایگانی مظنه شناخته‌شده نیست')
    return dict(version=VERSION, ins_code=observation['ins_code'], requested_at=observation['requested_at'],
        received_at=observation['received_at'], quote=archive_observation(observation['quote']),
        instrument=_archive_json(observation.get('instrument')), limits=_archive_json(observation.get('limits')))


def _restore_json(observation):
    if observation is None:
        return None
    if not isinstance(observation, dict):
        raise ValueError('مشاهده محفوظ مشخصات/دامنه باید شیء باشد')
    encoded = observation.get('body_base64')
    if not isinstance(encoded, str) or len(encoded) > 4*((MAX_JSON_BYTES+2)//3):
        raise ValueError('بایت محفوظ مشخصات/دامنه معتبر نیست')
    try:
        return dict(observation, content=base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError) as exc:
        raise ValueError('بایت محفوظ مشخصات/دامنه معتبر نیست') from exc


def restore_source(observation, quote_source):
    if quote_source == 'dated-quote':
        return restore_observation(observation)
    if quote_source != 'dated-quote-context' or not isinstance(observation, dict) or observation.get('version') != VERSION:
        raise ValueError('منبع محفوظ مظنه شناخته‌شده نیست')
    return dict(observation, quote=restore_observation(observation.get('quote')),
                instrument=_restore_json(observation.get('instrument')), limits=_restore_json(observation.get('limits')))


def parse_source(observation, symbol, quote_source):
    if quote_source == 'dated-quote':
        return parse_quote(observation, symbol)
    if quote_source == 'dated-quote-context':
        return parse_context(observation, symbol)
    raise ValueError('منبع خواندن مظنه شناخته‌شده نیست')
