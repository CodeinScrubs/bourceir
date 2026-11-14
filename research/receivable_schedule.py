"""Classify a source-linked payment plan without turning it into paid cash."""

import re
from datetime import datetime

from core.codal_documents import public_document_url
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'receivable-schedule-v1'
HOLDERS = frozenset({'individual_sejam', 'individual_nonsejam', 'legal_sejam', 'legal_nonsejam'})


def classify_payment_plan(record, *, symbol, fiscal_year_end, terminal, holder_group):
    """Assess a manually reviewed fact, not OCR or source authenticity.

    A deadline after the terminal does not establish an unpaid receivable at the
    terminal: the issuer might have paid earlier. A plan for a different holder
    group cannot replace the matching group's older notice.
    """
    if not isinstance(record, dict) or not isinstance(symbol, str) or not normalize_symbol(symbol):
        raise ValueError('A source-linked schedule and definite issuer are required')
    if (not is_canonical_iso_date(fiscal_year_end) or not is_canonical_iso_date(terminal)
            or not isinstance(holder_group, str) or holder_group not in HOLDERS):
        raise ValueError('Canonical dates and a definite holder group are required')
    if (not isinstance(record.get('symbol'), str)
            or normalize_symbol(record['symbol']) != normalize_symbol(symbol)
            or record.get('fiscal_year_end') != fiscal_year_end):
        raise ValueError('Issuer and dividend period must match the claim')
    tracing = record.get('tracing_no')
    if type(tracing) is not int or tracing <= 0:
        raise ValueError('A definite source notice identity is required')
    url = public_document_url(record.get('source_url'))
    if not isinstance(record.get('source_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['source_sha256']):
        raise ValueError('The reviewed source bytes need their recorded hash')
    try:
        published = datetime.fromisoformat(record['source_published_at'])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError('An explicit aware publication timestamp is required') from error
    if published.tzinfo is None or published.utcoffset() is None:
        raise ValueError('An explicit aware publication timestamp is required')
    groups = record.get('holder_groups')
    if (not isinstance(groups, list) or not groups
            or any(not isinstance(g, str) or g not in HOLDERS for g in groups)
            or len(set(groups)) != len(groups)):
        raise ValueError('Reviewed holder groups must be definite and unique')
    kind = record.get('date_kind')
    start, end = record.get('start_date'), record.get('end_date')
    if kind == 'conditional':
        if start is not None or end is not None:
            raise ValueError('A conditional undated plan cannot invent a date')
        relation = 'conditional_payment_date_unknown'
    elif kind == 'deadline':
        if start is not None or not is_canonical_iso_date(end):
            raise ValueError('A deadline has an upper date, not an invented start')
        relation = ('planned_deadline_before_or_at_terminal' if end <= terminal
                    else 'deadline_after_terminal_payment_timing_unknown')
    elif kind in ('fixed', 'window'):
        if (not is_canonical_iso_date(start) or not is_canonical_iso_date(end)
                or start > end or (kind == 'fixed' and start != end)):
            raise ValueError('A fixed date or ordered payment window is required')
        relation = ('planned_before_or_at_terminal' if end <= terminal
                    else 'planned_after_terminal' if start > terminal
                    else 'planned_window_straddles_terminal')
    else:
        raise ValueError('The reviewed timing semantics must be explicit')
    matches = holder_group in groups
    return dict(version=VERSION, symbol=normalize_symbol(symbol), fiscal_year_end=fiscal_year_end,
        terminal=terminal, holder_group=holder_group, tracing_no=tracing,
        source_url=url, source_sha256=record['source_sha256'],
        source_published_at=published.astimezone(TEHRAN_TZ).isoformat(),
        source_published_by_terminal=published.astimezone(TEHRAN_TZ).date().isoformat() <= terminal,
        applicable_to_holder=matches,
        timing_relation=relation if matches else 'different_holder_group',
        date_kind=kind, start_date=start, end_date=end,
        actual_payment_date=None, actual_payment_verified=False, spendable_cash_rial=None,
        source_authenticity_verified=False, feature_eligible=False, accepted_for_signals=False)
