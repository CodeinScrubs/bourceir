"""پرسش توصیفی نسخه‌دار: رتبه مشاهده‌شده و شواهد ترتیب قیمت، بدون سیگنال."""

import math
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

from core.text import normalize_symbol

VERSION = 'market-query-v3'
METRICS = {'market_cap_rial', 'confirmed_count', 'ambiguous_count', 'possible_count', 'valid_sessions',
           'observed_sessions', 'avg_value_rial', 'sector_code', 'symbol_fa'}


def decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and not isinstance(value, bool) else None


def iso_day(value):
    if not isinstance(value,str):
        raise ValueError('تاریخ باید متن میلادی YYYY-MM-DD باشد')
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError('تاریخ باید میلادی YYYY-MM-DD باشد')
    return day


def validate_spec(spec):
    allowed = {'start', 'end', 'cap_low', 'cap_high', 'dip_pct', 'rally_pct', 'where', 'kind'}
    if not isinstance(spec, dict) or set(spec)-allowed or not {'start', 'end'} <= set(spec):
        raise ValueError('قرارداد پرسش یا نام فیلد آن نامعتبر است')
    if iso_day(spec['start']) > iso_day(spec['end']):
        raise ValueError('ابتدای بازه باید پیش از انتهای آن باشد')
    result = dict(start=spec['start'], end=spec['end'], cap_low=10., cap_high=40., dip_pct=-2., rally_pct=2.,
                  where=None, kind='dip_rally') | spec
    low, high, dip, rally = [decimal(result[k]) for k in ('cap_low', 'cap_high', 'dip_pct', 'rally_pct')]
    if (any(v is None for v in (low, high, dip, rally)) or not 0 <= low < high <= 100 or
        not -100 < dip < 0 < rally <= 1000 or result['kind'] not in ('dip_rally', 'market_caps')):
        raise ValueError('بازه صدکی، آستانه قیمت یا نوع پرسش معتبر نیست')
    validate_predicate(result['where'])
    return result


def validate_predicate(node, depth=0, _budget=None):
    budget = [100] if _budget is None else _budget
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError('تعداد اجزای شرط از سقف صد بیشتر است')
    if node is None:
        return
    if not isinstance(node, dict) or depth > 5:
        raise ValueError('شرط پرسش نامعتبر یا بیش از حد تو در تو است')
    if set(node) in ({'all'}, {'any'}):
        children = node[next(iter(node))]
        if not isinstance(children, list) or not 1 <= len(children) <= 20:
            raise ValueError('گروه شرط باید یک تا بیست عضو داشته باشد')
        for child in children:
            if child is None:
                raise ValueError('عضو خالی شرط مجاز نیست')
            validate_predicate(child, depth+1, budget)
    elif (set(node) != {'field', 'op', 'value'} or node['field'] not in METRICS or
          node['op'] not in ('eq', 'gt', 'gte', 'lt', 'lte', 'in', 'between')):
        raise ValueError('فیلد یا عملگر شرط پشتیبانی نمی‌شود؛ کد دلخواه اجرا نمی‌شود')
    else:
        value, op = node['value'], node['op']
        categorical = node['field'] in ('sector_code', 'symbol_fa')
        if categorical and op not in ('eq', 'in'):
            raise ValueError('فیلد متنی فقط برابری یا عضویت می‌پذیرد')
        if op in ('in', 'between'):
            if not isinstance(value, list) or not 1 <= len(value) <= 100 or (op == 'between' and len(value) != 2):
                raise ValueError('مقدار شرط فهرستی نامعتبر است')
            values = value
        else:
            values = [value]
        if categorical:
            if any(not isinstance(v, str) or len(v) > 200 for v in values):
                raise ValueError('مقدار شرط متنی معتبر نیست')
        elif any(decimal(v) is None for v in values):
            raise ValueError('مقدار شرط باید عدد محدود باشد')
        if op == 'between' and decimal(value[0]) > decimal(value[1]):
            raise ValueError('دو حد شرط ترتیب نادرست دارند')


