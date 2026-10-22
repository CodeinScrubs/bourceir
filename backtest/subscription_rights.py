"""Explicit subscription funding, receivable offsets and delayed share/cash settlement."""

import math
from bisect import bisect_right
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from fractions import Fraction

from backtest.bonus_shares import _available, _ratio, rational
from backtest.cash_dividends import CashDividendBook, _evidence, _identifier, _money
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date

VERSION = 'subscription-rights-book-v1'
PORTFOLIO_VERSION = 'portfolio-subscription-rights-v1'
LIMIT = 2**53


def _cash(value, *, positive=False):
    amount = Fraction(_money(value, positive=positive))
    if amount.denominator != 1 or amount >= LIMIT:
        raise ValueError('وجه صریح باید ریال صحیح در دامنه دقیق موتور باشد')
    return amount


def _quantity(value):
    if type(value) is not int or not 0 < value < LIMIT:
        raise ValueError('تعداد سهم صحیح مثبت و در دامنه دقیق لازم است')
    return value


def _day(value):
    if not is_canonical_iso_date(value):
        raise ValueError('روز صریح استاندارد لازم است')
    return value


def _units(numerator, denominator):
    if (type(numerator) is not int or type(denominator) is not int or numerator <= 0
            or not 0 < denominator <= 10**9 or numerator > LIMIT*10**9):
        raise ValueError('تعداد گویا و محدود حق لازم است')
    value = Fraction(numerator, denominator)
    if value >= LIMIT:
        raise ValueError('تعداد حق از دامنه دقیق بزرگ‌تر است')
    return value


@dataclass(frozen=True, kw_only=True)
class _Proof:
    evidence_sha256: str
    evidence_available_at: str

    def __post_init__(self):
        _evidence(self.evidence_sha256)
        object.__setattr__(self, 'evidence_available_at', _available(self.evidence_available_at))


@dataclass(frozen=True, kw_only=True)
class RightsIssue(_Proof):
    event_id: str
    symbol: str
    record_date: str
    ex_date: str
    rights_numerator: int
    rights_denominator: int
    subscription_price_rial: str
    subscription_start: str
    subscription_end: str
    previous_price_session: str
    previous_official_rial: str
    ex_reference_rial: str

    def __post_init__(self):
        super().__post_init__()
        _identifier(self.event_id)
        for name in ('record_date', 'ex_date', 'subscription_start', 'subscription_end', 'previous_price_session'):
            _day(getattr(self, name))
        if (not isinstance(self.symbol, str) or not normalize_symbol(self.symbol)
                or not self.previous_price_session <= self.record_date < self.ex_date
                or not self.record_date < self.subscription_start <= self.subscription_end):
            raise ValueError('هویت و ترتیب روزهای مشمول، بدون حق و مهلت لازم‌اند')
        object.__setattr__(self, 'symbol', normalize_symbol(self.symbol))
        _ratio(self.rights_numerator, self.rights_denominator)
        for name in ('subscription_price_rial', 'previous_official_rial', 'ex_reference_rial'):
            object.__setattr__(self, name, str(_cash(getattr(self, name), positive=True).numerator))
        expected = (Fraction(self.previous_official_rial)+self.ratio*Fraction(self.subscription_price_rial))/(1+self.ratio)
        if not math.floor(expected) <= Fraction(self.ex_reference_rial) <= math.ceil(expected):
            raise ValueError('مرجع با سناریوی صریح مطالبات/آورده در پوش یک‌ریالی سازگار نیست')

    @property
    def ratio(self):
        return Fraction(self.rights_numerator, self.rights_denominator)


@dataclass(frozen=True, kw_only=True)
class RightsOffset:
    dividend_event_id: str
    amount_rial: str

    def __post_init__(self):
        _identifier(self.dividend_event_id)
        object.__setattr__(self, 'amount_rial', str(_cash(self.amount_rial, positive=True).numerator))


