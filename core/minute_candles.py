"""Reconstruct closed, unadjusted minute bars from a dated exchange trade tape."""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import groupby

from core.instrument_history import receipt_time
from core.market_day import market_day_request
from core.price_revision import daily_request
from core.queue_fall_study import trade_request
from core.text import to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, jalali_to_iso, now_tehran

VERSION = 'tsetmc-minute-candles-v1'
CONTROLS = {'open_rial': 'priceFirst', 'high_rial': 'priceMax', 'low_rial': 'priceMin',
            'close_rial': 'pDrCotVal', 'volume_shares': 'qTotTran5J',
            'trade_count': 'zTotTran', 'trade_value_rial': 'qTotCap'}


def closed_trade_day(value):
    """Resolve a calendar day in Tehran without shifting to another trading day."""
    today = now_tehran().date()
    value = to_en_digits(value).strip().lower()
    if value in ('دیروز', 'yesterday'):
        return (today - timedelta(days=1)).isoformat()
    if re.fullmatch(r'[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}', value):
        value = jalali_to_iso(value).split('T', 1)[0]
    if not is_canonical_iso_date(value) or value >= today.isoformat():
        raise ValueError('تاریخ یک روز کامل گذشته را به صورت میلادی YYYY-MM-DD یا شمسی YYYY/MM/DD بدهید')
    return value


def _integer(value, field, *, minimum=0):
    if (type(value) not in (int, float, Decimal) or isinstance(value, Decimal) and not value.is_finite()
            or not minimum <= value < 2**53):
        raise ValueError(f'عدد صحیح معتبر برای {field} در پاسخ بورس موجود نیست')
    number = int(value)
    if number != value:
        raise ValueError(f'عدد صحیح معتبر برای {field} در پاسخ بورس موجود نیست')
    return number


def _clock(value):
    clock = _integer(value, 'hEven', minimum=1)
    return time(clock // 10000, clock // 100 % 100, clock % 100)


def _bound_observation(observation, code, day, url, *, trade=False):
    url_key, time_key = ('source_url', 'captured_at') if trade else ('url', 'received_at')
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc'
            or observation.get('price_unit') != 'Rial' or observation.get('ins_code') != code
            or observation.get('requested_date') != day or observation.get(url_key) != url
            or (trade and observation.get('endpoint_is_grouped') is not False)):
        raise ValueError('هویت، تاریخ، نشانی یا واحد پاسخ بورس با درخواست مطابقت ندارد')
    received = receipt_time(observation.get(time_key)).astimezone(TEHRAN_TZ)
    if received.date() <= date.fromisoformat(day) or received > now_tehran():
        raise ValueError('پاسخ باید پس از پایان روز درخواستی به وقت تهران ثبت شده باشد')


def _daily_controls(observation, code, day, url):
    _bound_observation(observation, code, day, url)
    payload = observation.get('payload')
    native = payload.get('closingPriceDaily') if isinstance(payload, dict) else None
    if (not isinstance(native, dict) or str(native.get('insCode')) != code
            or str(native.get('dEven')) != day.replace('-', '')):
        raise ValueError('شناسه یا تاریخ داخل آمار روزانه بورس تأیید نشد')
    totals = {name: _integer(native.get(field), field) for name, field in CONTROLS.items()}
    counts = [totals[key] for key in ('trade_count', 'volume_shares', 'trade_value_rial')]
    if any(counts) and not all(counts):
        raise ValueError('تعداد، حجم و ارزش معاملات روزانه ناسازگارند')
    return totals, native