def predicate(node, row):
    if node is None:
        return True
    if 'all' in node or 'any' in node:
        key = 'all' if 'all' in node else 'any'
        values = [predicate(child, row) for child in node[key]]
        if key == 'all':
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    actual = row.get(node['field'])
    if actual is None:
        return None
    expected, op = node['value'], node['op']
    if node['field'] not in ('sector_code', 'symbol_fa'):
        actual = decimal(actual)
        expected = [decimal(v) for v in expected] if isinstance(expected, list) else decimal(expected)
        if actual is None:
            return None
    if op == 'in':
        return actual in expected
    if op == 'between':
        return expected[0] <= actual <= expected[1]
    return {'eq': lambda: actual == expected, 'gt': lambda: actual > expected, 'gte': lambda: actual >= expected,
            'lt': lambda: actual < expected, 'lte': lambda: actual <= expected}[op]()


def rank_caps(instruments, prices, *, low=10., high=40.):
    """کران رتبه شامل گم‌شده‌ها و تمام اعضای هم‌ارزش است؛ برش اجباریِ tie نداریم."""
    n, rows, identities = len(instruments), [], set()
    for meta in instruments:
        symbol = normalize_symbol(meta['symbol_fa'])
        identity = str(meta['ins_code'])
        if identity in identities or any(r['symbol_fa'] == symbol for r in rows):
            raise ValueError('شناسه یا نماد تکراری در جامعه پرسش')
        if meta.get('instrument_type_code') not in (300, 303) or not meta.get('is_active', True) or symbol[-1:].isdigit():
            raise ValueError('جامعه پرسش فقط سهام عادی فعال است')
        identities.add(identity)
        quote = prices.get(meta['id'], {})
        shares, price = decimal(meta.get('shares_outstanding')), decimal(quote.get('close_raw'))
        known = (shares is not None and price is not None and 0 < shares <= 2**53-1 and shares == shares.to_integral_value()
                 and 0 < price <= 2**53-1 and price == price.to_integral_value()
                 and quote.get('price_unit') == 'Rial' and quote.get('source') == 'tsetmc')
        rows.append(dict(symbol_id=meta['id'], symbol_fa=symbol, name_fa=meta.get('name_fa'), ins_code=identity,
            sector_code=meta.get('sector_code'), sector_name=meta.get('sector_name'), shares_outstanding=meta.get('shares_outstanding'),
            metadata_observed_at=meta.get('updated_at'), historical_effective_shares_verified=False,
            price_rial=quote.get('close_raw') if known else None, price_date=quote.get('date'),
            price_captured_at=quote.get('captured_at'), price_provider_timestamp=quote.get('provider_timestamp'),
            price_received_at=quote.get('quote_received_at'), price_kind=quote.get('quote_kind', 'daily_bar'),
            market_cap_rial=int(shares)*int(price) if known else None))
    known = [r['market_cap_rial'] for r in rows if r['market_cap_rial'] is not None]
    missing = n-len(known)
    lo, hi = decimal(low), decimal(high)
    if lo is None or hi is None or not 0 <= lo < hi <= 100:
        raise ValueError('برش صدکی نامعتبر است')
    first, last = n-math.floor(n*hi/100)+1, n-math.floor(n*lo/100)
    for row in rows:
        cap = row['market_cap_rial']
        lower = 1+sum(c > cap for c in known) if cap is not None else None
        upper = sum(c >= cap for c in known)+missing if cap is not None else None
        membership = ('unknown' if lower is None else 'inside' if lower >= first and upper <= last else
                      'outside' if upper < first or lower > last else 'boundary_uncertain')
        row.update(rank_lower=lower, rank_upper=upper, slice_membership=membership)
    rows.sort(key=lambda r: (r['market_cap_rial'] is None, -(r['market_cap_rial'] or 0), r['symbol_fa']))
    return dict(rows=rows, universe_total=n, caps_known=len(known), caps_unknown=missing,
                slice_first_rank=first, slice_last_rank=last, membership_counts=dict(Counter(r['slice_membership'] for r in rows)),
                rank_is_current_observation=True, historical_rank_verified=False)