@dataclass(frozen=True, kw_only=True)
class RightsExercise(_Proof):
    exercise_id: str
    event_id: str
    decision_date: str
    date: str
    quantity: int
    cash_paid_rial: str
    fee_rial: str
    offsets: tuple = ()

    def __post_init__(self):
        super().__post_init__()
        _identifier(self.exercise_id)
        _identifier(self.event_id)
        _quantity(self.quantity)
        if _day(self.decision_date) > _day(self.date):
            raise ValueError('تصمیم اعمال نباید پس از پرداخت باشد')
        for name in ('cash_paid_rial', 'fee_rial'):
            object.__setattr__(self, name, str(_cash(getattr(self, name)).numerator))
        if not isinstance(self.offsets, (list, tuple)) or len(self.offsets) > 100:
            raise ValueError('فهرست محدود و صریح تهاترها لازم است')
        offsets = tuple(self.offsets)
        if (any(not isinstance(v, RightsOffset) for v in offsets)
                or len({v.dividend_event_id for v in offsets}) != len(offsets)):
            raise ValueError('فهرست یکتای تهاترهای صریح لازم است')
        object.__setattr__(self, 'offsets', offsets)


@dataclass(frozen=True, kw_only=True)
class RightsCredit(_Proof):
    credit_id: str
    exercise_id: str
    date: str
    cumulative_quantity: int

    def __post_init__(self):
        super().__post_init__()
        _identifier(self.credit_id)
        _identifier(self.exercise_id)
        _day(self.date)
        _quantity(self.cumulative_quantity)


@dataclass(frozen=True, kw_only=True)
class RightsDisposal(_Proof):
    disposal_id: str
    event_id: str
    date: str
    units_numerator: int
    units_denominator: int
    kind: str
    net_expected_rial: str | None

    def __post_init__(self):
        super().__post_init__()
        _identifier(self.disposal_id)
        _identifier(self.event_id)
        _day(self.date)
        _units(self.units_numerator, self.units_denominator)
        if self.kind not in ('holder_sale_receipt', 'unused_rights_distribution'):
            raise ValueError('نوع واگذاری باید صریح باشد')
        if self.net_expected_rial is not None:
            object.__setattr__(self, 'net_expected_rial', str(_cash(self.net_expected_rial).numerator))

    @property
    def units(self):
        return Fraction(self.units_numerator, self.units_denominator)


@dataclass(frozen=True, kw_only=True)
class RightsPayment(_Proof):
    payment_id: str
    disposal_id: str
    date: str
    net_cash_rial: str
    final: bool

    def __post_init__(self):
        super().__post_init__()
        _identifier(self.payment_id)
        _identifier(self.disposal_id)
        _day(self.date)
        object.__setattr__(self, 'net_cash_rial', str(_cash(self.net_cash_rial).numerator))
        if type(self.final) is not bool:
            raise ValueError('وصول نهایی باید صریحاً بولی باشد')


@dataclass(frozen=True, kw_only=True)
class DividendCashReceipt(_Proof):
    receipt_id: str
    dividend_event_id: str
    date: str
    amount_rial: str

    def __post_init__(self):
        super().__post_init__()
        _identifier(self.receipt_id)
        _identifier(self.dividend_event_id)
        _day(self.date)
        object.__setattr__(self, 'amount_rial', str(_cash(self.amount_rial, positive=True).numerator))


