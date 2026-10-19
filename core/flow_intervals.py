"""Conservative estimates between archived retail counters, never individual trade detection."""

from datetime import datetime
from math import isfinite
from numbers import Real

from config import settings
from core.timeutils import TEHRAN_TZ

INTERVAL_VERSION = 'retail-counter-intervals-v2'


def _valid(row):
    captured, provider = (datetime.fromisoformat(row[k]) for k in ('captured_at', 'provider_timestamp'))
    if captured.tzinfo is None or provider.tzinfo is None:
        raise ValueError('زمان بدون منطقه زمانی')
    captured, provider = captured.astimezone(TEHRAN_TZ), provider.astimezone(TEHRAN_TZ)
    if (row.get('source') != 'tsetmc' or row.get('price_unit') != settings.PRICE_UNIT
            or captured.date() != provider.date() or not -5 <= (captured-provider).total_seconds() <= 45):
        raise ValueError('زمان/منبع ناسازگار')
    fields = ['volume', 'last_price', 'real_buy_volume', 'real_sell_volume', 'real_buy_count', 'real_sell_count']
    if any(not isinstance(row[k], Real) or isinstance(row[k], bool) or not 0 <= row[k] < 2**53 for k in fields):
        raise ValueError('عدد بومی، نامنفی و در دامنه دقیق لازم است')
    numbers = {k: float(row[k]) for k in fields}
    if not all(isfinite(v) and v >= 0 for v in numbers.values()) or numbers['last_price'] <= 0:
        raise ValueError('عدد نامعتبر')
    if any(value != int(value) for field, value in numbers.items() if field != 'last_price'):
        raise ValueError('شمار و حجم سهم باید صحیح باشند')
    for side in ('buy', 'sell'):
        count, volume = (numbers[f'real_{side}_{field}'] for field in ('count', 'volume'))
        if count != int(count) or (count > 0) != (volume > 0) or volume > numbers['volume']:
            raise ValueError('شمارنده ناسازگار')
    return captured, provider, numbers


def estimate_retail_intervals(records, *, threshold_rial=2_000_000_000, max_gap_seconds=45):
    """Consume chronological records for ONE symbol; baseline each day/reset/gap.

    Pending volume with no new count is accumulated only within an observed contiguous
    segment. The end price times volume/new-count is a proxy, not a cash-flow ledger.
    Archive gaps may reflect deduplication; they cannot establish polling outages.
    """
    if isinstance(threshold_rial, bool) or not isfinite(float(threshold_rial)) or threshold_rial <= 0:
        raise ValueError('آستانه باید مثبت و معلوم باشد')
    if isinstance(max_gap_seconds, bool) or not isfinite(float(max_gap_seconds)) or not 0 < max_gap_seconds <= 45:
        raise ValueError('فاصله مجاز باید مثبت و حداکثر ۴۵ ثانیه باشد')
    records = list(records)
    symbols = {r.get('symbol', r.get('symbol_fa')) for r in records}
    if len(symbols) > 1:
        raise ValueError('شمارنده نمادهای مختلف با هم قابل تفاضل نیست')
    previous, pending, events, rejected = None, {}, [], {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for row in records:
        try:
            captured, provider, values = _valid(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            reject('invalid_snapshot')
            previous, pending = None, {}
            continue
        current = (captured, provider, values)
        if previous is None:
            previous = current
            continue
        old_capture, old_provider, old = previous
        gap = (captured-old_capture).total_seconds()
        if captured.date() != old_capture.date():
            reason = 'new_session'
        elif gap <= 0 or (provider-old_provider).total_seconds() < 0:
            reason = 'nonmonotonic_clock'
        elif gap > max_gap_seconds or (provider-old_provider).total_seconds() > max_gap_seconds:
            reason = 'archive_gap'
        elif any(values[k] < old[k] for k in values if k != 'last_price'):
            reason = 'counter_revision_or_reset'
        else:
            reason = None
        if reason:
            reject(reason)
            # An out-of-order row cannot become a baseline and count an already
            # processed interval a second time. A new valid row must establish it.
            previous, pending = (None if reason == 'nonmonotonic_clock' else current), {}
            continue
        for side in ('buy', 'sell'):
            dv = values[f'real_{side}_volume']-old[f'real_{side}_volume']
            dc = values[f'real_{side}_count']-old[f'real_{side}_count']
            accumulated, start = pending.get(side, (0, old_capture))
            accumulated += dv
            if dc > 0:
                estimate = accumulated/dc*values['last_price']
                if estimate > threshold_rial:
                    events.append(dict(side=side, start=start.isoformat(), end=captured.isoformat(),
                        delta_volume=accumulated, delta_count=dc, price_proxy_rial=values['last_price'],
                        estimated_per_new_count_rial=estimate, single_trade_proven=False))
                pending.pop(side, None)
            elif accumulated > 0:
                pending[side] = (accumulated, start)
        previous = current
    return dict(version=INTERVAL_VERSION, threshold_rial=threshold_rial, max_gap_seconds=max_gap_seconds,
        events=events, rejected_segments=rejected, individual_trades_observed=False,
        independent_client_counter_clock=False, complete_session_proven=False,
        formula='accumulated retail volume / positive count increment * end last price; strict > threshold')