def _market_open_reference(code, day, direct, reference, report):
    """Pick the public market-day opening before seeing any prints, only when six controls agree."""
    if reference is None:
        return direct
    row, url = reference.get('row'), reference.get('url')
    digest = reference.get('body_sha256')
    if (not isinstance(row, dict) or str(row.get('insCode')) != code or url != market_day_request(day)
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or str(row.get('dEven')) not in (str(None), '', '0', day.replace('-', ''))):
        raise ValueError('شاهد روزانه سراسری با نماد یا تاریخ سازگار نیست')
    market = {key: _integer(row.get(field), field) for key, field in CONTROLS.items()}
    changed = [key for key in CONTROLS if market[key] != direct[key]]
    report.update(market_day_url=url, market_day_body_sha256=digest, market_open_rial=market['open_rial'],
                  market_open_minus_symbol_rial=market['open_rial'] - direct['open_rial'],
                  daily_source_disagreements=changed)
    if changed == ['open_rial'] and 0 < market['low_rial'] <= market['open_rial'] <= market['high_rial']:
        report['reference_basis'] = 'market_day_open_with_six_dated_symbol_controls'
        return market
    return direct


def _active_prints(rows, code, day):
    by_number = {}
    expected_day = day.replace('-', '')
    day_verified = identity_verified = True
    duplicate_rows = canceled_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('قالب ریزمعامله نامعتبر است')
        row_day, row_code = row.get('dEven'), row.get('insCode')
        if row_day in (None, 0, '0', '') and type(row_day) is not bool:
            day_verified = False
        elif str(row_day) != expected_day:
            raise ValueError('تاریخ یکی از ریزمعاملات متفاوت است')
        if row_code in (None, 0, '0', '') and type(row_code) is not bool:
            identity_verified = False
        elif str(row_code) != code:
            raise ValueError('شناسه یکی از ریزمعاملات متفاوت است')
        number = _integer(row.get('nTran'), 'nTran', minimum=1)
        canceled = _integer(row.get('canceled'), 'canceled')
        if canceled not in (0, 1):
            raise ValueError('وضعیت ابطال ریزمعامله نامعتبر است')
        canceled_rows += canceled
        trade = None if canceled else (
            _clock(row.get('hEven')),
            _integer(row.get('pTran'), 'pTran', minimum=1),
            _integer(row.get('qTitTran'), 'qTitTran', minimum=1),
        )
        if number in by_number:
            if by_number[number] != trade:
                raise ValueError('دو نسخه متعارض از یک شماره ریزمعامله')
            duplicate_rows += 1
        by_number[number] = trade
    active_numbers = sorted(number for number, trade in by_number.items() if trade is not None)
    active = [by_number[number] for number in active_numbers]
    if any(first[0] > second[0] for first, second in zip(active, active[1:])):
        raise ValueError('زمان معاملات با ترتیب شماره ریزمعامله سازگار نیست')
    return active, dict(raw_trade_rows=len(rows), duplicate_rows=duplicate_rows, canceled_rows=canceled_rows,
                        first_active_trade_number=active_numbers[0] if active_numbers else None,
                        trade_payload_date_verified=day_verified, trade_payload_identity_verified=identity_verified)


def _canceled_opening_witness(rows, first_active, opening):
    # The opening is the explicitly canceled first print. Require the entire
    # prefix to exist; skipping over an unknown print cannot explain the open.
    prefix = [row for row in rows if row['nTran'] < first_active]
    numbers = sorted({int(row['nTran']) for row in prefix})
    first_prices = [row.get('pTran') for row in prefix if row['nTran'] == 1]
    if len(numbers) != first_active - 1 or not first_prices or any(row['canceled'] != 1 for row in prefix):
        return None
    try:
        same_opening = all(_integer(price, 'pTran', minimum=1) == opening for price in first_prices)
    except ValueError:
        return None
    if not same_opening:
        return None
    return dict(canceled_trade_numbers=numbers, published_open_rial=opening, first_active_trade_number=first_active)


