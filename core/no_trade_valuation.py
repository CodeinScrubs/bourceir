"""Observed official marks on reported zero-activity dates; never executable OHLC candles."""
import base64
import json

import pandas as pd

from core import market_rules
from core.daily_publications import MAX_BYTES, _clock, _numeric, parse_publication, publication_request
from core.text import normalize_symbol

VERSION = 'observed-no-trade-mark-v1'
FALSE_FLAGS = dict(candle_created=False, executable=False, accepted_for_features=False,
    accepted_for_signals=False, historical_availability_verified=False,
    independent_source_correctness_verified=False, historical_membership_verified=False,
    first_publication_time_verified=False, full_session_coverage_verified=False,
    economic_entitlements_verified=False, continuity_verified=False, profitability_proven=False)


def parse_no_trade_publication(observation, ins_code, day, *, requested_after=None, received_by=None):
    """Reuse native publication binding. Source-level eligibility still needs path continuity."""
    publication = parse_publication(observation, ins_code, day, 'price',
        requested_after=requested_after, received_by=received_by)
    base = dict(version=VERSION, ins_code=ins_code, date=day, price_unit='Rial',
        body_sha256=observation['body_sha256'], requested_at=observation['requested_at'],
        received_at=observation['received_at'], native_identity_verified=True, native_date_verified=True,
        source_url=observation['url'], **FALSE_FLAGS)
    if publication['status'] == 'valid_price':
        return dict(base, status='observed_trading', flat_mark_eligible=False,
            official_close_rial=None, reference_rial=None, factor=None)
    # parse_publication already decoded and checked these exact immutable bytes,
    # including duplicate-key, numeric and identity/date guards.
    native = json.loads(observation['content'].decode('utf-8-sig'))['closingPriceDaily']
    close = _numeric(native.get('pClosing'), optional=True)
    reference = _numeric(native.get('priceYesterday'), optional=True)
    positive = close is not None and reference is not None and close > 0 and reference > 0
    eligible = positive and close == reference
    factor = (float(1 + market_rules.pit_daily_returns(pd.DataFrame([
        dict(close_final_raw=close, ref_price_raw=reference)])).iloc[0]) if eligible else None)
    return dict(base, status='observed_flat_mark' if eligible else 'unusable_official_mark',
        flat_mark_eligible=bool(eligible), official_close_rial=close, reference_rial=reference,
        reported_volume=native['qTotTran5J'], reported_value_rial=native['qTotCap'],
        reported_trade_count=native['zTotTran'], factor=factor)


def parse_no_trade_receipt(record):
    """Reparse a preserved daily-publication receipt; never trust its cached parsed result."""
    if not isinstance(record, dict) or record.get('version') != 'daily-publication-archive-v1':
        raise ValueError('رسید بومی روزانه برای دفتر ارزش لازم است')
    task = record.get('task')
    if (not isinstance(task, dict) or task.get('kind') != 'price'
            or type(task.get('instrument_type_code')) is not int or task['instrument_type_code'] not in (300, 303)
            or not isinstance(task.get('symbol'), str) or not task['symbol']
            or normalize_symbol(task['symbol']) != task['symbol'] or task['symbol'][-1:].isdigit()
            or not isinstance(task.get('ins_code'), str)):
        raise ValueError('هویت نماد عادی در رسید ارزش معتبر نیست')
    start, end = _clock(record.get('attempted_at')), _clock(record.get('completed_at'))
    code, _ = publication_request(task['ins_code'], task.get('date'), 'price')
    if end < start or task['ins_code'] != code or task['date'] > end.date().isoformat():
        raise ValueError('ترتیب دریافت رسید ارزش معتبر نیست')
    base = dict(version=VERSION, symbol=task['symbol'], ins_code=task['ins_code'], date=task['date'],
        flat_mark_eligible=False, **FALSE_FLAGS)
    source = record.get('source')
    if source is None:
        return dict(base, status='source_unavailable')
    if record.get('provider_error_type') is not None:
        raise ValueError('رسید خطای دریافت با بایت موجود ناسازگار است')
    if not isinstance(source, dict):
        raise ValueError('بایت بومی رسید ارزش موجود نیست')
    encoded = source.get('body_base64')
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4*((MAX_BYTES+2)//3):
        raise ValueError('بایت رسید ارزش موجود نیست یا از حد حجم گذشت')
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('بایت رسید ارزش نامعتبر است') from exc
    observation = {key:value for key,value in source.items() if key != 'body_base64'} | {'content':body}
    parsed = parse_no_trade_publication(observation, task['ins_code'], task['date'],
        requested_after=record['attempted_at'], received_by=record['completed_at'])
    return dict(parsed, symbol=task['symbol'])
