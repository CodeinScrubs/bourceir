"""Separate rare-event label contract with byte-bound flat official marks in raw gaps."""
from config import settings
from core.no_trade_valuation import parse_no_trade_receipt
from core.text import normalize_symbol
from research.rare_events import HORIZONS, _compute_rare_events, _day, _panel, summarize_rare_events

VERSION = 'rare-event-official-marks-v1'
MAX_RECEIPTS = 12000


def prepare_mark_ledger(prices, calendar, receipts, *, observed_through):
    """Validate date scopes before any source-byte decoding. Never replace an existing raw row."""
    end = _day(observed_through)
    if end >= settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('دفتر ارزش، مرز مهروموم را باز نمی‌کند')
    dates = list(calendar)
    if not dates or dates != sorted(set(dates)) or any(_day(d) != d for d in dates):
        raise ValueError('تقویم ثابت، مرتب و یکتا لازم است')
    keys = _panel(prices, {'symbol', 'date'})
    if keys.date.gt(end).any():
        raise ValueError('قیمت پس از زمان مشاهده وارد دفتر ارزش نمی‌شود')
    if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
        raise ValueError('دفتر ارزش باید حداکثر دوازده‌هزار رسید داشته باشد')
    scope, seen = [], set()
    names = set(keys.symbol)
    for record in receipts:
        task = record.get('task') if isinstance(record, dict) else None
        if not isinstance(task, dict) or not isinstance(task.get('symbol'), str):
            raise ValueError('هویت و تاریخ رسید ارزش لازم است')
        day = _day(task.get('date'))
        name = normalize_symbol(task['symbol'])
        if day > end or day not in dates or name != task['symbol'] or name not in names or (name, day) in seen:
            raise ValueError('دامنه تاریخ، نماد یا یکتایی رسید ارزش معتبر نیست')
        seen.add((name, day))
        scope.append((record, name, day))
    raw_dates = {name:set(group.date) for name,group in keys.loc[keys.symbol.isin({name for _,name,_ in scope})].groupby('symbol')}
    if any(day in raw_dates[name] for _,name,day in scope):
        raise ValueError('رسید ارزش اجازه جایگزینی مشاهده خام موجود را ندارد')
    output = []
    for record, name, day in scope:
        try:
            row = parse_no_trade_receipt(record)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            row = dict(symbol=name, date=day, status='rejected', error_type=type(error).__name__,
                flat_mark_eligible=False, accepted_for_features=False, accepted_for_signals=False, profitability_proven=False)
        output.append(row)
    return output


def compute_marked_rare_events(prices, decisions, calendar, *, observed_through, receipts, horizons=HORIZONS, threshold=2.):
    calendar = list(calendar)
    ledger = prepare_mark_ledger(prices, calendar, receipts, observed_through=observed_through)
    lookup = {(r['symbol'],r['date']):r for r in ledger}
    result = _compute_rare_events(prices, decisions, calendar, observed_through=observed_through,
        horizons=horizons, threshold=threshold, _marks=lookup)
    result['label_contract'] = VERSION
    result['label_contract'] = result['label_contract'].astype('string')
    return result


def summarize_marked_events(outcomes):
    if not outcomes.label_contract.eq(VERSION).all():
        raise ValueError('نسخه برچسب دفتر ارزش ناسازگار است')
    result = summarize_rare_events(outcomes)
    for summary in result:
        group = outcomes.loc[outcomes.horizon_sessions.eq(summary['horizon_sessions'])]
        summary.update(label_contract=VERSION, paths_with_no_trade_marks=int(group.no_trade_mark_sessions.gt(0).sum()),
            no_trade_mark_uses=int(group.no_trade_mark_sessions.sum()),
            economic_profit_verified=False, accepted_for_signals=False)
    return result