def reconcile_session(code, day, daily, tape, *, market_reference=None):
    """Require daily agreement and identify a separately reportable canceled opening."""
    code, daily_url = daily_request(code, day)
    report = dict(version=VERSION, source='tsetmc_trade_history', ins_code=code, date=day,
                  timezone='Asia/Tehran', interval_minutes=1, adjusted=False, price_unit='Rial',
                  minute_policy='traded_minutes_only', daily_source_url=daily_url,
                  trade_source_url=trade_request(code, day), bar_count=0, deltas=None)
    if daily is None:
        return [], report | dict(status='daily_unavailable')
    direct, native = _daily_controls(daily, code, day, daily_url)
    report.update(reference_basis='dated_symbol_daily', dated_symbol_open_rial=direct['open_rial'])
    official = _market_open_reference(code, day, direct, market_reference, report)
    report['official_daily'] = official
    low, opening, high = (official[name] for name in ('low_rial', 'open_rial', 'high_rial'))
    opening_outside = bool(official['trade_count']) and not low <= opening <= high
    check_canceled_opening = opening_outside and opening > 0 and report.get('daily_source_disagreements') == []
    if official['trade_count'] and (not 0 < low <= official['close_rial'] <= high
                                    or (opening_outside and not check_canceled_opening)):
        raise ValueError('قیمت‌های آمار روزانه بورس ناسازگارند')
    closing = native.get('pClosing')
    report['official_closing_price_rial'] = None if closing is None else _integer(closing, 'pClosing')
    if tape is None and not official['trade_count']:
        return [], report | dict(status='no_trades_reported')
    if tape is None:
        return [], report | dict(status='trade_history_unavailable')
    _bound_observation(tape, code, day, report['trade_source_url'], trade=True)
    response = tape.get('response')
    raw_rows = response.get('tradeHistory') if isinstance(response, dict) else None
    if not isinstance(raw_rows, list):
        raise ValueError('قالب پاسخ ریزمعاملات نامعتبر است')
    active, stats = _active_prints(raw_rows, code, day)
    report.update(stats)
    if not active:
        return [], report | dict(status='trade_history_empty' if official['trade_count'] else 'no_trades_reported')

    bars = []
    session_date = date.fromisoformat(day)
    for minute, group in groupby(active, key=lambda trade: trade[0].replace(second=0)):
        trades = list(group)
        prices = [trade[1] for trade in trades]
        stamp = datetime.combine(session_date, minute, tzinfo=TEHRAN_TZ)
        bars.append(dict(time_tehran=stamp.isoformat(), time_utc=stamp.astimezone(UTC).isoformat(),
                         open_rial=prices[0], high_rial=max(prices), low_rial=min(prices), close_rial=prices[-1],
                         volume_shares=sum(trade[2] for trade in trades), trade_count=len(trades),
                         trade_value_rial=sum(price * shares for _, price, shares in trades)))
    observed = dict(open_rial=bars[0]['open_rial'], high_rial=max(bar['high_rial'] for bar in bars),
                    low_rial=min(bar['low_rial'] for bar in bars), close_rial=bars[-1]['close_rial'],
                    volume_shares=sum(bar['volume_shares'] for bar in bars),
                    trade_count=sum(bar['trade_count'] for bar in bars),
                    trade_value_rial=sum(bar['trade_value_rial'] for bar in bars))
    deltas = {field: observed[field] - official[field] for field in CONTROLS}
    differing = [field for field, delta in deltas.items() if delta]
    witness = (_canceled_opening_witness(raw_rows, stats['first_active_trade_number'], opening)
               if check_canceled_opening and differing == ['open_rial'] else None)
    status = ('reconciled' if not differing else 'canceled_opening_conflict' if witness
              else 'invalid_source' if opening_outside else 'daily_mismatch')
    report.update(status=status, observed_bar_count=len(bars),
                  bar_count=len(bars) if status == 'reconciled' else 0, aggregate=observed, deltas=deltas,
                  first_trade_tehran=active[0][0].isoformat(), last_trade_tehran=active[-1][0].isoformat(),
                  first_bar_tehran=bars[0]['time_tehran'], last_bar_tehran=bars[-1]['time_tehran'])
    if witness:
        report['opening_cancellation'] = witness
    if status == 'invalid_source':
        report['detail'] = 'قیمت آغازِ آمار روزانه بیرون از دامنه است و شاهد کافی برای ابطال آغاز وجود ندارد'
    return bars if status in ('reconciled', 'canceled_opening_conflict') else [], report
