"""اصلاح محدود قیمت خام با کندل بومی تاریخ‌دار و تمام ریزمعاملات؛ بدون حدس مرجع."""

import math
import re

import pandas as pd

from core.instrument_history import receipt_time
from core.price_quality import RAW_PRICES, json_price_row, price_quality
from core.query_screener import trade_evidence
from core.timeutils import is_canonical_iso_date

VERSION = 'native-price-revision-v1'
SOURCE_FIELDS = dict(open_raw='priceFirst', high_raw='priceMax', low_raw='priceMin', close_raw='pDrCotVal',
                     close_final_raw='pClosing', ref_price_raw='priceYesterday', volume='qTotTran5J',
                     value='qTotCap', trade_count='zTotTran')
REVISION_FIELDS = frozenset(SOURCE_FIELDS) - {'close_final_raw', 'ref_price_raw'}
STATISTICS = ('count', 'volume', 'value', 'open', 'last', 'low', 'high')


def daily_request(ins_code, trade_date):
    code = str(ins_code) if type(ins_code) in (str, int) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,29}', code) or not is_canonical_iso_date(trade_date):
        raise ValueError('شناسه مثبت عددی و تاریخ میلادی استاندارد لازم است')
    return code, f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDaily/{code}/{trade_date.replace("-", "")}'


def revision_candidate(issue, observation, trades, *, cutoff):
    """هم‌خوانی دو مسیر یک ناشر؛ صحت مستقل و دسترسی به این نسخه در گذشته اثبات نمی‌شوند."""
    day = issue.get('date')
    # مرز پیش از خواندن ارقام پاسخ یا ریزمعاملات کنترل می‌شود.
    if not is_canonical_iso_date(cutoff) or not is_canonical_iso_date(day) or day >= cutoff:
        raise ValueError('اصلاح فقط برای روز صریح پیش از مرز مهروموم است')
    code, url = daily_request(issue.get('ins_code'), day)
    raw = issue.get('raw')
    if (not isinstance(raw, dict) or set(raw) != {'date', *SOURCE_FIELDS} or raw.get('date') != day
            or any(v is not None and (type(v) not in (int, float) or not -2**53 < v < 2**53 or not math.isfinite(v))
                   for k, v in raw.items() if k != 'date') or raw != json_price_row(raw)):
        raise ValueError('نسخه قبلی قیمت معتبر و صریح لازم است')
    _, flags = price_quality(pd.DataFrame([raw]))
    reasons = flags.columns[flags.iloc[0]].tolist()
    result = dict(version=VERSION, status='source_unavailable', before=raw, after=None, changed_fields=[],
                  original_reasons=reasons, remaining_reasons=None, reconciliation=None,
                  native_identity_verified=False, native_date_verified=False, applied=False,
                  source_independently_verified=False, historical_availability_verified=False,
                  accepted_for_signals=False, profitability_proven=False)
    if not reasons:
        return result | dict(status='original_row_not_defective')
    if observation is None:
        return result
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc'
            or observation.get('price_unit') != 'Rial' or observation.get('url') != url
            or observation.get('ins_code') != code or observation.get('requested_date') != day):
        raise ValueError('نشانی، هویت، منبع یا واحد شاهد روزانه سازگار نیست')
    receipt_time(observation.get('received_at'))
    payload = observation.get('payload')
    native = payload.get('closingPriceDaily') if isinstance(payload, dict) else None
    if not isinstance(native, dict):
        return result | dict(status='invalid_native_schema')
    identity = native.get('insCode')
    if type(identity) not in (str, int) or str(identity) != code:
        return result | dict(status='native_identity_mismatch')
    result['native_identity_verified'] = True
    stated = native.get('dEven')
    if type(stated) not in (str, int) or str(stated) != day.replace('-', ''):
        return result | dict(status='native_date_mismatch')
    result['native_date_verified'] = True
    # URL تاریخ‌دار کافی نیست: منبع برای بعضی روزها عملاً آخرین روز را برمی‌گرداند.
    values = {field: native.get(name) for field, name in SOURCE_FIELDS.items()}
    if any(type(value) not in (int, float) or not 0 < value < 2**53 or not math.isfinite(value)
           for value in values.values()):
        return result | dict(status='invalid_native_numeric')
    if any(values[k] % 1 for k in ('volume', 'trade_count')):
        return result | dict(status='invalid_native_quantity')
    after = dict(date=day, **{k: float(v) for k, v in values.items()})
    _, revised_flags = price_quality(pd.DataFrame([after]))
    remaining = revised_flags.columns[revised_flags.iloc[0]].tolist()
    changes = sorted(k for k in SOURCE_FIELDS if after[k] != raw[k])
    result.update(after=after, changed_fields=changes, remaining_reasons=remaining)
    if remaining:
        return result | dict(status='native_row_still_invalid')
    if not changes:
        return result | dict(status='native_row_unchanged')
    if not set(changes) <= REVISION_FIELDS:
        return result | dict(status='protected_reference_or_official_change')
    if trades is None:
        return result | dict(status='source_candidate_needs_trades')
    checked = trade_evidence(trades, after | dict(source='tsetmc', price_unit='Rial', ins_code=code))
    checks = checked.get('reconciliation')
    # نتیجه الگوی قیمت در انتخاب یا گزارش اصلاح مصرف نمی‌شود؛ فقط هفت تطبیق حسابی لازم‌اند.
    result.update(reconciliation=checks, trade_issues=checked['issues'],
                  trade_payload_date_verified=checked['payload_date_verified'],
                  trade_payload_identity_verified=checked['payload_identity_verified'],
                  duplicate_trade_rows=checked['duplicate_rows'], canceled_revision_rows=checked['canceled_revision_rows'])
    if checked['contradictory'] or not all((checks or {}).get(k) is True for k in STATISTICS):
        return result | dict(status='unverified_full_trade_path')
    return result | dict(status='verified_native_trade_revision')


def valid_revised_row(before, after, changes):
    """نگهبان مستقل دامنه نوشتن؛ قیمت مرجع و پایانی رسمی هرگز از مسیر معامله ساخته نمی‌شوند."""
    if (set(before) != {'date', *SOURCE_FIELDS} or set(after) != set(before)
            or not changes or set(changes) - REVISION_FIELDS or len(changes) != len(set(changes))
            or {k for k in before if before[k] != after[k]} != set(changes)
            or any(type(after[k]) not in (int, float) or not 0 < after[k] < 2**53
                   or not math.isfinite(after[k]) for k in SOURCE_FIELDS)
            or any(after[k] % 1 for k in ('volume', 'trade_count'))):
        return False
    _, before_flags = price_quality(pd.DataFrame([before]))
    _, after_flags = price_quality(pd.DataFrame([after]))
    return bool(before_flags.any(axis=None) and not after_flags.any(axis=None)
                and all(k in RAW_PRICES or k == 'trade_count' for k in changes))
