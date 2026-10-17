"""بررسی توصیفی جلسه؛ نه انتخاب روش، نه سیگنال و نه پذیرش اجرای معامله."""

import math
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

MARKET_REVIEW_VERSION = 'market-review-v1'
STATUS_LABELS_FA = dict(traded='معامله با کندل معتبر', not_traded='بدون معامله', unavailable='دریافت‌نشده',
                        identity_conflict='شناسه ناسازگار', unknown_or_conflicting_date='تاریخ یا ساعت نامعلوم/ناسازگار',
                        stale_session='مربوط به جلسه قدیمی', future_session='تاریخ آینده', invalid_candle='کندل یا آمار ناسازگار')


def number(value, *, positive=False, integer=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        return None
    return result if not integer or result.is_integer() else None


def _clock(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(TEHRAN_TZ) if dt.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def market_scope(observation):
    """فقط نوع صریح ۳۰۰/۳۰۳؛ شناسه یا نام مبهم حذف انتخابی نمی‌شود."""
    items = observation.get('instruments', []) if isinstance(observation, dict) else []
    candidates, excluded = [], Counter()
    for item in items:
        meta = item.get('metadata') if isinstance(item, dict) else None
        if not isinstance(meta, dict):
            excluded['outside_or_unknown_scope'] += 1
            continue
        symbol = normalize_symbol(meta.get('symbol_fa') or '')
        code = str(meta.get('ins_code') or '')
        kind = number(meta.get('instrument_type_code'), integer=True)
        if kind not in (300, 303) or not symbol or symbol[-1:].isdigit() or not code.isdigit():
            excluded['outside_or_unknown_scope'] += 1
        else:
            candidates.append(dict(item, metadata=dict(meta, symbol_fa=symbol, ins_code=code)))
    ids = Counter(row['metadata']['ins_code'] for row in candidates)
    names = Counter(row['metadata']['symbol_fa'] for row in candidates)
    result = []
    for item in candidates:
        meta = item['metadata']
        if ids[meta['ins_code']] > 1 or names[meta['symbol_fa']] > 1:
            excluded['ambiguous_identity_rows'] += 1
        else:
            result.append(item)
    return sorted(result, key=lambda row: row['metadata']['ins_code']), dict(excluded)


def registry_coverage(observation, registry):
    """غیبت در پاسخ امروز، حذف یا توقف قطعی ابزار را ثابت نمی‌کند."""
    observed, _ = market_scope(observation)
    cached, _ = market_scope(dict(instruments=[dict(metadata=row) for row in registry]))
    present = {row['metadata']['ins_code']: row['metadata'] for row in observed}
    known = {row['metadata']['ins_code']: row['metadata'] for row in cached}
    return dict(known_registry_count=len(known), observed_source_count=len(present),
                registry_not_observed=[known[code] for code in sorted(known.keys() - present.keys())],
                observed_not_in_registry=[present[code] for code in sorted(present.keys() - known.keys())],
                registry_inactive_but_observed=[known[code] for code in sorted(present.keys() & known.keys())
                                                if known[code].get('is_active') == 0],
                registry_current_membership_verified=False, absence_reason_verified=False)


def _price_quality(candle):
    prices = {key: number(candle.get(key), positive=True)
              for key in ('open_raw', 'low_raw', 'high_raw', 'close_raw')}
    volume = number(candle.get('volume'), positive=True, integer=True)
    value = number(candle.get('value'), positive=True)
    count = number(candle.get('trade_count'), positive=True, integer=True)
    if any(v is None for v in prices.values()) or None in (volume, value, count):
        return False
    low, high = prices['low_raw'], prices['high_raw']
    tolerance = high * 1e-6
    return (low <= prices['open_raw'] <= high and low <= prices['close_raw'] <= high
            and low - tolerance <= value / volume <= high + tolerance)


def _stats(rows):
    traded = [row for row in rows if row['status'] == 'traded']
    returns = [row['closing_return'] for row in traded if row['closing_return'] is not None]
    values = [row['value_rial'] for row in traded]
    return dict(scope_count=len(rows), status_counts=dict(Counter(row['status'] for row in rows)),
                traded_count=len(traded), return_known_count=len(returns),
                return_unknown_count=len(traded) - len(returns),
                positive=sum(v > 0 for v in returns), negative=sum(v < 0 for v in returns),
                unchanged=sum(v == 0 for v in returns),
                positive_fraction=sum(v > 0 for v in returns) / len(returns) if returns else None,
                median_closing_return=median(returns) if returns else None,
                value_rial=sum(values) if values else None,
                value_covered_count=len(values),
                one_price_up_proxy_count=sum(row['one_price_up_proxy'] for row in traded),
                one_price_down_proxy_count=sum(row['one_price_down_proxy'] for row in traded),
                verified_queue_count=None, verified_retail_net_value_rial=None)


def build_market_review(observation, closings, overviews, sectors, *, session_date, observed_at):
    if not is_canonical_iso_date(session_date):
        raise ValueError('تاریخ جلسه باید میلادی معتبر باشد')
    now = _clock(observed_at)
    if now is None or session_date > now.date().isoformat():
        raise ValueError('زمان مشاهده باید منطقه زمانی داشته و جلسه در آینده نباشد')
    scope, exclusions = market_scope(observation)
    rows = []
    for item in scope:
        meta = item['metadata']
        code = meta['ins_code']
        received = closings.get(code)
        row = dict(symbol=meta['symbol_fa'], ins_code=code, instrument_type_code=meta['instrument_type_code'],
                   sector_code=meta.get('sector_code'), sector_name=sectors.get(meta.get('sector_code')),
                   status='unavailable', closing_return=None, last_return=None, value_rial=None,
                   market_cap_observed_rial=None, market_cap_shares_historical_date_verified=False,
                   one_price_up_proxy=False, one_price_down_proxy=False)
        rows.append(row)
        if not isinstance(received, dict):
            continue
        if received.get('ins_code') != code or received.get('identity_attestation') == 'conflicting_response':
            row['status'] = 'identity_conflict'
            continue
        candle = received.get('candle', {})
        row['source_date'] = candle.get('date')
        row['received_at'] = received.get('received_at')
        row['provider_timestamp'] = received.get('provider_timestamp')
        received_clock = _clock(row['received_at'])
        stamp = _clock(received.get('provider_timestamp'))
        if (received_clock is None or received_clock > now or stamp is None
                or stamp > received_clock or stamp.date().isoformat() != candle.get('date')):
            row['status'] = 'unknown_or_conflicting_date'
            continue
        if candle.get('date') != session_date:
            row['status'] = 'stale_session' if candle.get('date', '') < session_date else 'future_session'
            continue
        if number(candle.get('volume'), integer=True) == 0 and number(candle.get('trade_count'), integer=True) == 0:
            row['status'] = 'not_traded'
            continue
        if not _price_quality(candle):
            row['status'] = 'invalid_candle'
            continue
        row.update(status='traded', value_rial=number(candle['value'], positive=True),
                   volume=number(candle['volume'], positive=True, integer=True),
                   close_final_raw=number(candle.get('close_final_raw'), positive=True),
                   close_raw=number(candle.get('close_raw'), positive=True))
        ref = number(candle.get('ref_price_raw'), positive=True)
        row['reference_known'] = ref is not None
        if ref is not None:
            if row['close_final_raw'] is not None:
                row['closing_return'] = row['close_final_raw'] / ref - 1
            row['last_return'] = row['close_raw'] / ref - 1
            # تک‌قیمت فقط یک قرینه کندلی است؛ صف، ظرفیت اجرا یا حد مجاز را اثبات نمی‌کند.
            single = candle['high_raw'] == candle['low_raw']
            row['one_price_up_proxy'] = single and row['last_return'] >= .005
            row['one_price_down_proxy'] = single and row['last_return'] <= -.005
        shares = number(meta.get('shares_outstanding'), positive=True, integer=True)
        if shares and row['close_final_raw']:
            cap = shares * row['close_final_raw']
            row['market_cap_observed_rial'] = cap if math.isfinite(cap) else None
    stats = _stats(rows)
    sectors_grouped = defaultdict(list)
    for row in rows:
        sectors_grouped[row['sector_code']].append(row)
    industries = [dict(code=code, name=sectors.get(code), **_stats(group))
                  for code, group in sectors_grouped.items()]
    industries.sort(key=lambda item: (item['median_closing_return'] is None,
                                     -(item['median_closing_return'] or 0), str(item['code'])))
    headlines = []
    for overview in overviews:
        if not isinstance(overview, dict):
            continue
        stamp = _clock(overview.get('provider_timestamp'))
        received = _clock(overview.get('received_at'))
        same_session = (stamp is not None and received is not None and stamp <= received <= now
                        and stamp.date().isoformat() == session_date)
        headline = {key: overview.get(key) for key in ('flow', 'url', 'received_at', 'provider_timestamp', 'market_state')}
        headline.update(status='session_context_matched' if same_session else 'unknown_or_other_session',
                        index_clock_independently_verified=False)
        for label in ('index', 'equal_weight'):
            current = number(overview.get(label + '_value'), positive=True)
            raw_change = overview.get(label + '_change')
            try:
                change = float(raw_change) if not isinstance(raw_change, bool) else float('nan')
            except (TypeError, ValueError, OverflowError):
                change = float('nan')
            valid = same_session and current is not None and math.isfinite(change) and current - change > 0
            headline[label + '_value'] = current if valid else None
            headline[label + '_change'] = change if valid else None
            headline[label + '_return'] = change / (current - change) if valid else None
        headlines.append(headline)
    traded = [row for row in rows if row['status'] == 'traded']
    known = [row for row in traded if row['closing_return'] is not None]
    by_value = sorted(traded, key=lambda row: (-row['value_rial'], row['ins_code']))
    stats['top10_value_fraction'] = (sum(row['value_rial'] for row in by_value[:10]) / stats['value_rial']
                                     if stats['value_rial'] else None)
    quality_failures = any(row['status'] not in ('traded', 'not_traded') for row in rows)
    return dict(version=MARKET_REVIEW_VERSION, session_date=session_date, observed_at=observed_at,
                status='partial' if not rows or quality_failures or stats['return_unknown_count'] else 'observed',
                scope_types=[300, 303], excluded_rows=exclusions, summary=stats, sectors=industries,
                official_overviews=headlines, instruments=rows,
                top_value=by_value[:15], gainers=sorted(known, key=lambda row: (-row['closing_return'], row['ins_code']))[:15],
                losers=sorted(known, key=lambda row: (row['closing_return'], row['ins_code']))[:15],
                largest_observed=[row for row in sorted(traded, key=lambda row: -(row['market_cap_observed_rial'] or 0))
                                  if row['market_cap_observed_rial'] is not None][:15],
                return_definition='official_close / same_session_reference - 1; not holding-period total return',
                official_overview_scope_equals_common_stocks=False, queue_time_verified=False,
                client_value_date_alignment_verified=False, independent_source_verification=False,
                historical_universe_verified=False, accepted_for_signals=False, holdout_opened=False,
                descriptive_period_exposed=session_date, independent_validation_for_derived_hypotheses=False)
