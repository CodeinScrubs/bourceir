"""Reporting-scope claims from native Codal metadata, never financial facts or signals."""

import re
from collections import Counter

from core.codal_documents import public_document_url
from core.opportunity_radar import stamp
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import is_canonical_iso_date, jalali_to_iso

VERSION = 'codal-financial-scope-v2'
FALSE_FLAGS = ('document_identity_verified', 'reporting_entity_verified',
    'historical_publication_verified', 'complete_revision_history_verified',
    'native_revision_links_verified', 'standalone_reporting_verified', 'financial_values_read',
    'accepted_for_features', 'accepted_for_signals', 'profitability_proven')
_TITLE = re.compile(
    r'^(?P<prefix>اطلاعات و صورت های مالی میاندوره ای|صورت های مالی)'
    r'(?P<consolidated> تلفیقی)? '
    r'(?:(?:دوره (?P<months>[0-9]{1,2}) ماهه)|(?P<annual>سال مالی))'
    r' منتهی به (?P<period>[0-9]{4}/[0-9]{2}/[0-9]{2})'
    r' \((?P<audit>حسابرسی شده|حسابرسی نشده)\)(?P<tail>.*)$')


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 5000:
        raise ValueError('A bounded native financial metadata string is required')
    return value


def classify_financial_letter(letter, *, expected_symbol, cutoff):
    """Keep named entities, consolidation, duration and audit basis distinct.

    The issuer symbol is the publisher key, not proof of the reporting entity.
    All scope values below are title claims; no statement cells are consumed.
    """
    if not is_canonical_iso_date(cutoff) or not isinstance(letter, dict):
        raise ValueError('Financial metadata and a canonical sealed cutoff are required')
    symbol = normalize_symbol(_text(expected_symbol))
    if (normalize_symbol(_text(letter.get('symbol_fa'))) != symbol
            or to_en_digits(normalize_symbol(_text(letter.get('letter_code')))) != 'ن-10'
            or letter.get('source_estimate') is not False):
        raise ValueError('Native financial issuer, letter code or publication evidence differs')
    published = stamp(letter.get('source_published_at'))
    # Reject held dates before interpreting even the title's period or scope.
    if published.date().isoformat() >= cutoff:
        raise ValueError('Financial publication reaches the sealed boundary')
    trace = letter.get('tracing_no')
    if type(trace) is not int or not 0 < trace < 2**53:
        raise ValueError('A positive native tracing number is required')
    company = normalize_symbol(_text(letter.get('company_name')))
    title = _text(letter.get('title'))
    normalized = to_en_digits(normalize_fa(title.replace('\u200c', ' ')))
    result = dict(version=VERSION, tracing_no=trace, publisher_symbol=symbol,
        publisher_company_claim=company, source_published_at_claim=published.isoformat(),
        source_url=public_document_url(_text(letter.get('url'))), title=title,
        status='unresolved', reasons=[], period_end_jalali=None, period_end_iso=None,
        cumulative_months=None, period_kind_claim=None, scope_claim=None, named_entity_claim=None,
        consolidated_claim=None, audit_claim=None, correction_title_claim=None,
        reporting_key=None, issuer_statement_candidate=False,
        **dict.fromkeys(FALSE_FLAGS, False))
    match = _TITLE.fullmatch(normalized)
    if match is None:
        return result | dict(reasons=['unsupported_financial_title'])
    fields = match.groupdict()
    # "Financial year ended" alone does not prove twelve months: a changed
    # fiscal year can have another duration. Native statement headers are needed.
    months = None if fields['annual'] else int(fields['months'])
    kind = 'annual' if fields['annual'] else 'interim'
    if months is not None and not 1 <= months <= 12:
        return result | dict(reasons=['unsupported_cumulative_month_count'])
    try:
        period = jalali_to_iso(fields['period'])[:10]
    except (TypeError, ValueError, OverflowError):
        return result | dict(reasons=['invalid_financial_period'])
    if not period <= published.date().isoformat() < cutoff:
        return result | dict(reasons=['period_after_publication'])
    tail = fields['tail'].strip()
    qualifiers = re.findall(r'\(([^()]*)\)', tail)
    if re.sub(r'\([^()]*\)', '', tail).strip():
        return result | dict(reasons=['unsupported_scope_qualifier'])
    entity, correction = None, False
    for qualifier in qualifiers:
        if qualifier == 'اصلاحیه' and not correction:
            correction = True
        elif qualifier.startswith('شرکت ') and entity is None and qualifier[5:].strip():
            # Even a similar company name needs native statement corroboration;
            # a publisher key or a name substring is not entity equivalence.
            entity = normalize_symbol(qualifier[5:])
        else:
            return result | dict(reasons=['unsupported_scope_qualifier'])
    # Positive consolidated wording is a title claim. Its absence is unknown,
    # not proof that a statement is standalone or has no consolidated sheets.
    consolidated = True if fields['consolidated'] else None
    scope = 'named_entity' if entity is not None else (
        'issuer_consolidated' if consolidated else 'issuer_unqualified')
    audit = 'audited' if fields['audit'] == 'حسابرسی شده' else 'unaudited'
    key = dict(publisher_symbol=symbol, publisher_company_claim=company,
        scope_claim=scope, named_entity_claim=entity, consolidated_claim=consolidated,
        cumulative_months=months, period_kind_claim=kind, period_end_iso=period, audit_claim=audit)
    return result | dict(status='metadata_only', period_end_jalali=fields['period'],
        period_end_iso=period, cumulative_months=months, period_kind_claim=kind, scope_claim=scope,
        named_entity_claim=entity, consolidated_claim=consolidated, audit_claim=audit,
        correction_title_claim=correction, reporting_key=key,
        issuer_statement_candidate=entity is None)