def daily_evidence(row, *, dip_pct=-2., rally_pct=2.):
    """کندل فقط در دو حالت ترتیب را اثبات می‌کند؛ سبز بودن به‌تنهایی کافی نیست."""
    values = {key: decimal(row.get(key)) for key in ('low_raw', 'high_raw', 'ref_price_raw', 'volume')}
    result = dict(row, event_confirmed=None, event_possible=None, evidence='invalid_daily', tick_evidence=None)
    if (row.get('source') != 'tsetmc' or row.get('price_unit') != 'Rial' or
        any(v is None or v <= 0 for v in values.values())):
        return result
    low, high, ref, _ = values.values()
    if values['volume'] != values['volume'].to_integral_value():
        return result
    op,last=decimal(row.get('open_raw')),decimal(row.get('close_raw'))
    result['unavailable_price_fields']=[k for k,v in [('open_raw',op),('close_raw',last)] if v is None]
    if low>high or any(v is not None and (v<=0 or not low<=v<=high) for v in (op,last)):
        return result
    dip, rally = ref*(1+decimal(dip_pct)/100), ref*(1+decimal(rally_pct)/100)
    candidate = low <= dip and high >= rally
    open_proof,last_proof=op is not None and op<=dip,last is not None and last>=rally
    confirmed = candidate and (open_proof or last_proof)
    reason = ('not_touched' if not candidate else 'open_before_high' if open_proof else
              'low_before_last' if last_proof else 'order_unknown')
    result.update(event_confirmed=True if confirmed else None if candidate else False,
                  event_possible=bool(candidate), evidence=reason,
                  dip_level_rial=str(dip), rally_level_rial=str(rally))
    return result


