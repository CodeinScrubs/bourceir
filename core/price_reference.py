"""بازیابی مرجع نامعلوم از دو پاسخ بومی صریح یک ناشر؛ بدون استنباط از قیمت قبلی."""

import math

import pandas as pd

from core.instrument_history import receipt_time
from core.price_quality import json_price_row, price_quality
from core.price_revision import SOURCE_FIELDS, daily_request
from core.timeutils import is_canonical_iso_date

VERSION = 'explicit-price-reference-v1'
REFERENCE_REASONS = {'missing_or_nonfinite_ref_price_raw', 'nonpositive_ref_price_raw'}


def valid_reference_change(before, after):
    """تنها مرجع گمشده/نامثبت عوض می‌شود؛ همه ارقام دیگر باید دقیقاً ثابت باشند."""
    fields = {'date', *SOURCE_FIELDS}
    if (not isinstance(before, dict) or not isinstance(after, dict) or set(before) != fields or set(after) != fields
            or not is_canonical_iso_date(before['date']) or before['date'] != after['date']
            or any(v is not None and (type(v) not in (int, float) or not -2**53 < v < 2**53 or not math.isfinite(v))
                   for k, v in before.items() if k != 'date')
            or before['ref_price_raw'] is not None and before['ref_price_raw'] > 0
            or any(before[k] != after[k] for k in fields - {'ref_price_raw'})
            or any(type(after[k]) not in (int, float) or not 0 < after[k] < 2**53
                   or not math.isfinite(after[k]) for k in SOURCE_FIELDS)
            or any(after[k] % 1 for k in ('volume', 'trade_count'))):
        return False
    _, flags = price_quality(pd.DataFrame([before]))
    reasons = set(flags.columns[flags.iloc[0]])
    _, revised = price_quality(pd.DataFrame([after]))
    return bool(reasons and reasons <= REFERENCE_REASONS and not revised.any(axis=None))


def _bound_row(observation, code, day, *, history):
    suffix = f'GetClosingPriceDailyList/{code}/0' if history else f'GetClosingPriceDaily/{code}/{day.replace("-", "")}'
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc'
            or observation.get('price_unit') != 'Rial' or observation.get('ins_code') != code
            or observation.get('requested_date') != day
            or observation.get('url') != 'https://cdn.tsetmc.com/api/ClosingPrice/' + suffix):
        raise ValueError('واحد، تاریخ، هویت یا نشانی شاهد مرجع سازگار نیست')
    receipt_time(observation.get('received_at'))
    payload = observation.get('payload')
    row = payload.get('closingPriceDaily') if isinstance(payload, dict) else None
    if history:
        if not isinstance(row, list) or len(row) != 1:
            return None, 'history_row_missing_or_ambiguous'
        row = row[0]
    if not isinstance(row, dict):
        return None, 'native_schema_invalid'
    # هویت و تاریخ هر دو بدنه پیش از هر رقم مالی ارزیابی می‌شوند.
    if type(row.get('insCode')) not in (str, int) or str(row['insCode']) != code:
        return None, 'native_identity_mismatch'
    if type(row.get('dEven')) not in (str, int) or str(row['dEven']) != day.replace('-', ''):
        return None, 'native_date_mismatch'
    return row, None


def reference_candidate(issue, daily, history, *, cutoff):
    day = issue.get('date')
    if not is_canonical_iso_date(cutoff) or not is_canonical_iso_date(day) or day >= cutoff:
        raise ValueError('بازیابی مرجع فقط پیش از مرز مهروموم و با تاریخ استاندارد مجاز است')
    code, _ = daily_request(issue.get('ins_code'), day)
    before = issue.get('raw')
    if (not isinstance(before, dict) or before.get('date') != day or set(before) != {'date', *SOURCE_FIELDS}
            or any(v is not None and (type(v) not in (int, float) or not -2**53 < v < 2**53 or not math.isfinite(v))
                   for k, v in before.items() if k != 'date') or before != json_price_row(before)):
        raise ValueError('نسخه قبلی خام باید صریح و استاندارد باشد')
    result = dict(version=VERSION, status='source_unavailable', before=before, after=None,
                  changed_fields=[], daily_identity_and_date_verified=False,
                  history_identity_and_date_verified=False, both_paths_all_fields_equal=False,
                  source_independently_verified=False, historical_availability_verified=False,
                  economic_entitlements_verified=False, accepted_for_signals=False, profitability_proven=False)
    _, flags = price_quality(pd.DataFrame([before]))
    reasons = set(flags.columns[flags.iloc[0]])
    if not reasons or not reasons <= REFERENCE_REASONS:
        return result | dict(status='outside_reference_only_repair')
    if daily is None or history is None:
        return result
    # حتی پاسخ نخست تا تأیید هویت/تاریخ پاسخ دوم از نظر مالی خوانده نمی‌شود.
    first, error = _bound_row(daily, code, day, history=False)
    if error:
        return result | dict(status=error)
    result['daily_identity_and_date_verified'] = True
    second, error = _bound_row(history, code, day, history=True)
    if error:
        return result | dict(status=error)
    result['history_identity_and_date_verified'] = True
    rows = [{k: native.get(v) for k, v in SOURCE_FIELDS.items()} for native in (first, second)]
    if any(type(v) not in (int, float) or not 0 < v < 2**53 or not math.isfinite(v)
           for row in rows for v in row.values()):
        return result | dict(status='native_numeric_invalid')
    if rows[0] != rows[1]:
        return result | dict(status='native_paths_disagree')
    result['both_paths_all_fields_equal'] = True
    after = dict(date=day, **{k: float(v) for k, v in rows[0].items()})
    if not valid_reference_change(before, after):
        return result | dict(status='other_fields_changed_or_row_invalid')
    return result | dict(status='verified_explicit_reference', after=after, changed_fields=['ref_price_raw'])
