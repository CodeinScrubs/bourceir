"""A valuation-only view of sessions with dated zero-activity evidence."""
from dataclasses import dataclass


@dataclass(frozen=True)
class _Mark:
    date: str
    price: float
    raw_date: str
    raw_price: float


class ValuationGap(ValueError):
    def __init__(self, symbol, status, **evidence):
        super().__init__(status)
        self.symbol = symbol
        self.status = status
        self.evidence = evidence


class ObservedValuation:
    def __init__(self, proofs):
        self.proofs = proofs
        self.last_marks = {}

    def session(self, raw, day, previous_day, exposed, dividends):
        """Keep source-mark rows out of the execution and selection views."""
        valuation_rows = {}
        flat = {}
        for symbol in sorted(exposed):
            row = raw.get(symbol)
            proof = self.proofs.get((symbol, day))
            previous = self.last_marks.get(symbol)
            if row is not None:
                if proof is not None:
                    raise ValuationGap(symbol, 'conflicting_no_trade_valuation', evidence_sha256=proof['body_sha256'])
                # A cash ex-date during a suspension can already have changed
                # the valuation basis before the first post-event candle.
                if previous is not None and previous.date == previous_day and previous.raw_date != previous.date:
                    if row['previous_date'] != previous.raw_date or row['previous_official'] != previous.raw_price:
                        raise ValuationGap(symbol, 'conflicting_valuation_predecessor')
                    if previous.price != previous.raw_price:
                        valuation_rows[symbol] = dict(row, previous_official=previous.price,
                                                      action=row['ref_price_raw'] / previous.price)
                continue
            if proof is None:
                raise ValuationGap(symbol, 'missing_held_mark')
            sha = proof['body_sha256']
            if previous is None or previous.date != previous_day:
                raise ValuationGap(symbol, 'discontinuous_no_trade_valuation', evidence_sha256=sha)
            mark = proof['official_close_rial']
            view = dict(mark_valid=True, mark=mark, ref_price_raw=proof['reference_rial'],
                        previous_official=previous.price, previous_date=previous.raw_date, action=mark/previous.price)
            if mark != previous.price:
                if dividends is None or (symbol, day) not in dividends.by_ex:
                    raise ValuationGap(symbol, 'discontinuous_no_trade_valuation', evidence_sha256=sha)
                try:
                    dividends.check_action(symbol, day, view)
                except ValueError as error:
                    raise ValuationGap(symbol, 'unknown_corporate_entitlement',
                                       reason=str(error), evidence_sha256=sha) from error
            valuation_rows[symbol] = view
            flat[symbol] = dict(evidence_sha256=sha, mark_rial=mark, declared_cash_ex=mark != previous.price)
        self.last_marks.update((symbol, _Mark(day, row['mark'], day, row['mark']))
                               for symbol, row in raw.items() if row['mark_valid'])
        for symbol, mark in flat.items():
            previous = self.last_marks[symbol]
            self.last_marks[symbol] = _Mark(day, mark['mark_rial'], previous.raw_date, previous.raw_price)
        return (dict(raw) | valuation_rows if valuation_rows else raw), flat
