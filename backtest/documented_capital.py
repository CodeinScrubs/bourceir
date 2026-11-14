"""Opt-in legal-unit claims with observed market reference prices.

No shares are made saleable and no rights sale/payment is invented. Original
tradeable quantities stay fixed. The theoretical reference and its residual are
diagnostics, not rules for fabricating quantities or explaining rounding.
"""
import math
import re
from bisect import bisect_right
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction

from backtest.corporate_actions import DatedCashDividendBook
from core.daily_publications import parse_publication
from core.no_trade_valuation import parse_no_trade_publication
from core.timeutils import is_canonical_iso_date

VERSION = 'documented-capital-uncredited-claims-v1'


def rational(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


@dataclass(frozen=True)
class DocumentedCapitalEvent:
    event_id: str
    symbol: str
    record_date: str
    ex_date: str
    previous_price_session: str
    previous_official_rial: int
    observed_reference_rial: int
    capital_before_rial: int
    capital_after_rial: int
    cash_and_claims_rial: int
    retained_earnings_rial: int
    revaluation_rial: int
    subscription_price_rial: int
    document_sha256: str
    native_sha256: str
    subscription_price_verified: bool = False
    other_reserves_rial: int = 0

    def __post_init__(self):
        if type(self.subscription_price_verified) is not bool:
            raise ValueError('Subscription-price evidence status must be explicit')
        if (not isinstance(self.event_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',self.event_id)
                or not isinstance(self.symbol,str) or not self.symbol.isascii() or not self.symbol.isdigit()
                or any(not is_canonical_iso_date(x) for x in (self.record_date,self.ex_date,self.previous_price_session))
                or not self.previous_price_session<=self.record_date<self.ex_date):
            raise ValueError('Dated, stable native identity and record/reference sequence are required')
        for field in ('previous_official_rial','observed_reference_rial','capital_before_rial','capital_after_rial',
                      'cash_and_claims_rial','retained_earnings_rial','revaluation_rial','subscription_price_rial','other_reserves_rial'):
            v=getattr(self,field)
            if type(v) is not int or not 0<=v<2**63:
                raise ValueError('Explicit bounded integer-rial amounts are required')
        if (min(self.previous_official_rial,self.observed_reference_rial,self.capital_before_rial)<=0
                or self.capital_after_rial<=self.capital_before_rial
                or self.cash_and_claims_rial>0 and self.subscription_price_rial<=0
                or self.capital_before_rial+self.cash_and_claims_rial+self.retained_earnings_rial+self.revaluation_rial+self.other_reserves_rial
                    !=self.capital_after_rial):
            raise ValueError('Complete source funding components must reconcile exactly')
        if any(not isinstance(x,str) or not re.fullmatch('[a-f0-9]{64}',x)
               for x in (self.document_sha256,self.native_sha256)):
            raise ValueError('Primary document and dated native evidence hashes are required')

    @property
    def bonus_ratio(self):
        return Fraction(self.retained_earnings_rial+self.revaluation_rial+self.other_reserves_rial,self.capital_before_rial)

    @property
    def rights_ratio(self):
        return Fraction(self.cash_and_claims_rial,self.capital_before_rial)

    @property
    def theoretical_reference(self):
        return (self.previous_official_rial+self.rights_ratio*self.subscription_price_rial)/(1+self.bonus_ratio+self.rights_ratio)


def bind_capital_event(*, observation, **fields):
    """Bind a reviewed event to exact dated trade-day or zero-activity bytes."""
    symbol,day=fields['symbol'],fields['ex_date']
    publication=parse_publication(observation,symbol,day,'price')
    if publication['status']=='valid_price':
        reference=publication['row']['ref_price_raw']
    else:
        proof=parse_no_trade_publication(observation,symbol,day)
        if not proof['flat_mark_eligible']:raise ValueError('Dated price or zero-activity reference witness required')
        reference=proof['reference_rial']
    if observation.get('http_status')!=200 or reference!=fields['observed_reference_rial']:
        raise ValueError('Observed official reference must match the declared market basis')
    return DocumentedCapitalEvent(**fields,native_sha256=observation['body_sha256'])


class DocumentedCapitalBook:
    """No-credit/no-payment scenario; not a complete entitlement valuation.

    Unknown future holder credit or rights availability never becomes spendable
    cash. This contract intentionally does not model releases or rights sales.
    A rights-sale policy remains pending evidence of availability and execution.
    """
    def __init__(self,events,*,dividends):
        self.events=tuple(events)
        if (len(self.events)>1000 or any(not isinstance(e,DocumentedCapitalEvent) for e in self.events)
                or len({e.event_id for e in self.events})!=len(self.events)
                or len({(e.symbol,e.ex_date) for e in self.events})!=len(self.events)
                or not isinstance(dividends,DatedCashDividendBook) or dividends.started or dividends.payments):
            raise ValueError('Unique documented capital events and a fresh unpaid dividend book are required')
        self.by_ex={(e.symbol,e.ex_date):e for e in self.events}
        if set(self.by_ex)&set(dividends.by_ex):
            raise ValueError('Simultaneous dividend/capital references require a separate contract')
        self.dividends=dividends
        self.eligible={};self.claims={};self.ledger=[];self.additional_entitlements_unknown=[]
        self.started=False;self.last_begin=None;self.last_end=None

    def start(self,calendar):
        if self.started:raise ValueError('A fresh book is required for every replay')
        self.days=list(calendar)
        if not self.days or self.days!=sorted(set(self.days)):raise ValueError('A sorted unique calendar is required')
        self.record_sessions={}
        for e in self.events:
            i=bisect_right(self.days,e.record_date)-1
            if i<0 or e.ex_date not in self.days:raise ValueError('Event record and basis sessions must be in the declared calendar')
            self.record_sessions.setdefault(self.days[i],[]).append(e)
        self.dividends.start(self.days);self.started=True

    @contextmanager
    def _atomic(self):
        saved=deepcopy(self.__dict__)
        try:yield
        except Exception:
            self.__dict__.clear();self.__dict__.update(saved)
            raise

    def check_action(self,symbol,day,row):
        e=self.by_ex.get((symbol,day))
        if e is None:return self.dividends.check_action(symbol,day,row)
        if (row.get('previous_date')!=e.previous_price_session
                or Fraction(str(row.get('previous_official')))!=e.previous_official_rial
                or Fraction(str(row.get('ref_price_raw')))!=e.observed_reference_rial):
            raise ValueError('The market reference witness does not match this observed price predecessor')
        return e

    def begin_day(self,day,session,positions):
        with self._atomic():return self._begin_day(day,session,positions)

    def _begin_day(self,day,session,positions):
        if (not self.started or day not in self.days or self.last_begin!=self.last_end
                or self.last_begin is not None and day<=self.last_begin):
            raise ValueError('Capital-book transitions must be ordered and complete')
        self.last_begin=day
        self.dividends.begin_day(day,session)
        for e in self.events:
            quantity=self.eligible.get(e.event_id,0)
            if e.ex_date!=day or not quantity:continue
            self.check_action(e.symbol,day,session.get(e.symbol,{}))
            if e.symbol in self.pending_symbols:
                self.additional_entitlements_unknown.append(dict(event_id=e.event_id,symbol=e.symbol,
                    kind='additional_capital_on_uncredited_claims_unknown'))
            self.claims[e.event_id]=dict(event=e,eligible_original_units=quantity,
                pending_bonus_units=quantity*e.bonus_ratio,pending_rights_units=quantity*e.rights_ratio)
            self.ledger.append(dict(date=day,event_id=e.event_id,symbol=e.symbol,
                kind='uncredited_capital_claim_created',bonus_units=rational(quantity*e.bonus_ratio),
                rights_units=rational(quantity*e.rights_ratio),cash_rial='0',saleable_shares_created=0,
                reference_residual_rial=rational(Fraction(e.observed_reference_rial)-e.theoretical_reference),
                rounding_rule_verified=False,subscription_price_verified=e.subscription_price_verified))

    @property
    def pending_symbols(self):
        return {c['event'].symbol for c in self.claims.values()
                if c['pending_bonus_units']+c['pending_rights_units']>0}

    @property
    def pending_whole_symbols(self):
        return {c['event'].symbol for c in self.claims.values()
                if c['pending_bonus_units']+c['pending_rights_units']>=1}

    @property
    def has_unvalued_claims(self):
        return bool(self.pending_symbols or self.additional_entitlements_unknown or self.dividends.outstanding_face_rial>0)

    def end_day(self,day,positions):
        with self._atomic():return self._end_day(day,positions)

    def _end_day(self,day,positions):
        if self.last_begin!=day or self.last_end==day:raise ValueError('A unique matching beginning is required')
        aggregate={}
        for pos in positions.values():
            q=pos['quantity']
            if not math.isfinite(q) or q<0 or q!=int(q):raise ValueError('Only integer original shares are supported')
            aggregate.setdefault(pos['symbol'],dict(quantity=0))['quantity']+=int(q)
        for e in self.record_sessions.get(day,[]):self.eligible[e.event_id]=aggregate.get(e.symbol,{}).get('quantity',0)
        for e in self.dividends.by_record_date.get(day,()):
            if e.symbol in self.pending_symbols:
                self.additional_entitlements_unknown.append(dict(event_id=e.event_id,symbol=e.symbol,
                    kind='additional_dividend_on_uncredited_claims_unknown'))
        cash=self.dividends.end_day(day,aggregate)
        if cash!=0:raise ValueError('This book cannot manufacture a dividend or rights-sale receipt')
        self.last_end=day
        return {},0.,0.

    def records(self):
        return [dict(event_id=key,symbol=c['event'].symbol,eligible_original_units=c['eligible_original_units'],
            pending_bonus_units=rational(c['pending_bonus_units']),pending_rights_units=rational(c['pending_rights_units']),
            holder_credit_date=None,rights_available_date=None,rights_sale_proceeds_rial=None,
            cash_received_rial='0',fair_value_rial=None) for key,c in sorted(self.claims.items())]
