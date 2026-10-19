"""دفتر پژوهشیِ تعداد سهم ثابت و طلب سود؛ ورودی صریح، بدون حدس حق یا وصول."""

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date

VERSION = 'cash-dividend-book-v1'
PORTFOLIO_VERSION = 'portfolio-cash-dividends-v1'


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValueError('شناسه یکتا و محدود رخداد/وصول لازم است')


def _evidence(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('هش صریح شاهد لازم است؛ وجود هش، اصالت شاهد را اثبات نمی‌کند')


def _money(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('رقم ریال صریح و متناهی لازم است')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('رقم ریال صریح و متناهی لازم است') from error
    if (not result.is_finite() or not 0 <= result <= 2**63 - 1 or (positive and result <= 0)
            or len(result.as_tuple().digits) > 100 or result.as_tuple().exponent < -100):
        raise ValueError('رقم ریال غیرمنفی در دامنه منبع لازم است')
    return result


def _exact(operation, left, right):
    # کران ورودی: ۱۹ رقم صحیح + حداکثر۱۰۰ اعشار، تعداد سهم حداکثر۱۶رقمی و ده‌هزار رخداد.
    with localcontext() as context:
        context.prec = 160
        return {'add': Decimal.__add__, 'sub': Decimal.__sub__, 'mul': Decimal.__mul__}[operation](left, right)


@dataclass(frozen=True)
class CashDividend:
    event_id: str
    symbol: str
    record_date: str
    ex_date: str
    dps_rial: str
    evidence_sha256: str

    def __post_init__(self):
        _identifier(self.event_id)
        _evidence(self.evidence_sha256)
        if (not isinstance(self.symbol, str) or not self.symbol.strip()
                or not is_canonical_iso_date(self.record_date) or not is_canonical_iso_date(self.ex_date)
                or self.record_date >= self.ex_date):
            raise ValueError('نماد و دو تاریخ صریح به ترتیب جلسه مشمول/بدون حق لازم‌اند')
        object.__setattr__(self, 'symbol', normalize_symbol(self.symbol))
        object.__setattr__(self, 'dps_rial', str(_money(self.dps_rial)))


@dataclass(frozen=True)
class DividendPayment:
    payment_id: str
    event_id: str
    date: str
    per_share_rial: str
    evidence_sha256: str

    def __post_init__(self):
        _identifier(self.payment_id)
        _identifier(self.event_id)
        _evidence(self.evidence_sha256)
        if not is_canonical_iso_date(self.date):
            raise ValueError('تاریخ صریح وصول در مدل لازم است')
        object.__setattr__(self, 'per_share_rial', str(_money(self.per_share_rial, positive=True)))


class CashDividendBook:
    """هش شاهد، تأیید هویت/شرایط استحقاق/وصول برای همه دارندگان نیست.

    پرداخت‌ها ورودی مدل‌اند؛ از برنامه پرداخت یا نسبت مرجع ساخته نمی‌شوند.
    طلب فقط مبلغ اسمی دارد؛ فقدان قیمت مستقل آن با صفر ارزش‌گذاری نمی‌شود.
    """

    def __init__(self, events, payments=()):
        self.events, self.payments = tuple(events), tuple(payments)
        if (len(self.events) > 10000 or len(self.payments) > 10000
                or any(not isinstance(e, CashDividend) for e in self.events)
                or any(not isinstance(p, DividendPayment) for p in self.payments)):
            raise ValueError('فهرست محدود از رخداد/وصول صریح لازم است')
        if len({e.event_id for e in self.events}) != len(self.events):
            raise ValueError('شناسه رخداد تکراری است')
        if len({(e.symbol, e.ex_date) for e in self.events}) != len(self.events):
            raise ValueError('چند اقدام هم‌زمان برای یک نماد در این قرارداد پشتیبانی نمی‌شود')
        if len({p.payment_id for p in self.payments}) != len(self.payments):
            raise ValueError('شناسه وصول تکراری است')
        self.by_id = {e.event_id: e for e in self.events}
        self.by_ex = {(e.symbol, e.ex_date): e for e in self.events}
        self.by_record_date, self.by_ex_date = {}, {}
        for event in self.events:
            self.by_record_date.setdefault(event.record_date, []).append(event)
            self.by_ex_date.setdefault(event.ex_date, []).append(event)
        paid = {}
        for payment in self.payments:
            event = self.by_id.get(payment.event_id)
            if event is None or payment.date < event.ex_date:
                raise ValueError('وصول پیش از رخداد یا برای رخداد مجهول مجاز نیست')
            paid[event.event_id] = _exact('add', paid.get(event.event_id, Decimal(0)), Decimal(payment.per_share_rial))
            if paid[event.event_id] > Decimal(event.dps_rial):
                raise ValueError('جمع وصول هر سهم از سود مصوب بیشتر است')
        self.eligible, self.claims, self.ledger = {}, {}, []
        self.total_paid_rial = Decimal(0)
        self.started = False
        self.last_begin_day, self.last_end_day = None, None
        self.payment_order = sorted(self.payments, key=lambda p: (p.date, p.payment_id))
        self.next_payment = 0

    def start(self, calendar):
        if self.started:
            raise ValueError('برای هر بازپخش، دفتر تازه لازم است')
        self.started = True
        sessions = set(calendar)
        self.sessions = sessions
        if any(e.record_date not in sessions or e.ex_date not in sessions for e in self.events):
            raise ValueError('جلسه مشمول و بدون حق باید در تقویم صریح مدل باشند')
        # تاریخ وصول می‌تواند تعطیل باشد؛ در پایان نخستین جلسه پس از آن قابل استفاده می‌شود.

    def check_action(self, symbol, day, row):
        event = self.by_ex.get((symbol, day))
        if event is None:
            raise ValueError('شکاف اقدام شرکتی برای پوزیشن باز، شاهد نقدی صریح ندارد')
        try:
            previous, reference = _money(row['previous_official'], positive=True), _money(row['ref_price_raw'], positive=True)
        except (KeyError, ValueError) as error:
            raise ValueError('قیمت‌های لازم برای تطبیق حق نقدی معلوم نیستند') from error
        if row.get('previous_date') != event.record_date or _exact('sub', previous, reference) != Decimal(event.dps_rial):
            raise ValueError('شکاف مرجع یا جلسه پیشین با رخداد نقدی صریح برابر نیست')
        return event

    def begin_day(self, day, session):
        if not self.started or day not in self.sessions:
            raise ValueError('روز باید در تقویم دفتر آغازشده باشد')
        if self.last_begin_day is not None and day <= self.last_begin_day:
            raise ValueError('آغاز روز باید یکتا و به ترتیب زمان باشد')
        self.last_begin_day = day
        for event in self.by_ex_date.get(day, ()):
            quantity = self.eligible.get(event.event_id, 0)
            if event.ex_date != day or not quantity:
                continue
            row = session.get(event.symbol)
            if row is None:
                raise ValueError('جلسه بدون حقِ طلب ثبت‌شده در قیمت‌ها غایب است')
            self.check_action(event.symbol, day, row)
            self.claims[event.event_id] = dict(event=event, quantity=quantity, paid_per_share=Decimal(0))
            self.ledger.append(dict(date=day, event_id=event.event_id, symbol=event.symbol,
                kind='receivable_created', quantity=quantity,
                face_rial=str(_exact('mul', Decimal(quantity), Decimal(event.dps_rial))), cash_rial='0'))

    def end_day(self, day, positions):
        if not self.started or day not in self.sessions:
            raise ValueError('روز باید در تقویم دفتر آغازشده باشد')
        if self.last_end_day is not None and day <= self.last_end_day:
            raise ValueError('پایان روز باید یکتا و به ترتیب زمان باشد')
        self.last_end_day = day
        for event in self.by_record_date.get(day, ()):
            value = positions.get(event.symbol, {}).get('quantity', 0)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0 or value != int(value) or value > 2**53:
                raise ValueError('تعداد سهم مشمول باید صحیح، معلوم و در دامنه دقیق باشد')
            self.eligible[event.event_id] = int(value)
        added = Decimal(0)
        while self.next_payment < len(self.payment_order) and self.payment_order[self.next_payment].date <= day:
            payment = self.payment_order[self.next_payment]
            self.next_payment += 1
            claim = self.claims.get(payment.event_id)
            if claim is None:
                continue
            amount = _exact('mul', Decimal(claim['quantity']), Decimal(payment.per_share_rial))
            claim['paid_per_share'] = _exact('add', claim['paid_per_share'], Decimal(payment.per_share_rial))
            added = _exact('add', added, amount)
            self.ledger.append(dict(date=day, stated_payment_date=payment.date, event_id=payment.event_id,
                payment_id=payment.payment_id, symbol=claim['event'].symbol, kind='cash_received',
                quantity=claim['quantity'], cash_rial=str(amount)))
        self.total_paid_rial = _exact('add', self.total_paid_rial, added)
        return float(added)

    @property
    def outstanding_face_rial(self):
        total = Decimal(0)
        for claim in self.claims.values():
            remaining = _exact('sub', Decimal(claim['event'].dps_rial), claim['paid_per_share'])
            total = _exact('add', total, _exact('mul', Decimal(claim['quantity']), remaining))
        return total

    def records(self):
        return [dict(event_id=key, symbol=claim['event'].symbol, entitled_quantity=claim['quantity'],
            dps_rial=claim['event'].dps_rial, paid_per_share_rial=str(claim['paid_per_share']),
            outstanding_face_rial=str(_exact('mul', Decimal(claim['quantity']),
                _exact('sub', Decimal(claim['event'].dps_rial), claim['paid_per_share']))), fair_value_rial=None)
            for key, claim in sorted(self.claims.items())]


def replay_cash_dividend_portfolio(prices, selections, calendar, config, *, events, payments=(), start=None, end=None,
                                   dip_rule=None):
    from backtest.portfolio import replay_portfolio

    prices, selections = prices.copy(), selections.copy()
    prices['symbol'] = prices.symbol.map(normalize_symbol)
    selections['symbol'] = selections.symbol.map(normalize_symbol)
    return replay_portfolio(prices, selections, calendar, config, start=start, end=end, dip_rule=dip_rule,
                            cash_dividends=CashDividendBook(events, payments))