def trade_evidence(observation, daily, *, dip_pct=-2., rally_pct=2.):
    """نسخه تکراری یکسان حذف می‌شود؛ تعارض شماره، تاریخ یا OHLCV قطعیت نمی‌سازد."""
    result = dict(event_confirmed=None, complete=False, contradictory=False, duplicate_rows=0, issues=[], witness=None,
                  payload_date_verified=False, payload_identity_verified=False)
    if (not isinstance(observation,dict) or not isinstance(daily,dict) or observation.get('source') != 'tsetmc' or observation.get('price_unit') != 'Rial' or
        observation.get('requested_date') != daily.get('date') or
        str(observation.get('ins_code')) != str(daily.get('ins_code')) or observation.get('endpoint_is_grouped') is not False):
        result['issues'].append('منبع یا تاریخ درخواست ریزمعاملات سازگار نیست')
        return result
    if daily_evidence(daily, dip_pct=dip_pct, rally_pct=rally_pct)['event_possible'] is None:
        result['issues'].append('کندل روز برای تطبیق ریزمعاملات معتبر نیست')
        return result
    response=observation.get('response')
    rows = response.get('tradeHistory') if isinstance(response,dict) else None
    if not isinstance(rows, list) or not rows:
        result['issues'].append('ریزمعامله در پاسخ موجود نیست')
        return result
    by_number, dates_verified, identities_verified = {}, True, True
    try:
        for raw in rows:
            if not isinstance(raw,dict):
                raise ValueError('قالب ردیف ریزمعامله نامعتبر است')
            raw_date, raw_ins = raw.get('dEven'), raw.get('insCode')
            if raw_date not in (None, 0) and str(raw_date) != daily['date'].replace('-', ''):
                raise ValueError('تاریخ داخل پاسخ با روز درخواست متفاوت است')
            if raw_ins not in (None, '', 0) and str(raw_ins) != str(daily['ins_code']):
                raise ValueError('شناسه داخل پاسخ متفاوت است')
            dates_verified &= raw_date not in (None, 0)
            identities_verified &= raw_ins not in (None, '', 0)
            if isinstance(raw.get('canceled'), bool) or raw.get('canceled') not in (0, 1):
                raise ValueError('وضعیت ابطال معامله نامعلوم است')
            number, clock, price, volume = [decimal(raw.get(k)) for k in ('nTran', 'hEven', 'pTran', 'qTitTran')]
            if any(v is None for v in (number, clock, price, volume)) or number <= 0 or number != int(number):
                raise ValueError('شماره یا داده معامله نامعتبر است')
            if clock != int(clock) or not 0 <= int(clock)//10000 <= 23 or not 0 <= int(clock)//100 % 100 < 60 or not 0 <= int(clock) % 100 < 60:
                raise ValueError('ساعت معامله نامعتبر است')
            normalized = dict(number=int(number), clock=int(clock), price=price, volume=volume, canceled=raw['canceled'])
            if int(number) in by_number:
                if by_number[int(number)] != normalized:
                    raise ValueError('دو نسخه متعارض از یک شماره معامله')
                result['duplicate_rows'] += 1
            by_number[int(number)] = normalized
        active = [r for _, r in sorted(by_number.items()) if not r['canceled']]
        if not active or any(r['price'] <= 0 or r['volume'] <= 0 or r['volume'] != int(r['volume']) for r in active):
            raise ValueError('قیمت یا حجم معامله فعال نامعتبر است')
        if any(a['clock'] > b['clock'] for a, b in zip(active, active[1:])):
            raise ValueError('شماره و ساعت معاملات ترتیب سازگار ندارند')
        prices = [r['price'] for r in active]
        if min(prices) < decimal(daily['low_raw']) or max(prices) > decimal(daily['high_raw']):
            result['contradictory'] = True
            raise ValueError('قیمت ریزمعامله بیرون از کندل روز است')
        result.update(payload_date_verified=dates_verified, payload_identity_verified=identities_verified)
        count, volume = decimal(daily.get('trade_count')), decimal(daily.get('volume'))
        total_value = sum(r['price']*r['volume'] for r in active)
        total_volume = sum(r['volume'] for r in active)
        value = decimal(daily.get('value'))
        invalid_present = any(daily.get(k) is not None and decimal(daily[k]) is None
                              for k in ('trade_count', 'value'))
        if (invalid_present or (count is not None and (count <= 0 or count != count.to_integral_value()))
                or (value is not None and value <= 0)):
            result['contradictory'] = True
            raise ValueError('تعداد یا ارزش معاملات روز نامعتبر است')
        checks = dict(count=count == len(active), volume=volume == total_volume,
            open=prices[0] == decimal(daily['open_raw']) if decimal(daily['open_raw']) is not None else None,
            last=prices[-1] == decimal(daily['close_raw']) if decimal(daily['close_raw']) is not None else None,
            low=min(prices) == decimal(daily['low_raw']), high=max(prices) == decimal(daily['high_raw']),
            value=value == total_value)
        result.update(reconciliation=checks, complete=all(checks.values()), active_rows=len(active),
                      date_binding='payload' if dates_verified else 'dated_request_and_daily_reconciliation')
        # کسری می‌تواند شاهد ناقص باشد؛ افزونی یا اختلاف پس از پوشش کامل، تعارض است.
        excess = ((count is not None and len(active) > count) or total_volume > volume
                  or (value is not None and total_value > value))
        exhausted = count == len(active) or total_volume == volume or value == total_value
        available_checks = [ok for key, ok in checks.items()
                            if ok is not None and (key != 'count' or count is not None)
                            and (key != 'value' or value is not None)]
        if excess or (exhausted and not all(available_checks)):
            result['contradictory'] = True
            raise ValueError('جمع یا مسیر ریزمعاملات با آمار روز تعارض دارد؛ وقوع الگو تأیید نمی‌شود')
        dip = decimal(daily['ref_price_raw'])*(1+decimal(dip_pct)/100)
        rally = decimal(daily['ref_price_raw'])*(1+decimal(rally_pct)/100)
        first = None
        for row in active:
            if first is None and row['price'] <= dip:
                first = row
            elif first is not None and row['price'] >= rally:
                result['witness'] = dict(dip_number=first['number'], dip_clock=first['clock'], dip_price_rial=str(first['price']),
                    rally_number=row['number'], rally_clock=row['clock'], rally_price_rial=str(row['price']))
                break
        if result['complete']:
            result['event_confirmed'] = result['witness'] is not None
        elif result['witness'] and dates_verified and identities_verified:
            result['event_confirmed'] = True
        if not result['complete']:
            result['issues'].append('ریزمعاملات با تمام آمار روز تطبیق ندارد؛ نبود الگو ثابت نشده است')
    except (ValueError, KeyError, TypeError, InvalidOperation) as error:
        result.update(event_confirmed=None, complete=False, witness=None)
        result['issues'].append(str(error))
    return result


def summarize_symbol(meta, days, *, market_dates):
    confirmed = sum(r['event_confirmed'] is True for r in days)
    ambiguous = sum(r['event_confirmed'] is None and r['event_possible'] is True for r in days)
    invalid = sum(r['event_possible'] is None for r in days)
    values = [decimal(r.get('value')) for r in days]
    avg_value = float(sum(values)/len(values)) if values and all(v is not None and v >= 0 for v in values) else None
    missing = len(set(market_dates)-{r['date'] for r in days})
    return dict(meta, confirmed_count=confirmed, ambiguous_count=ambiguous, possible_count=confirmed+ambiguous+invalid,
        observed_sessions=len(days), valid_sessions=len(days)-invalid, invalid_sessions=invalid, missing_market_dates=missing,
        avg_value_rial=avg_value, event_rate_among_valid_pct=100*confirmed/(len(days)-invalid) if len(days)>invalid else None,
        full_year_count_verified=False, predictive_validation=False, execution_proven=False)
