"""دفتر سناریوی سهام جایزه؛ سهم قابل فروش، طلب سهم و کسور مستقل‌اند."""

import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction

from backtest.cash_dividends import _evidence, _identifier, _money
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'bonus-share-book-v1'
PORTFOLIO_VERSION = 'portfolio-bonus-shares-v1'


def rational(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def _ratio(numerator, denominator, *, at_most_one=False):
    if (type(numerator) is not int or type(denominator) is not int
            or not 0 < numerator <= 10**12 or not 0 < denominator <= 10**9):
        raise ValueError('صورت و مخرج صحیح مثبت و محدود لازم‌اند')
    value = Fraction(numerator, denominator)
    if at_most_one and value > 1:
        raise ValueError('سهم تجمعی آزادسازی حداکثر یک است')
    return value


def _available(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError('زمان دسترسی شاهد باید صریح باشد') from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('زمان دسترسی شاهد باید منطقه زمانی داشته باشد')
    return stamp.astimezone(TEHRAN_TZ).isoformat()


@dataclass(frozen=True)
class BonusCapitalization:
    event_id: str
    symbol: str
    record_date: str
    ex_date: str
    bonus_numerator: int
    bonus_denominator: int
    previous_price_session: str
    previous_official_rial: str
    ex_reference_rial: str
    evidence_sha256: str
    evidence_available_at: str

    def __post_init__(self):
        _identifier(self.event_id)
        _evidence(self.evidence_sha256)
        if (not isinstance(self.symbol, str) or not normalize_symbol(self.symbol)
                or any(not is_canonical_iso_date(d) for d in (self.record_date, self.ex_date, self.previous_price_session))
                or not self.previous_price_session <= self.record_date < self.ex_date):
            raise ValueError('نماد و ترتیب آخرین قیمت، روز مشمول و روز بدون حق لازم‌اند')
        object.__setattr__(self, 'evidence_available_at', _available(self.evidence_available_at))
        _ratio(self.bonus_numerator, self.bonus_denominator)
        object.__setattr__(self, 'symbol', normalize_symbol(self.symbol))
        for field in ('previous_official_rial', 'ex_reference_rial'):
            object.__setattr__(self, field, str(_money(getattr(self, field), positive=True)))
        before, after = (_money(getattr(self, field)) for field in ('previous_official_rial','ex_reference_rial'))
        expected = Fraction(before)/(1+self.ratio)
        if (before != before.to_integral_value() or after != after.to_integral_value()
                or not math.floor(expected) <= after <= math.ceil(expected)):
            raise ValueError('سناریوی جایزه خالص با پوش یک‌ریالی مرجع سازگار نیست؛ اقدام ترکیبی/قاعده دیگر نامعلوم است')

    @property
    def ratio(self):
        return Fraction(self.bonus_numerator, self.bonus_denominator)


@dataclass(frozen=True)
class BonusRelease:
    release_id: str
    event_id: str
    date: str
    cumulative_numerator: int
    cumulative_denominator: int
    evidence_sha256: str
    evidence_available_at: str

    def __post_init__(self):
        _identifier(self.release_id)
        _identifier(self.event_id)
        _evidence(self.evidence_sha256)
        if not is_canonical_iso_date(self.date):
            raise ValueError('روز آزادسازی در مدل باید صریح باشد')
        object.__setattr__(self, 'evidence_available_at', _available(self.evidence_available_at))
        _ratio(self.cumulative_numerator, self.cumulative_denominator, at_most_one=True)

    @property
    def ratio(self):
        return Fraction(self.cumulative_numerator, self.cumulative_denominator)


@dataclass(frozen=True)
class BonusFractionSettlement:
    settlement_id: str
    event_id: str
    date: str
    net_cash_rial: str
    evidence_sha256: str
    evidence_available_at: str

    def __post_init__(self):
        _identifier(self.settlement_id)
        _identifier(self.event_id)
        _evidence(self.evidence_sha256)
        if not is_canonical_iso_date(self.date):
            raise ValueError('تاریخ وصول کسور در مدل لازم است')
        object.__setattr__(self, 'evidence_available_at', _available(self.evidence_available_at))
        amount = _money(self.net_cash_rial)
        if amount != amount.to_integral_value() or amount >= 2**53:
            raise ValueError('خالص وجه کسور برای همین دفتر باید ریال صحیح صریح باشد')
        object.__setattr__(self, 'net_cash_rial', str(amount))


class BonusShareBook:
    """نسبت ورودی اظهارشده است؛ نسبت قیمت یا سرمایه، حق واقعی دارنده را اثبات نمی‌کند.

    آزادسازی تجمعی صریح، سهام صحیح را از استخر همان رخداد خارج می‌کند؛ کسور بین لات‌ها
    جمع می‌شوند. قضاوت استحقاق، تاریخ اعتبار حساب و ارزش کسور خودکار ساخته نمی‌شوند.
    """

    def __init__(self, events, releases=(), settlements=()):
        self.events, self.releases, self.settlements = map(tuple, (events, releases, settlements))
        for values, expected in ((self.events, BonusCapitalization), (self.releases, BonusRelease),
                                  (self.settlements, BonusFractionSettlement)):
            if len(values) > 10000 or any(not isinstance(value, expected) for value in values):
                raise ValueError('فهرست محدود از ورودی‌های صریح همان نوع لازم است')
        if (len({e.event_id for e in self.events}) != len(self.events)
                or len({(e.symbol, e.ex_date) for e in self.events}) != len(self.events)
                or len({r.release_id for r in self.releases}) != len(self.releases)
                or len({(r.event_id, r.date) for r in self.releases}) != len(self.releases)
                or len({s.settlement_id for s in self.settlements}) != len(self.settlements)
                or len({s.event_id for s in self.settlements}) != len(self.settlements)):
            raise ValueError('رخداد، آزادسازی یا وصول کسور تکراری است')
        self.by_id = {e.event_id: e for e in self.events}
        self.by_ex = {(e.symbol, e.ex_date): e for e in self.events}
        self.release_order = sorted(self.releases, key=lambda r: (r.date, r.release_id))
        prior = {}
        for release in self.release_order:
            event = self.by_id.get(release.event_id)
            if event is None or release.date < event.ex_date or release.ratio <= prior.get(event.event_id, 0):
                raise ValueError('آزادسازی باید پس از رخداد و اکیداً تجمعی باشد')
            prior[event.event_id] = release.ratio
        self.settlement_order = sorted(self.settlements, key=lambda s: (s.date, s.settlement_id))
        for settlement in self.settlement_order:
            event = self.by_id.get(settlement.event_id)
            full = [r.date for r in self.releases if r.event_id == settlement.event_id and r.ratio == 1]
            if event is None or not full or settlement.date < min(full):
                raise ValueError('وصول کسور به آزادسازی کامل صریح و رخداد معلوم نیاز دارد')
        self.eligible, self.claims, self.ledger, self.last_marks = {}, {}, [], {}
        self.started, self.last_begin, self.last_end = False, None, None
        self.next_release, self.next_settlement = 0, 0
        self.total_fraction_cash = Fraction(0)
        self.total_fraction_realized = Fraction(0)

    def start(self, calendar):
        if self.started:
            raise ValueError('برای هر بازپخش دفتر تازه لازم است')
        self.days = list(calendar)
        if (not self.days or self.days != sorted(set(self.days))
                or any(not is_canonical_iso_date(d) for d in self.days)):
            raise ValueError('تقویم صریح، یکتا و مرتب لازم است')
        self.record_sessions = {}
        for event in self.events:
            index = bisect_right(self.days, event.record_date)-1
            if index < 0:
                raise ValueError('حقِ پیش از آغاز بازپخش به موجودی آغازین مستقل نیاز دارد')
            if event.ex_date <= self.days[-1] and event.ex_date not in self.days:
                raise ValueError('جلسه بدون حق در تقویم فعال غایب است')
            if event.record_date <= self.days[-1]:
                self.record_sessions.setdefault(self.days[index], []).append(event)
        self.started = True

    def check_action(self, symbol, day, row):
        event = self.by_ex.get((symbol, day))
        if (event is None or row.get('previous_date') != event.previous_price_session
                or _money(row.get('previous_official'), positive=True) != _money(event.previous_official_rial)
                or _money(row.get('ref_price_raw'), positive=True) != _money(event.ex_reference_rial)):
            raise ValueError('شکاف قیمت/روز برای حق جایزه اظهارشده تطبیق نشد')
        return event

    def begin_day(self, day, session, positions):
        if not self.started or day not in self.days or (self.last_begin and day <= self.last_begin):
            raise ValueError('آغاز روز باید در تقویم و یکتا باشد')
        if self.last_begin != self.last_end:
            raise ValueError('روز قبلی دفتر پایان نیافته است')
        self.last_begin = day
        for symbol, row in session.items():
            if row['valid']:
                self.last_marks[symbol] = (row['mark'], day)
        for event in self.events:
            if event.ex_date != day:
                continue
            lots = self.eligible.get(event.event_id, [])
            if not lots:
                continue
            if any(c['event'].symbol == event.symbol and self._pending(c) > 0 for c in self.claims.values()):
                raise ValueError('استحقاق اقدام دوم روی حقِ قبلیِ تسویه‌نشده هنوز معلوم نیست')
            self.check_action(event.symbol, day, session.get(event.symbol, {}))
            for lot in lots:
                current = positions.get(lot['id'])
                if (current is None or current['quantity'] != lot['quantity']
                        or Fraction(str(current['cost'])) != lot['cost']):
                    raise ValueError('موجودی مشمول تا جلسه بدون حق تغییر کرده؛ تطبیق مستقل لازم است')
            units, basis = Fraction(0), Fraction(0)
            for lot in lots:
                claim_units = lot['quantity'] * event.ratio
                claim_basis = lot['cost'] * event.ratio / (1+event.ratio)
                units += claim_units
                basis += claim_basis
                positions[lot['id']]['cost'] = float(lot['cost']-claim_basis)
            if units > 2**53:
                raise ValueError('تعداد سهم جایزه از دامنه دقیق موتور بیشتر است')
            self.claims[event.event_id] = dict(event=event, units=units, basis=basis, credited=0,
                released=Fraction(0), fraction_settled=False, allocated_basis=Fraction(0), origin_lots=lots)
            self.ledger.append(dict(date=day, event_id=event.event_id, symbol=event.symbol,
                kind='uncredited_bonus_created', units=rational(units), basis_rial=rational(basis), cash_rial='0'))

    @staticmethod
    def _pending(claim):
        return Fraction(0) if claim['fraction_settled'] else claim['units']-claim['credited']

    @property
    def pending_units(self):
        return sum((self._pending(c) for c in self.claims.values()), Fraction(0))

    @property
    def pending_symbols(self):
        return {c['event'].symbol for c in self.claims.values() if self._pending(c) > 0}

    @property
    def pending_whole_symbols(self):
        return {c['event'].symbol for c in self.claims.values() if self._pending(c) >= 1}

    @property
    def pending_basis(self):
        return sum((c['basis']-c['allocated_basis'] for c in self.claims.values()), Fraction(0))

    def end_day(self, day, positions):
        if self.last_begin != day or self.last_end == day:
            raise ValueError('پایان روز به آغاز همان روز و یکتایی نیاز دارد')
        self.last_end = day
        for event in self.record_sessions.get(day, []):
            if any(c['event'].symbol == event.symbol and self._pending(c) > 0 for c in self.claims.values()):
                raise ValueError('روز مشمولِ اقدام جدید با حق قبلیِ تعیین‌تکلیف‌نشده هم‌پوشانی دارد')
            lots = []
            for key, pos in positions.items():
                if pos['symbol'] != event.symbol:
                    continue
                quantity = pos['quantity']
                if (isinstance(quantity, bool) or not math.isfinite(quantity) or not 0 < quantity <= 2**53
                        or quantity != int(quantity)):
                    raise ValueError('تعداد مشمول باید سهم صحیح معلوم در دامنه دقیق باشد')
                lots.append(dict(id=key, quantity=int(quantity), cost=Fraction(_money(pos['cost']))))
            self.eligible[event.event_id] = lots
        credits, cash, realized = {}, Fraction(0), Fraction(0)
        while self.next_release < len(self.release_order) and self.release_order[self.next_release].date <= day:
            release = self.release_order[self.next_release]
            self.next_release += 1
            claim = self.claims.get(release.event_id)
            if claim is None:
                continue
            count = math.floor(claim['units'] * release.ratio)-claim['credited']
            allocated = claim['basis'] * count / claim['units']
            claim['credited'] += count
            claim['released'] = release.ratio
            claim['allocated_basis'] += allocated
            if count:
                symbol = claim['event'].symbol
                mark, stamp = self.last_marks.get(symbol, (float('nan'), None))
                credits['BONUS-'+release.release_id] = dict(symbol=symbol, quantity=float(count), cost=float(allocated),
                    mark=mark, mark_date=stamp, credit_only=True, source_event=release.event_id, source_release=release.release_id)
            self.ledger.append(dict(date=day, stated_release_date=release.date, event_id=release.event_id,
                release_id=release.release_id, kind='bonus_whole_shares_credited', quantity=count,
                basis_rial=rational(allocated), cash_rial='0', original_orders_modified=False))
        while self.next_settlement < len(self.settlement_order) and self.settlement_order[self.next_settlement].date <= day:
            settlement = self.settlement_order[self.next_settlement]
            self.next_settlement += 1
            claim = self.claims.get(settlement.event_id)
            if claim is None:
                continue
            fraction = self._pending(claim)
            if claim['released'] != 1 or not 0 < fraction < 1:
                raise ValueError('وصول کسور فقط برای کسر باقیمانده پس از اعتبار تمام سهام صحیح است')
            amount = Fraction(_money(settlement.net_cash_rial))
            basis = claim['basis']-claim['allocated_basis']
            claim['fraction_settled'] = True
            claim['allocated_basis'] += basis
            cash += amount
            realized += amount-basis
            self.ledger.append(dict(date=day, stated_settlement_date=settlement.date,
                event_id=settlement.event_id, settlement_id=settlement.settlement_id,
                kind='fraction_cash_received', units=rational(fraction), net_cash_rial=str(settlement.net_cash_rial),
                basis_rial=rational(basis), share_price_imputed=False))
        self.total_fraction_cash += cash
        self.total_fraction_realized += realized
        return credits, float(cash), float(realized)

    def records(self):
        return [dict(event=asdict(c['event']), entitlement_units=rational(c['units']),
            pending_units=rational(self._pending(c)), credited_whole_shares=c['credited'],
            cost_basis_rial=rational(c['basis']), pending_cost_basis_rial=rational(c['basis']-c['allocated_basis']),
            fraction_settled=c['fraction_settled'], origin_lots=[dict(lot, cost=rational(lot['cost'])) for lot in c['origin_lots']],
            holder_entitlement_verified=False, holder_credit_verified=False) for c in self.claims.values()]


def replay_bonus_share_portfolio(prices, selections, calendar, config, *, events, releases=(), settlements=(), **kwargs):
    from backtest.portfolio import replay_portfolio

    return replay_portfolio(prices, selections, calendar, config,
        bonus_shares=BonusShareBook(events, releases, settlements), **kwargs)