def financial_scope_catalog(letters, *, expected_symbol, cutoff):
    """Retain unresolved financial titles and all known versions in the census."""
    _text(expected_symbol)
    if (not is_canonical_iso_date(cutoff) or not isinstance(letters, list)
            or len(letters) > 5000 or any(not isinstance(v, dict) for v in letters)):
        raise ValueError('A bounded native metadata list is required')
    financial = [v for v in letters if to_en_digits(normalize_symbol(v.get('letter_code'))) == 'ن-10']
    records = [classify_financial_letter(v, expected_symbol=expected_symbol, cutoff=cutoff) for v in financial]
    traces = [v['tracing_no'] for v in records]
    if len(set(traces)) != len(traces):
        raise ValueError('Duplicate native financial tracing numbers require upstream reconciliation')
    if len({v['publisher_company_claim'] for v in records}) > 1:
        raise ValueError('Publisher company metadata is contradictory')
    groups = {}
    for record in records:
        if record['reporting_key'] is None:
            continue
        key = tuple(record['reporting_key'].values())
        groups.setdefault(key, []).append(record)
    output = []
    for values in groups.values():
        values = sorted(values, key=lambda v: (stamp(v['source_published_at_claim']), v['tracing_no']))
        dates = [v['source_published_at_claim'] for v in values]
        output.append(dict(reporting_key=values[0]['reporting_key'],
            tracing_numbers=[v['tracing_no'] for v in values],
            original_title_claims=sum(v['correction_title_claim'] is False for v in values),
            correction_title_claims=sum(v['correction_title_claim'] is True for v in values),
            publication_claim_ambiguous=len(set(dates)) != len(dates),
            native_revision_links_verified=False, complete_revision_history_verified=False))
    return dict(version=VERSION, cutoff=cutoff, publisher_symbol=normalize_symbol(expected_symbol),
        input_letters=len(letters), nonfinancial_letters=len(letters)-len(financial),
        financial_letters=len(records), records=sorted(records, key=lambda v: v['tracing_no']),
        status_counts=dict(sorted(Counter(v['status'] for v in records).items())),
        scope_claim_counts=dict(sorted(Counter(v['scope_claim'] or 'unresolved' for v in records).items())),
        issuer_statement_candidates=sum(v['issuer_statement_candidate'] for v in records),
        groups=sorted(output, key=lambda v: v['tracing_numbers'][0]),
        values_or_growth_computed=False, metadata_only=True,
        **dict.fromkeys(FALSE_FLAGS, False))
