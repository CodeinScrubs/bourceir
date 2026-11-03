"""Explicit sequential bonus/dividend accounting; no inferred credits or payments.

The older single-action books keep their existing contracts. This opt-in adapter
separates an actual meeting date from its inventory snapshot and previous price.
It rejects simultaneous actions and entitlements on uncredited bonus shares.
"""

from bisect import bisect_right
from contextlib import contextmanager
from copy import deepcopy

from backtest.bonus_shares import BonusShareBook
from backtest.cash_dividends import CashDividendBook, _exact, _money
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date

VERSION = 'explicit-bonus-dividend-book-v1'
PORTFOLIO_VERSION = 'portfolio-bonus-dividends-v1'


class DatedCashDividendBook(CashDividendBook):
    """Meeting date, EOD inventory snapshot, and previous price are distinct.

    A previous price session is supplied for every event and checked against the
    raw row; it is never guessed from a dividend amount or an issuer registration.
    Mapping a closed meeting date to an observed session is a model convention,
    not independent proof that the historical market calendar is complete.
    """

    def __init__(self, events, payments=(), *, previous_price_sessions):
        super().__init__(events, payments)
        if (not isinstance(previous_price_sessions, dict)
                or set(previous_price_sessions) != set(self.by_id)
                or any(not is_canonical_iso_date(day) or day > self.by_id[key].record_date
                       for key, day in previous_price_sessions.items())):
            raise ValueError('آخرین جلسه قیمت پیش از مجمع باید برای تک‌تک رخدادها صریح باشد')
        self.previous_price_sessions = dict(previous_price_sessions)
        self.record_snapshot_sessions = {}

    def start(self, calendar):
        if self.started:
            raise ValueError('برای هر بازپخش دفتر تازه لازم است')
        days = list(calendar)
        if (not days or days != sorted(set(days))
                or any(not is_canonical_iso_date(day) for day in days)):
            raise ValueError('تقویم صریح، یکتا و مرتب لازم است')
        sessions, snapshots, by_snapshot = set(days), {}, {}
        for event in self.events:
            index = bisect_right(days, event.record_date)-1
            if index < 0 or event.ex_date not in sessions:
                raise ValueError('موجودی پیش از مجمع و جلسه بدون حق در بازه صریح لازم‌اند')
            previous = self.previous_price_sessions[event.event_id]
            if previous not in sessions or previous > days[index]:
                raise ValueError('جلسه قیمت قبلی باید در تقویم و پیش از عکس موجودی مجمع باشد')
            snapshots[event.event_id] = days[index]
            by_snapshot.setdefault(days[index], []).append(event)
        self.record_snapshot_sessions, self.by_record_date = snapshots, by_snapshot
        self.sessions, self.started = sessions, True

    def check_action(self, symbol, day, row):
        event = self.by_ex.get((symbol, day))
        if event is None:
            raise ValueError('شکاف مرجع، رخداد سود نقدی صریح ندارد')
        previous = _money(row.get('previous_official'), positive=True)
        reference = _money(row.get('ref_price_raw'), positive=True)
        if (row.get('previous_date') != self.previous_price_sessions[event.event_id]
                or _exact('sub', previous, reference) != _money(event.dps_rial)):
            raise ValueError('جلسه قیمت قبلی یا شکاف مرجع با سند سود نقدی برابر نیست')
        return event


class BonusDividendBook(BonusShareBook):
    """One lot inventory, explicit bonus credits, and separate cash receivables.

    Every begin/end transition rolls back both books and the lot inventory on
    validation failure. An uncertain entitlement cannot leave half a cash credit
    or partly allocated cost in the state that is reported to the caller.
    """

    def __init__(self, events, releases=(), settlements=(), *, dividends, credit_exit_session=None):
        super().__init__(events, releases, settlements)
        if credit_exit_session is not None and not is_canonical_iso_date(credit_exit_session):
            raise ValueError('جلسه خروج سهام اعتباری باید تاریخ صریح یا نامعلوم باشد')
        if not isinstance(dividends, CashDividendBook) or dividends.started:
            raise ValueError('دفتر سود نقدی تازه و صریح لازم است')
        if set(self.by_id) & set(dividends.by_id):
            raise ValueError('شناسه رخداد جایزه و نقدی باید مستقل و یکتا باشد')
        if set(self.by_ex) & set(dividends.by_ex):
            raise ValueError('اقدام نقدی و جایزه هم‌زمان به قرارداد مستقل نیاز دارد')
        self.dividends = deepcopy(dividends)
        self.credit_exit_session = credit_exit_session

    @contextmanager
    def _atomic(self, positions=None):
        saved = deepcopy(self.__dict__)
        inventory = None if positions is None else deepcopy(positions)
        try:
            yield
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(saved)
            if positions is not None:
                positions.clear()
                positions.update(inventory)
            raise

    def start(self, calendar):
        days = list(calendar)
        with self._atomic():
            super().start(days)
            if self.credit_exit_session is not None and self.credit_exit_session not in self.days:
                raise ValueError('جلسه خروج سهام اعتباری در تقویم بازپخش نیست')
            self.dividends.start(days)

    def check_action(self, symbol, day, row):
        if (symbol, day) in self.dividends.by_ex:
            return self.dividends.check_action(symbol, day, row)
        return super().check_action(symbol, day, row)

    def begin_day(self, day, session, positions):
        with self._atomic(positions):
            self.dividends.begin_day(day, session)
            super().begin_day(day, session, positions)

    def end_day(self, day, positions):
        with self._atomic(positions):
            for event in self.dividends.by_record_date.get(day, ()):
                if event.symbol in self.pending_symbols:
                    raise ValueError('سود نقدی روی سهام جایزه دریافت‌نشده به استحقاق مستقل نیاز دارد')
            aggregate = {}
            for position in positions.values():
                symbol = normalize_symbol(position['symbol'])
                quantity = _money(position['quantity'], positive=True)
                if quantity != quantity.to_integral_value() or quantity > 2**53:
                    raise ValueError('تعداد مشمول سود نقدی باید صحیح و در دامنه دقیق باشد')
                aggregate[symbol] = aggregate.get(symbol, 0)+int(quantity)
            dividend_cash = self.dividends.end_day(day, {s: dict(quantity=q) for s, q in aggregate.items()})
            credits, bonus_cash, bonus_realized = super().end_day(day, positions)
            return credits, bonus_cash+dividend_cash, bonus_realized+dividend_cash

    @property
    def has_unvalued_claims(self):
        return self.pending_units > 0 or self.dividends.outstanding_face_rial > 0


def replay_corporate_action_portfolio(prices, selections, calendar, config, *, events, dividends,
                                      releases=(), settlements=(), **kwargs):
    from backtest.portfolio import replay_portfolio

    prices, selections = prices.copy(), selections.copy()
    prices['symbol'] = prices.symbol.map(normalize_symbol)
    selections['symbol'] = selections.symbol.map(normalize_symbol)
    return replay_portfolio(prices, selections, calendar, config,
        bonus_shares=BonusDividendBook(events, releases, settlements, dividends=dividends), **kwargs)
