"""محاسبه دفتر مجازی با نقد مشترک؛ سناریو و مظنه هیچ‌کدام اجرای واقعی نیستند."""

import hashlib
import json
import math
from datetime import datetime

from core.journal import summarize_executions
from core.risk import RiskProfile, finite_number
from core.screening import current_snapshot
from core.timeutils import is_market_open

VERSION = 'paper-portfolio-v3'
SUPPORTED_VERSIONS = ('paper-portfolio-v1', 'paper-portfolio-v2', VERSION)
HORIZONS = ('intraday', 'swing', 'weeks_months')
MODES = ('manual', 'observed')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def integer(value):
    number = finite_number(value)
    if not number.is_integer() or number > 2**53 - 1:
        raise ValueError('ریال و تعداد باید عدد صحیح مثبت در محدوده دقیق باشند')
    return int(number)


def text(value, label, maximum=10000):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(f'{label} لازم است؛ حداکثر {maximum} نویسه')
    return value.strip()


def aware(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('زمان باید منطقه زمانی داشته باشد')
    return parsed


def event_digest(portfolio_id, idea_id, kind, recorded_at, payload, previous):
    return digest(dict(portfolio_id=portfolio_id, idea_id=idea_id, kind=kind, recorded_at=recorded_at,
                       payload=payload, previous_sha256=previous))


def quote_fill(snapshot, *, side, quantity, at, decision_at, protocol, consumed_volume=0,
               consumed_value=0, consumed_depth=0, calendar=None):
    """فرض اجرای نامساعد با مظنه پس از تصمیم و ظرفیت مشترک؛ جایگاه صف معلوم نیست."""
    if side not in ('buy', 'sell') or not snapshot or snapshot.get('source') != 'tsetmc':
        raise ValueError('مظنه معتبر بازار برای این سمت موجود نیست')
    quantity = integer(quantity)
    if not is_market_open(at, calendar=calendar):
        raise ValueError('شبیه‌سازی با مظنه فقط در ساعت بازار انجام می‌شود')
    for field in ('captured_at', 'provider_timestamp'):
        if not current_snapshot(snapshot.get(field), at) or aware(snapshot[field]) > at:
            raise ValueError('مظنه یا زمان دریافت تازه نیست')
        if aware(snapshot[field]) <= aware(decision_at):
            raise ValueError('برای مظنه پس از ثبت تحلیل منتظر دریافت بعدی بمان')
    if snapshot.get('price_unit') != 'Rial':
        raise ValueError('واحد ریال مظنه تأیید نشده است')
    lower, upper, low, high, bid, ask, last = [finite_number(snapshot.get(k)) for k in
        ('price_limit_down', 'price_limit_up', 'low_price', 'high_price', 'bid_price_1', 'ask_price_1', 'last_price')]
    if not lower <= low <= last <= high <= upper or not lower <= bid <= ask <= upper:
        raise ValueError('دامنه و قیمت‌های مظنه ناسازگارند')
    if any(finite_number(snapshot.get(k), positive=False) > 0 for k in ('buy_queue_volume', 'sell_queue_volume')):
        raise ValueError('صف مشاهده شد؛ اجرای مجازی با مظنه تأیید نمی‌شود')
    book_side = 'ask' if side == 'buy' else 'bid'
    depth = integer(snapshot.get(f'{book_side}_vol_1'))
    integer(snapshot.get(f'{book_side}_count_1'))
    slip = protocol['slippage_bps'] / 10000
    price = ask * (1 + slip) if side == 'buy' else bid * (1 - slip)
    # گرد کردن نامساعد به ریال صحیح، بدون بیرون رفتن از معاملات مشاهده‌شده.
    price = math.ceil(price) if side == 'buy' else math.floor(price)
    if not low <= price <= high:
        raise ValueError('قیمت با لغزش بیرون از بازه معاملات مشاهده‌شده است')
    participation = protocol['participation']
    volume, value = [finite_number(snapshot.get(k)) for k in ('volume', 'value')]
    cap = min(depth - consumed_depth, math.floor(volume * participation) - consumed_volume,
              math.floor((value * participation - consumed_value) / price))
    if quantity > cap:
        raise ValueError(f'تعداد از ظرفیت باقی‌مانده مظنه و مشارکت روز بیشتر است؛ حداکثر {max(0, cap)} سهم')
    return dict(price_rial=price, capacity_before=cap, snapshot=dict(snapshot),
                execution_proven=False, trading_state_verified=False, independent_book_clock=False)


def portfolio_state(protocol, ideas, trades, marks, *, integrity=None, opening=None, passive_values=None):
    """بهای میانگین هر تحلیل مستقل است ولی همه از یک نقد خرج می‌کنند."""
    profile = RiskProfile(protocol['capital_rial'], protocol['max_loss_rial'])
    keyed = [dict(t, symbol_fa=t['idea_id']) for t in trades]
    initial = opening or {}
    lots = initial.get('positions', {})
    ledger = summarize_executions(keyed, profile, marks, sell_fee_rate=protocol['fees']['sell'],
                                 opening_positions=lots if opening else None,
                                 opening_cash_rial=initial.get('available_cash_rial'))
    attribution = []
    for key, idea in ideas.items():
        own = [t for t in trades if t['idea_id'] == key]
        opening_cost = lots.get(key, {}).get('cost_rial', 0)
        bought = opening_cost + sum(t['quantity'] * t['price_rial'] + t['fee_rial'] for t in own if t['side'] == 'buy')
        sold = sum(t['quantity'] * t['price_rial'] - t['fee_rial'] for t in own if t['side'] == 'sell')
        position = ledger['positions'].get(key, {})
        closed_cost = bought - position.get('cost_rial', 0)
        realized = sold - closed_cost
        attribution.append(dict(id=key, symbol_fa=idea['symbol_fa'], label=idea['label'],
                                status='open' if position else 'closed' if own else 'untraded',
                                quantity=position.get('quantity', 0), bought_rial=bought,
                                opening_basis_rial=opening_cost, imported_inventory=key in lots,
                                realized_net_rial=realized, unrealized_net_rial=position.get('unrealized_net_rial'),
                                closed_cost_rial=closed_cost, created_at=idea['created_at']))
    issues = integrity or {}
    blocked = any(issues.values())
    # کنترل اعتبار در خروجی محاسبه است؛ مصرف‌کننده JSON هم نباید سود نامعتبر ببیند.
    if blocked:
        ledger['realized_net_rial'] = None
    for item in attribution:
        if issues.get(item['id']):
            item['realized_net_rial'] = item['unrealized_net_rial'] = None
    for key, position in ledger['positions'].items():
        if issues.get(key):
            position['liquidation_value_rial'] = position['unrealized_net_rial'] = None
    passive = initial.get('passive', [])
    passive_values = passive_values or {}
    passive_unknown = [p['symbol_fa'] for p in passive if passive_values.get(p['symbol_fa']) is None]
    other = initial.get('locked_cash_rial', 0) + initial.get('rounding_rial', 0)
    other += sum(v for v in passive_values.values() if v is not None)
    equity = None if blocked or passive_unknown or ledger['equity_rial'] is None else ledger['equity_rial'] + other
    ledger.update(equity_rial=equity, total_net_rial=None if equity is None else equity - profile.capital_rial,
                  loss_limit_reached=None if equity is None else equity <= profile.equity_floor_rial)
    return dict(ledger=ledger, attribution=attribution, integrity=issues,
                passive_unknown=passive_unknown, passive_values=passive_values,
                locked_cash_rial=initial.get('locked_cash_rial', 0), opening_rounding_rial=initial.get('rounding_rial', 0),
                equity_rial=equity, total_net_rial=None if equity is None else equity - profile.capital_rial,
                return_pct=None if equity is None else (equity / profile.capital_rial - 1) * 100,
                loss_limit_reached=None if equity is None else equity <= profile.equity_floor_rial,
                accepted_for_signals=False, profitability_proven=False)