class SubscriptionRightsBook:
    """Declared accounting inputs, never a claim that holder allocation or execution is proven."""

    def __init__(self, events, exercises=(), credits=(), disposals=(), payments=(), *, dividends=None, dividend_receipts=()):
        groups = ((events, RightsIssue), (exercises, RightsExercise), (credits, RightsCredit),
                  (disposals, RightsDisposal), (payments, RightsPayment))
        for values, kind in groups:
            if not isinstance(values, (list, tuple)) or len(values) > 10000 or any(not isinstance(v, kind) for v in values):
                raise ValueError('فهرست محدود از رکوردهای صریح همان نوع لازم است')
        self.events, self.exercises, self.credits, self.disposals, self.payments = [tuple(v) for v, _ in groups]
        for values, key in ((self.events, 'event_id'), (self.exercises, 'exercise_id'),
                            (self.credits, 'credit_id'), (self.disposals, 'disposal_id'), (self.payments, 'payment_id')):
            if len({getattr(v, key) for v in values}) != len(values):
                raise ValueError('شناسه رکورد تکراری است')
        self.by_id = {e.event_id: e for e in self.events}
        self.by_ex = {(e.symbol, e.ex_date): e for e in self.events}
        if len(self.by_ex) != len(self.events):
            raise ValueError('چند اقدام هم‌زمان برای یک نماد تعریف نشده است')
        self.exercise_by_id = {e.exercise_id: e for e in self.exercises}
        self.disposal_by_id = {d.disposal_id: d for d in self.disposals}
        if dividends is not None and (not isinstance(dividends, CashDividendBook) or dividends.started):
            raise ValueError('دفتر سود نقدیِ آغازنشده لازم است')
        self.dividends = deepcopy(dividends)
        if (not isinstance(dividend_receipts, (list, tuple)) or len(dividend_receipts) > 10000
                or any(not isinstance(r, DividendCashReceipt) for r in dividend_receipts)
                or len({r.receipt_id for r in dividend_receipts}) != len(dividend_receipts)):
            raise ValueError('فهرست یکتای وصول مبلغ کل سود نقدی لازم است')
        self.dividend_receipts = tuple(dividend_receipts)
        for receipt in self.dividend_receipts:
            event = None if self.dividends is None else self.dividends.by_id.get(receipt.dividend_event_id)
            if event is None or receipt.date < event.ex_date:
                raise ValueError('وصول سود به طلب معلوم و روز پس از ایجاد آن نیاز دارد')
        self.dividend_receipt_order = sorted(self.dividend_receipts, key=lambda r: (r.date, r.receipt_id))
        if self.dividends is not None and set(self.by_ex) & set(self.dividends.by_ex):
            raise ValueError('سود نقدی و حق‌تقدم با جلسه بدون حق مشترک هنوز قرارداد مستقل می‌خواهند')
        for exercise in self.exercises:
            event = self.by_id.get(exercise.event_id)
            if (event is None or not event.record_date < exercise.decision_date <= exercise.date
                    or not event.subscription_start <= exercise.date <= event.subscription_end):
                raise ValueError('اعمال باید برای رخداد معلوم و در مهلت صریح باشد')
            principal = Fraction(event.subscription_price_rial)*exercise.quantity
            offset = sum((Fraction(v.amount_rial) for v in exercise.offsets), Fraction(0))
            if (principal >= LIMIT or offset > principal
                    or Fraction(exercise.cash_paid_rial)+offset != principal+Fraction(exercise.fee_rial)):
                raise ValueError('اصل، هزینه، نقد و تهاتر دقیقاً برابر نیستند')
            if exercise.offsets and self.dividends is None:
                raise ValueError('تهاتر بدون دفتر مستقل مطالبات سود نقدی مجاز نیست')
        self.credit_order = sorted(self.credits, key=lambda c: (c.date, c.credit_id))
        previous, dates = {}, set()
        for credit in self.credit_order:
            exercise = self.exercise_by_id.get(credit.exercise_id)
            key = (credit.exercise_id, credit.date)
            if (exercise is None or credit.date < exercise.date or key in dates
                    or not previous.get(credit.exercise_id, 0) < credit.cumulative_quantity <= exercise.quantity):
                raise ValueError('اعتبار باید پس از پرداخت، یکتا و تجمعیِ محدود باشد')
            dates.add(key)
            previous[credit.exercise_id] = credit.cumulative_quantity
        for disposal in self.disposals:
            event = self.by_id.get(disposal.event_id)
            if event is None or disposal.date <= event.record_date:
                raise ValueError('واگذاری به رخداد معلوم و روز پس از مشمول نیاز دارد')
        self.payment_order = sorted(self.payments, key=lambda p: (p.date, p.payment_id))
        paid, closed, dates = {}, set(), set()
        for payment in self.payment_order:
            disposal = self.disposal_by_id.get(payment.disposal_id)
            key = (payment.disposal_id, payment.date)
            if (disposal is None or payment.date < disposal.date or key in dates or payment.disposal_id in closed):
                raise ValueError('وصول باید برای واگذاری معلوم، یکتا در روز و پیش از بسته‌شدن باشد')
            dates.add(key)
            total = paid.get(payment.disposal_id, Fraction(0))+Fraction(payment.net_cash_rial)
            if total >= LIMIT:
                raise ValueError('جمع وصول از دامنه دقیق بزرگ‌تر است')
            expected = None if disposal.net_expected_rial is None else Fraction(disposal.net_expected_rial)
            if expected is not None and (total > expected or (payment.final and total != expected)):
                raise ValueError('وصول با خالص اسمی اظهارشده برابر نیست')
            paid[payment.disposal_id] = total
            if payment.final:
                closed.add(payment.disposal_id)
        self.exercise_order = sorted(self.exercises, key=lambda e: (e.date, e.exercise_id))
        self.disposal_order = sorted(self.disposals, key=lambda d: (d.date, d.disposal_id))
        self.eligible, self.claims, self.funded, self.disposed = {}, {}, {}, {}
        self.ledger, self.last_marks, self.dividend_offsets, self.dividend_cash_by_event = [], {}, {}, {}
        self.next_exercise = self.next_credit = self.next_disposal = self.next_payment = 0
        self.next_dividend_receipt = 0
        self.total_cash_paid = self.total_fees = self.total_offset_income = Fraction(0)
        self.total_disposal_cash = self.total_dividend_receipt_cash = Fraction(0)
        self.started, self.last_begin, self.last_end, self.step = False, None, None, 0

    @contextmanager
    def _atomic(self):
        names = ('eligible', 'claims', 'funded', 'disposed', 'last_marks', 'dividend_offsets', 'dividend_cash_by_event',
                 'next_exercise', 'next_credit', 'next_disposal', 'next_payment',
                 'total_cash_paid', 'total_fees', 'total_offset_income', 'total_disposal_cash',
                 'next_dividend_receipt', 'total_dividend_receipt_cash', 'last_begin', 'last_end', 'step')
        saved = {name: deepcopy(getattr(self, name)) for name in names}
        count = len(self.ledger)
        dividend_state = deepcopy(self.dividends.__dict__) if self.dividends is not None else None
        try:
            yield
        except Exception:
            for name, value in saved.items():
                setattr(self, name, value)
            del self.ledger[count:]
            if dividend_state is not None:
                self.dividends.__dict__.clear()
                self.dividends.__dict__.update(dividend_state)
            raise

    def start(self, calendar):
        if self.started:
            raise ValueError('هر بازپخش به دفتر تازه نیاز دارد')
        days = list(calendar)
        if not days or days != sorted(set(days)) or any(not is_canonical_iso_date(d) for d in days):
            raise ValueError('تقویم صریح یکتا و مرتب لازم است')
        sessions = {}
        for event in self.events:
            index = bisect_right(days, event.record_date)-1
            if index < 0 or (event.ex_date <= days[-1] and event.ex_date not in days):
                raise ValueError('حق پیش از آغاز یا جلسه بدون حقِ غایب، موجودی/تقویم مستقل می‌خواهد')
            if event.record_date <= days[-1]:
                sessions.setdefault(days[index], []).append(event)
        if self.dividends is not None:
            if any(e.record_date not in days or e.ex_date not in days for e in self.dividends.events):
                raise ValueError('تقویم سود نقدی کامل و صریح لازم است')
            self.dividends.start(days)
        self.days, self.record_sessions, self.started = days, sessions, True

    def check_action(self, symbol, day, row):
        event = self.by_ex.get((symbol, day))
        if event is None:
            if self.dividends is not None:
                return self.dividends.check_action(symbol, day, row)
            raise ValueError('شکاف مرجع بدون شاهد حق صریح است')
        if (row.get('previous_date') != event.previous_price_session
                or _money(row.get('previous_official'), positive=True) != _money(event.previous_official_rial)
                or _money(row.get('ref_price_raw'), positive=True) != _money(event.ex_reference_rial)):
            raise ValueError('جلسه و دو قیمت مرجع با رخداد اظهارشده برابر نیستند')
        return event

    @property
    def pending_units(self):
        rights = sum((c['remaining'] for c in self.claims.values()), Fraction(0))
        shares = sum((Fraction(c['exercise'].quantity-c['credited']) for c in self.funded.values()), Fraction(0))
        return rights+shares

    @property
    def pending_symbols(self):
        return ({c['event'].symbol for c in self.claims.values() if c['remaining'] > 0}
                | {c['event'].symbol for c in self.funded.values() if c['exercise'].quantity > c['credited']})

    @property
    def pending_whole_symbols(self):
        totals = {}
        for claim in self.claims.values():
            symbol = claim['event'].symbol
            totals[symbol] = totals.get(symbol, Fraction(0))+claim['remaining']
        for claim in self.funded.values():
            symbol = claim['event'].symbol
            totals[symbol] = totals.get(symbol, Fraction(0))+claim['exercise'].quantity-claim['credited']
        return {symbol for symbol, units in totals.items() if units >= 1}

    @property
    def pending_basis(self):
        return sum((c['basis']-c['allocated_basis'] for c in self.funded.values()), Fraction(0))

    @property
    def outstanding_dividend_face(self):
        before = Fraction(0) if self.dividends is None else Fraction(self.dividends.outstanding_face_rial)
        return before-sum(self.dividend_offsets.values(), Fraction(0))-sum(self.dividend_cash_by_event.values(), Fraction(0))

    @property
    def total_dividend_cash(self):
        before = Fraction(0) if self.dividends is None else Fraction(self.dividends.total_paid_rial)
        return before+self.total_dividend_receipt_cash

    @property
    def has_unvalued_claims(self):
        return (self.pending_units > 0 or self.outstanding_dividend_face > 0
                or any(not c['closed'] for c in self.disposed.values()))

    def _offset(self, offset, event, day):
        claim = self.dividends.claims.get(offset.dividend_event_id) if self.dividends is not None else None
        if claim is None or claim['event'].symbol != event.symbol or claim['event'].ex_date >= day:
            raise ValueError('تهاتر فقط از طلب موجودِ جلسه پیشین همان ناشر مجاز است')
        face = Fraction(claim['quantity'])*(Fraction(claim['event'].dps_rial)-Fraction(claim['paid_per_share']))
        used = self.dividend_offsets.get(offset.dividend_event_id, Fraction(0))
        received = self.dividend_cash_by_event.get(offset.dividend_event_id, Fraction(0))
        amount = Fraction(offset.amount_rial)
        if amount > face-used-received:
            raise ValueError('مطالبه کافی برای تهاتر موجود نیست')
        self.dividend_offsets[offset.dividend_event_id] = used+amount
        self.total_offset_income += amount
        self.ledger.append(dict(date=day, kind='dividend_offset_applied', event_id=event.event_id,
            dividend_event_id=offset.dividend_event_id, amount_rial=str(amount.numerator), cash_rial='0'))
        return amount

    @staticmethod
    def _engine_amount(value):
        if abs(value) >= LIMIT:
            raise ValueError('وجه مرحله از دامنه دقیق موتور بزرگ‌تر است')
        return float(value)

    def begin_day(self, day, session, positions, available_cash):
        with self._atomic():
            if (not self.started or self.step >= len(self.days) or day != self.days[self.step]
                    or self.last_begin != self.last_end):
                raise ValueError('آغاز روز باید جلسه بعدیِ کامل و مرتب دفتر باشد')
            self.last_begin = day
            if self.dividends is not None:
                self.dividends.begin_day(day, session)
            for symbol, row in session.items():
                if row.get('valid'):
                    mark = float(_money(row.get('mark'), positive=True))
                    self.last_marks[symbol] = (mark, day)
            for event in self.events:
                if event.event_id in self.claims or event.record_date >= day:
                    continue
                lots = self.eligible.get(event.event_id)
                if lots is None:
                    continue
                units = sum((lot['quantity']*event.ratio for lot in lots), Fraction(0))
                if units >= LIMIT:
                    raise ValueError('حق مشمول از دامنه دقیق بزرگ‌تر است')
                self.claims[event.event_id] = dict(event=event, units=units, remaining=units, origin_lots=lots)
                if units:
                    self.ledger.append(dict(date=day, event_id=event.event_id, symbol=event.symbol,
                        kind='subscription_rights_recognized', units=rational(units), cash_rial='0',
                        model_cost_basis_rial='0', fair_value_rial=None))
            exposed = {p['symbol'] for p in positions.values()} | self.pending_symbols
            for event in self.events:
                if event.ex_date == day and event.symbol in exposed:
                    self.check_action(event.symbol, day, session.get(event.symbol, {}))
            cash_used, offset_income = Fraction(0), Fraction(0)
            cash = Fraction(_money(available_cash))
            while self.next_exercise < len(self.exercise_order) and self.exercise_order[self.next_exercise].date <= day:
                exercise = self.exercise_order[self.next_exercise]
                event = self.by_id[exercise.event_id]
                claim = self.claims.get(event.event_id)
                if claim is None or exercise.quantity > claim['remaining']:
                    raise ValueError('حق کافی برای اعمالِ اظهارشده موجود نیست')
                cash_needed = Fraction(exercise.cash_paid_rial)
                if cash_used+cash_needed > cash:
                    raise ValueError('نقد مشترک برای پرداختِ اظهارشده کافی نیست')
                for offset in exercise.offsets:
                    offset_income += self._offset(offset, event, day)
                basis = Fraction(exercise.quantity)*Fraction(event.subscription_price_rial)+Fraction(exercise.fee_rial)
                claim['remaining'] -= exercise.quantity
                self.funded[exercise.exercise_id] = dict(exercise=exercise, event=event, basis=basis,
                    credited=0, allocated_basis=Fraction(0))
                cash_used += cash_needed
                self.total_cash_paid += cash_needed
                self.total_fees += Fraction(exercise.fee_rial)
                self.next_exercise += 1
                self.ledger.append(dict(date=day, stated_payment_date=exercise.date, exercise_id=exercise.exercise_id,
                    event_id=event.event_id, kind='subscription_funded', quantity=exercise.quantity,
                    cash_paid_rial=exercise.cash_paid_rial, fee_rial=exercise.fee_rial,
                    basis_rial=rational(basis), shares_credited=False))
            return -self._engine_amount(cash_used), self._engine_amount(offset_income)

    def _capture_eligible(self, day, positions):
        for event in self.record_sessions.get(day, ()):
            if event.symbol in self.pending_symbols:
                raise ValueError('روز مشمول جدید با حق قبلی دریافت‌نشده هم‌پوشانی دارد')
            lots = []
            for key, position in positions.items():
                if position['symbol'] != event.symbol:
                    continue
                value = _money(position['quantity'], positive=True)
                if value != value.to_integral_value():
                    raise ValueError('موجودی مشمول باید سهم صحیح باشد')
                quantity = _quantity(int(value))
                lots.append(dict(lot_id=key, quantity=quantity, original_basis_rial=str(_money(position['cost']))))
            if sum(lot['quantity'] for lot in lots) >= LIMIT:
                raise ValueError('جمع تعداد مشمول از دامنه دقیق بزرگ‌تر است')
            self.eligible[event.event_id] = lots

    def _dividend_end(self, day, positions):
        if self.dividends is None:
            return Fraction(0)
        for event in self.dividends.by_record_date.get(day, ()):
            if event.symbol in self.pending_symbols:
                raise ValueError('سود نقدی روی سهم/حق دریافت‌نشده به شرط استحقاق مستقل نیاز دارد')
        projected = {}
        index = self.dividends.next_payment
        while index < len(self.dividends.payment_order) and self.dividends.payment_order[index].date <= day:
            payment = self.dividends.payment_order[index]
            index += 1
            claim = self.dividends.claims.get(payment.event_id)
            if claim is None:
                continue
            amount = Fraction(claim['quantity'])*Fraction(payment.per_share_rial)
            projected[payment.event_id] = projected.get(payment.event_id, Fraction(0))+amount
        index = self.next_dividend_receipt
        while index < len(self.dividend_receipt_order) and self.dividend_receipt_order[index].date <= day:
            receipt = self.dividend_receipt_order[index]
            index += 1
            if receipt.dividend_event_id not in self.dividends.claims:
                raise ValueError('مبلغ کل وصول سود بدون طلب مشمول مجاز نیست')
            projected[receipt.dividend_event_id] = projected.get(receipt.dividend_event_id, Fraction(0))+Fraction(receipt.amount_rial)
        for event_id, amount in projected.items():
            claim = self.dividends.claims[event_id]
            face = Fraction(claim['quantity'])*(Fraction(claim['event'].dps_rial)-Fraction(claim['paid_per_share']))
            if amount+self.dividend_offsets.get(event_id, Fraction(0))+self.dividend_cash_by_event.get(event_id, Fraction(0)) > face:
                raise ValueError('جمع وصول و تهاتر از طلب مصوب بیشتر است')
        aggregate = {}
        for position in positions.values():
            symbol = position['symbol']
            value = _money(position['quantity'], positive=True)
            if value != value.to_integral_value():
                raise ValueError('تعداد مشمول سود نقدی باید صحیح باشد')
            aggregate[symbol] = aggregate.get(symbol, 0)+_quantity(int(value))
        before = Fraction(self.dividends.total_paid_rial)
        self.dividends.end_day(day, {s: dict(quantity=q) for s, q in aggregate.items()})
        cash = Fraction(self.dividends.total_paid_rial)-before
        while self.next_dividend_receipt < len(self.dividend_receipt_order) and self.dividend_receipt_order[self.next_dividend_receipt].date <= day:
            receipt = self.dividend_receipt_order[self.next_dividend_receipt]
            amount = Fraction(receipt.amount_rial)
            key = receipt.dividend_event_id
            self.dividend_cash_by_event[key] = self.dividend_cash_by_event.get(key, Fraction(0))+amount
            self.total_dividend_receipt_cash += amount
            cash += amount
            self.ledger.append(dict(date=day, stated_receipt_date=receipt.date, receipt_id=receipt.receipt_id,
                dividend_event_id=key, kind='holder_dividend_cash_received', cash_rial=receipt.amount_rial))
            self.next_dividend_receipt += 1
        return cash

    def end_day(self, day, positions):
        with self._atomic():
            if self.last_begin != day or self.last_end == day:
                raise ValueError('پایان روز باید پس از آغاز همان جلسه و یکتا باشد')
            self._capture_eligible(day, positions)
            dividend_cash = self._dividend_end(day, positions)
            credits = {}
            while self.next_credit < len(self.credit_order) and self.credit_order[self.next_credit].date <= day:
                credit = self.credit_order[self.next_credit]
                claim = self.funded.get(credit.exercise_id)
                if claim is None:
                    raise ValueError('اعتبار سهم بدون پرداخت مدل‌شده مجاز نیست')
                count = credit.cumulative_quantity-claim['credited']
                basis = claim['basis']*count/claim['exercise'].quantity
                claim['credited'] += count
                claim['allocated_basis'] += basis
                event = claim['event']
                mark, stamp = self.last_marks.get(event.symbol, (float('nan'), None))
                if stamp is None or stamp < event.ex_date:
                    mark, stamp = float('nan'), None
                credits['RIGHTS-'+credit.credit_id] = dict(symbol=event.symbol, quantity=float(count), cost=float(basis),
                    mark=mark, mark_date=stamp, credit_only=True, source_event=event.event_id,
                    source_exercise=credit.exercise_id, source_credit=credit.credit_id, valuation_not_before=event.ex_date)
                self.ledger.append(dict(date=day, stated_credit_date=credit.date, event_id=event.event_id,
                    exercise_id=credit.exercise_id, credit_id=credit.credit_id, kind='funded_shares_credited',
                    quantity=count, basis_rial=rational(basis), cash_rial='0'))
                self.next_credit += 1
            while self.next_disposal < len(self.disposal_order) and self.disposal_order[self.next_disposal].date <= day:
                disposal = self.disposal_order[self.next_disposal]
                claim = self.claims.get(disposal.event_id)
                if claim is None or disposal.units > claim['remaining']:
                    raise ValueError('حق کافی برای واگذاریِ اظهارشده موجود نیست')
                claim['remaining'] -= disposal.units
                self.disposed[disposal.disposal_id] = dict(disposal=disposal, received=Fraction(0), closed=False)
                self.ledger.append(dict(date=day, stated_disposal_date=disposal.date, event_id=disposal.event_id,
                    disposal_id=disposal.disposal_id, kind=disposal.kind, units=rational(disposal.units),
                    net_expected_rial=disposal.net_expected_rial, cash_rial='0', execution_verified=False))
                self.next_disposal += 1
            receipt_cash = Fraction(0)
            while self.next_payment < len(self.payment_order) and self.payment_order[self.next_payment].date <= day:
                payment = self.payment_order[self.next_payment]
                claim = self.disposed.get(payment.disposal_id)
                if claim is None or claim['closed']:
                    raise ValueError('وصول فقط برای طلب واگذاریِ باز مجاز است')
                amount = Fraction(payment.net_cash_rial)
                claim['received'] += amount
                claim['closed'] = payment.final
                receipt_cash += amount
                self.ledger.append(dict(date=day, stated_payment_date=payment.date, disposal_id=payment.disposal_id,
                    payment_id=payment.payment_id, kind='rights_cash_received', cash_rial=payment.net_cash_rial,
                    final=payment.final, price_imputed=False))
                self.next_payment += 1
            self.total_disposal_cash += receipt_cash
            self.last_end = day
            self.step += 1
            cash = self._engine_amount(dividend_cash+receipt_cash)
            # Rights have zero allocated model basis; original share basis is retained.
            return credits, cash, cash

    def records(self):
        return dict(rights=[dict(event=asdict(c['event']), original_units=rational(c['units']),
                remaining_units=rational(c['remaining']), original_lots=deepcopy(c['origin_lots']), fair_value_rial=None)
            for c in self.claims.values()],
            funded=[dict(exercise=asdict(c['exercise']), credited_shares=c['credited'],
                pending_shares=c['exercise'].quantity-c['credited'], basis_rial=rational(c['basis']),
                pending_basis_rial=rational(c['basis']-c['allocated_basis']), fair_value_rial=None)
                for c in self.funded.values()],
            disposals=[dict(disposal=asdict(c['disposal']), received_rial=rational(c['received']),
                closed=c['closed'], fair_value_rial=None) for c in self.disposed.values()],
            dividends=[] if self.dividends is None else [dict(row,
                offset_applied_rial=rational(self.dividend_offsets.get(row['event_id'], Fraction(0))),
                holder_cash_receipts_rial=rational(self.dividend_cash_by_event.get(row['event_id'], Fraction(0))),
                scheduled_cash_only_outstanding_face_rial=row['outstanding_face_rial'],
                outstanding_face_rial=rational(Fraction(row['outstanding_face_rial'])
                    -self.dividend_offsets.get(row['event_id'], Fraction(0))
                    -self.dividend_cash_by_event.get(row['event_id'], Fraction(0)))) for row in self.dividends.records()])


def replay_subscription_portfolio(prices, selections, calendar, config, *, events, exercises=(), credits=(),
                                  disposals=(), payments=(), dividends=None, dividend_receipts=(), **kwargs):
    from backtest.portfolio import replay_portfolio

    prices, selections = prices.copy(), selections.copy()
    prices['symbol'] = prices.symbol.map(normalize_symbol)
    selections['symbol'] = selections.symbol.map(normalize_symbol)
    return replay_portfolio(prices, selections, calendar, config, subscription_rights=SubscriptionRightsBook(
        events, exercises, credits, disposals, payments, dividends=dividends, dividend_receipts=dividend_receipts), **kwargs)
