"""سن قیمت و مدت خدمت چرخه؛ مخرج تمام تلاش‌ها، مستقل از حذف تکرار ذخیره‌سازی."""

import math
from numbers import Real

import numpy as np

VERSION = 'observed-feed-latency-v1'


def _number(value):
    if not isinstance(value, Real) or isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _quantiles(values):
    return dict(p50=None, p95=None, maximum=None) if not values else dict(
        p50=float(np.quantile(values, .5)), p95=float(np.quantile(values, .95)), maximum=max(values))


def feed_latency(durations, quotes, *, expected_slots, interval_seconds):
    """هر خانه = یک نماد در یک تلاش، شامل خطا و شروع بدون پایان.

    سن در پایان چرخه است؛ پیوند رسید بعد از بررسی محتوا اعلام می‌شود. مدت محفوظ از
    ساعت monotonic است، نه سنجش خالص شبکه. صدک‌ها با درون‌یابی خطی محاسبه می‌شوند.
    """
    durations, quotes = list(durations), list(quotes)
    if (type(expected_slots) is not int or expected_slots < len(quotes)
            or _number(interval_seconds) is None or interval_seconds <= 0):
        raise ValueError('مخرج نامنفی و فاصله مثبت صریح لازم است')
    known = [number for item in durations if (number := _number(item)) is not None and number >= 0]
    ages, fresh_bound, future = [], 0, 0
    for quote in quotes:
        if type(quote.get('bound')) is not bool:
            raise ValueError('وضعیت پیوند رسید باید بولی صریح باشد')
        age = _number(quote.get('age_seconds'))
        if age is None:
            continue
        ages.append(age)
        future += age < 0
        fresh_bound += quote['bound'] and 0 <= age <= 45
    return dict(version=VERSION, attempted_polls=len(durations), known_duration_polls=len(known),
        unknown_duration_polls=len(durations)-len(known), poll_service_seconds=_quantiles(known),
        polls_exceeding_requested_interval=sum(number > interval_seconds for number in known),
        expected_instrument_poll_slots=expected_slots, observed_quote_slots=len(quotes),
        known_price_age_slots=len(ages), unknown_or_missing_price_age_slots=expected_slots-len(ages),
        price_age_at_poll_finish_seconds=_quantiles(ages), future_price_timestamps=future,
        fresh_bound_slots=int(fresh_bound), observed_fresh_bound_fraction=fresh_bound/expected_slots if expected_slots else None,
        full_session_uptime_verified=False, independent_book_clock_verified=False,
        network_only_latency_measured=False, order_fill_latency_measured=False, accepted_for_signals=False)
