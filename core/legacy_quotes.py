"""خواندن بسته یکپارچه مظنه؛ ساعت قیمت جای ساعت مستقل دفتر سفارش نیست."""

import base64
import hashlib
import re
from datetime import datetime

from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time

VERSION = 'dated-quote-v1'
MAX_BYTES = 262144
MAX_INSTRUMENTS = 10
QUOTE_SOURCES = ('market-watch', 'dated-quote')


def quote_request(ins_code):
    code = str(ins_code)
    if not re.fullmatch(r'[0-9]{1,20}', code) or int(code) <= 0:
        raise ValueError('شناسه ابزار باید عدد صحیح مثبت باشد')
    return code, f'https://old.tsetmc.com/tsev2/data/instinfodata.aspx?i={code}&c=&e=1'


def _integer(text):
    if not re.fullmatch(r'[0-9]{1,19}', text) or int(text) > 2**63 - 1:
        raise ValueError('عدد صحیح نامنفی و محدود در پاسخ لازم است')
    return int(text)


def _clock(text):
    try:
        result = datetime.fromisoformat(text)
        if result.tzinfo is None:
            raise ValueError('زمان بدون منطقه زمانی')
        return result.astimezone(TEHRAN_TZ)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('زمان دریافت معتبر نیست') from exc


def archive_observation(observation):
    """فقط بایت عمومی و مشخصات درخواست؛ کوکی و سرآیند نشست ذخیره نمی‌شوند."""
    if not isinstance(observation.get('content'), bytes) or len(observation['content']) > MAX_BYTES:
        raise ValueError('بایت شاهد مظنه از حد حجم گذشت')
    keys = ('source', 'ins_code', 'url', 'requested_at', 'received_at', 'response_bytes', 'body_sha256')
    return dict(version=VERSION, **{key: observation[key] for key in keys},
                body_base64=base64.b64encode(observation['content']).decode('ascii'))


def restore_observation(evidence):
    if not isinstance(evidence, dict) or evidence.get('version') != VERSION:
        raise ValueError('قرارداد شاهد مظنه معتبر نیست')
    encoded = evidence.get('body_base64')
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError('بایت شاهد مظنه از حد حجم گذشت')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('بایت شاهد مظنه معتبر نیست') from exc
    return dict(evidence, content=content)


def parse_quote(observation, symbol):
    """نام و هویت از دامنه درخواست می‌آیند؛ بدنه شناسه مستقل ندارد."""
    code, url = quote_request(observation['ins_code'])
    content = observation.get('content')
    if (observation.get('source') != 'tsetmc' or observation.get('url') != url
            or not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES
            or observation.get('response_bytes') != len(content)
            or observation.get('body_sha256') != hashlib.sha256(content).hexdigest()):
        raise ValueError('منبع، حجم یا هش پاسخ مظنه ناسازگار است')
    start, received = (_clock(observation.get(key)) for key in ('requested_at', 'received_at'))
    if received < start:
        raise ValueError('ساعت دریافت پیش از درخواست است')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('رمزگذاری پاسخ مظنه معتبر نیست') from exc
    if '\x00' in text or text.lstrip().startswith('<'):
        raise ValueError('پاسخ مظنه ساختار متنی مجاز ندارد')
    sections = text.strip().split(';')
    if len(sections) != 9:
        raise ValueError('تعداد بخش‌های پاسخ مظنه شناخته‌شده نیست')
    # بخش شاخص می‌تواند برچسب نمایشی داشته باشد؛ هیچ بخش آن اجرا یا تفسیر نمی‌شود.
    if any(('<' in section or '>' in section) for index, section in enumerate(sections) if index != 1):
        raise ValueError('برچسب نمایشی بیرون از بخش شاخص مجاز نیست')
    fields = sections[0].split(',')
    if len(fields) == 17:
        if _integer(fields[14]) != _integer(fields[10]):
            raise ValueError('نسخه دارای ستون تکراری ناسازگار است')
        fields = fields[:14] + fields[15:]
    if len(fields) != 16:
        raise ValueError('ستون‌های قیمت شناخته‌شده نیستند')
    try:
        if not re.fullmatch(r'[0-9]{8}', fields[12]) or not re.fullmatch(r'[0-9]{1,6}', fields[13]):
            raise ValueError('تاریخ یا ساعت منبع نامعلوم است')
        stamp = datetime.fromisoformat(deven_to_iso(int(fields[12])) + 'T' + heven_to_time(int(fields[13])))
        stamp = stamp.replace(tzinfo=TEHRAN_TZ)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('تاریخ یا ساعت منبع معتبر نیست') from exc
    if stamp > received:
        raise ValueError('زمان منبع پس از دریافت است')
    names = ('last_price', 'close_price', 'open_price', 'ref_price', 'high_price', 'low_price',
             'trade_count', 'volume', 'value')
    numbers = dict(zip(names, (_integer(value) for value in fields[2:11])))
    if numbers['volume'] > 0:
        if any(value <= 0 for value in numbers.values()):
            raise ValueError('آمار مثبت روز معامله ناقص است')
        if not (numbers['low_price'] <= numbers['open_price'] <= numbers['high_price']
                and numbers['low_price'] <= numbers['last_price'] <= numbers['high_price']):
            raise ValueError('قیمت‌های معامله با کمینه و بیشینه سازگار نیستند')
        if not numbers['low_price'] <= numbers['value'] / numbers['volume'] <= numbers['high_price']:
            raise ValueError('ارزش و حجم با بازه معامله سازگار نیستند')
    elif numbers['value'] or numbers['trade_count']:
        raise ValueError('حجم صفر با ارزش یا تعداد معامله مثبت سازگار نیست')
    snapshot = dict(ins_code=code, symbol_fa=normalize_symbol(symbol), source='tsetmc', price_unit='Rial',
                    captured_at=observation['received_at'], provider_timestamp=stamp.isoformat(),
                    last_trade_time=stamp.strftime('%H:%M:%S'), **numbers,
                    price_limit_up=None, price_limit_down=None, base_volume=None,
                    buy_queue_volume=None, sell_queue_volume=None)
    # سرانه/صف/دامنه از ستون نامعلوم یا بیشینه و کمینه روز ساخته نمی‌شوند.
    for client in ('real', 'legal'):
        for side in ('buy', 'sell'):
            snapshot[f'{client}_{side}_count'] = None
            snapshot[f'{client}_{side}_volume'] = None
    levels = sections[2].split(',')
    if levels[-1:] == ['']:
        levels.pop()
    if levels == ['']:
        levels = []
    if len(levels) > 5:
        raise ValueError('بیش از پنج سطح سفارش در پاسخ است')
    for level in range(1, 6):
        for side in ('bid', 'ask'):
            for key in ('count', 'vol', 'price'):
                snapshot[f'{side}_{key}_{level}'] = None
        if level > len(levels):
            continue
        parts = levels[level - 1].split('@')
        if len(parts) != 6:
            raise ValueError('ستون‌های سطح سفارش شناخته‌شده نیستند')
        bid_count, bid_vol, bid_price, ask_price, ask_vol, ask_count = map(_integer, parts)
        for side, count, volume, price in (('bid', bid_count, bid_vol, bid_price),
                                            ('ask', ask_count, ask_vol, ask_price)):
            if (count == 0 or volume == 0 or price == 0) and (count or volume or price):
                raise ValueError('قیمت، تعداد و حجم سطح سفارش با هم سازگار نیستند')
            snapshot.update({f'{side}_count_{level}': count, f'{side}_vol_{level}': volume,
                             f'{side}_price_{level}': price})
    return snapshot
