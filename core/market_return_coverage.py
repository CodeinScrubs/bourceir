"""Descriptive sensitivity to an explicitly zero opening price, never a trading signal."""

import math
from collections import Counter
from datetime import datetime
from statistics import median

from core.market_review import number
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time, is_canonical_iso_date, now_tehran

VERSION = 'market-return-coverage-v1'


def _clock(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(TEHRAN_TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def _return(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _native_agrees(code, observation, candle, session_date):
    payload = observation.get('payload')
    native = payload.get('closingPriceInfo') if isinstance(payload, dict) else None
    if (observation.get('source') != 'tsetmc' or not isinstance(native, dict)
            or observation.get('url') != f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceInfo/{code}'):
        return False
    native_code = native.get('insCode')
    if native_code is not None and type(native_code) not in (str, int):
        return False
    identity = ('request_url_only' if native_code in (None, 0, '0')
                else 'response_matches_request' if str(native_code) == code else 'conflicting_response')
    if isinstance(native_code, bool) or identity != observation.get('identity_attestation'):
        return False
    fields = {'open_raw':'priceFirst', 'low_raw':'priceMin', 'high_raw':'priceMax', 'close_raw':'pDrCotVal',
              'close_final_raw':'pClosing', 'ref_price_raw':'priceYesterday', 'volume':'qTotTran5J',
              'value':'qTotCap', 'trade_count':'zTotTran'}
    if any(name not in candle or source not in native or candle[name] != native[source]
           or isinstance(candle[name], bool) or isinstance(native[source], bool)
           for name, source in fields.items()):
        return False
    for key in ('dEven', 'finalLastDate', 'hEven'):
        value = native.get(key)
        if type(value) not in (str, int) or not str(value).isascii() or not str(value).isdigit():
            return False
        if key != 'hEven' and len(str(value)) != 8:
            return False
        if key == 'hEven' and (not 1 <= len(str(value)) <= 6 or int(value) == 0):
            return False
    try:
        date = deven_to_iso(native.get('dEven'))
        final_date = deven_to_iso(native.get('finalLastDate'))
        native_time = datetime.fromisoformat(f"{date}T{heven_to_time(native.get('hEven'))}").replace(tzinfo=TEHRAN_TZ)
        return (date == final_date == session_date and _clock(observation.get('provider_timestamp')) == native_time)
    except (TypeError, ValueError, OverflowError):
        return False


def _partial_return(row, observation, *, session_date, observed_at):
    if not isinstance(observation, dict) or observation.get('ins_code') != row.get('ins_code'):
        return None, 'missing_or_conflicting_identity'
    identity = observation.get('identity_attestation')
    if identity not in ('request_url_only', 'response_matches_request'):
        return None, 'missing_or_conflicting_identity'
    candle = observation.get('candle')
    if not isinstance(candle, dict) or observation.get('date_status') != 'matched':
        return None, 'unknown_or_conflicting_date'
    if not _native_agrees(row['ins_code'], observation, candle, session_date):
        return None, 'native_payload_mismatch'
    received = _clock(observation.get('received_at'))
    provider = _clock(observation.get('provider_timestamp'))
    if (candle.get('date') != session_date or row.get('source_date') != session_date
            or row.get('received_at') != observation.get('received_at')
            or row.get('provider_timestamp') != observation.get('provider_timestamp')
            or received is None or provider is None or provider > received or received > observed_at
            or provider.date().isoformat() != session_date):
        return None, 'unknown_or_conflicting_date'
    if number(candle.get('open_raw')) != 0:
        return None, 'open_not_observed_zero'
    low, high, last, final, reference, volume, value, count = (
        number(candle.get('low_raw'), positive=True), number(candle.get('high_raw'), positive=True),
        number(candle.get('close_raw'), positive=True), number(candle.get('close_final_raw'), positive=True),
        number(candle.get('ref_price_raw'), positive=True),
        number(candle.get('volume'), positive=True, integer=True), number(candle.get('value'), positive=True),
        number(candle.get('trade_count'), positive=True, integer=True))
    if (any(item is None for item in (low, high, last, final, reference, volume, value, count))
            or not low <= last <= high or not low-high*1e-6 <= value/volume <= high+high*1e-6):
        return None, 'other_components_unknown_or_inconsistent'
    change = final/reference-1
    if not math.isfinite(change):
        return None, 'other_components_unknown_or_inconsistent'
    return dict(symbol=row['symbol'], ins_code=row['ins_code'], received_at=row['received_at'],
                provider_timestamp=row['provider_timestamp'], identity_attestation=identity,
                close_final_raw=final, ref_price_raw=reference, closing_return=change), None


def missing_open_sensitivity(report, observations, *, at=None):
    """Extend breadth for sensitivity only; never impute an opening or alter the source report."""
    if not isinstance(report, dict) or not isinstance(observations, dict):
        raise ValueError('Market report and observations are required')
    rows, summary = report.get('instruments'), report.get('summary')
    session_date, observed_at = report.get('session_date'), _clock(report.get('observed_at'))
    at = now_tehran() if at is None else at
    checked_at = _clock(at.isoformat()) if isinstance(at, datetime) else None
    if (not isinstance(rows, list) or not isinstance(summary, dict) or not is_canonical_iso_date(session_date)
            or observed_at is None or checked_at is None or observed_at > checked_at
            or session_date > observed_at.date().isoformat()):
        raise ValueError('Market report scope or clock is invalid')
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('Market report row is invalid')
    if any(not isinstance(row.get('symbol'), str) or not row['symbol'].strip() for row in rows):
        raise ValueError('Market report symbol is missing')
    codes = [row.get('ins_code') for row in rows]
    if any(not isinstance(code, str) or not code.isascii() or not code.isdigit() for code in codes) or len(set(codes)) != len(codes):
        raise ValueError('Market report identities must be unique numeric codes')
    baseline = []
    for row in rows:
        if row.get('status') == 'traded' and row.get('closing_return') is not None:
            value = _return(row['closing_return'])
            if value is None:
                raise ValueError('Market report contains an invalid baseline return')
            baseline.append(value)
    traded_count = sum(row.get('status') == 'traded' for row in rows)
    counts = dict(scope_count=len(rows), traded_count=traded_count, return_known_count=len(baseline),
                  return_unknown_count=traded_count-len(baseline), positive=sum(v > 0 for v in baseline),
                  negative=sum(v < 0 for v in baseline), unchanged=sum(v == 0 for v in baseline))
    if any(type(summary.get(key)) is not int or summary[key] != value for key, value in counts.items()):
        raise ValueError('Market report return counts do not reconcile')
    partial, excluded = [], Counter()
    invalid = [row for row in rows if row.get('status') == 'invalid_candle']
    for row in invalid:
        result, reason = _partial_return(row, observations.get(row['ins_code']),
                                         session_date=session_date, observed_at=observed_at)
        if reason:
            excluded[reason] += 1
        else:
            partial.append(result)
    values = baseline + [row['closing_return'] for row in partial]
    positive = sum(v > 0 for v in values)
    return dict(version=VERSION, invalid_candles=len(invalid), partial_returns=len(partial),
                excluded=dict(excluded), rows=partial, baseline_known=len(baseline),
                baseline_positive=counts['positive'], sensitivity_known=len(values),
                sensitivity_positive=positive, sensitivity_negative=sum(v < 0 for v in values),
                sensitivity_unchanged=sum(v == 0 for v in values),
                sensitivity_positive_fraction=positive/len(values) if values else None,
                sensitivity_median=median(values) if values else None,
                opening_imputed=False, source_independently_verified=False,
                simultaneous_snapshot_verified=False, accepted_for_signals=False)
