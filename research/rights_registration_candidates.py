"""Exploratory links between registration notices and potential right starts.

These are discovery hints only; neither a name nor a publication date verifies an
issuer, event, right, window, theoretical value, or outcome. No prices are read.
"""

from datetime import date

from core.codal_documents import public_document_url
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date

VERSION = 'rights-registration-candidates-v2'


def link_notices(inventory, notices):
    """Keep every same-name start for each unique observed notice, without date filtering."""
    if not isinstance(inventory, dict) or not isinstance(notices, list):
        raise ValueError('Inventory and notice list required')
    rows, starts = inventory.get('candidate_rows'), inventory.get('candidate_starts')
    start, end = inventory.get('source_start'), inventory.get('end_date_exclusive')
    if (not isinstance(rows, list) or not isinstance(starts, list)
            or not is_canonical_iso_date(start) or not is_canonical_iso_date(end) or start >= end):
        raise ValueError('Bounded candidate inventory required')
    parents = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Candidate must be an object')
        code, parent_rows = row.get('ins_code'), row.get('parents')
        if (type(code) is not str or not code.isascii() or not code.isdigit() or code.startswith('0')
                or code in parents or not isinstance(parent_rows, list) or not parent_rows):
            raise ValueError('Unique candidate codes and parents required')
        names = set()
        for parent in parent_rows:
            value = parent.get('parent_symbol') if isinstance(parent, dict) else None
            if not isinstance(value, str) or not normalize_symbol(value):
                raise ValueError('Parent symbol required')
            names.add(normalize_symbol(value))
        parents[code] = (names, len(parent_rows))
    indexed, seen = {}, set()
    for item in starts:
        if not isinstance(item, dict):
            raise ValueError('Start must be an object')
        code, day, censored = item.get('ins_code'), item.get('date'), item.get('left_censored')
        if (code not in parents or not is_canonical_iso_date(day) or not start <= day < end
                or type(censored) is not bool or (code, day) in seen):
            raise ValueError('Unique dated candidate start required')
        seen.add((code, day))
        for name in parents[code][0]:
            indexed.setdefault(name, []).append(dict(ins_code=code, date=day,
                left_censored=censored, parent_count=parents[code][1]))

    by_tracing = {}
    for row in notices:
        if not isinstance(row, dict):
            raise ValueError('Notice must be an object')
        tracing, symbol, published = row.get('tracing_no'), row.get('symbol_fa'), row.get('source_published_at')
        digest, source = row.get('native_metadata_sha256'), row.get('source_folder')
        if (type(tracing) is not int or tracing <= 0 or not isinstance(symbol, str)
                or not normalize_symbol(symbol) or not isinstance(published, str)
                or not is_canonical_iso_date(published[:10]) or not isinstance(source, str) or not source
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Explicit notice identity, date and provenance required')
        url = public_document_url(row['url']) if row.get('url') is not None else None
        pdf_url = public_document_url(row['pdf_url']) if row.get('pdf_url') is not None else None
        company, letter_code = row.get('company_name'), row.get('letter_code')
        if (company is not None and not isinstance(company, str)
                or letter_code is not None and not isinstance(letter_code, str)):
            raise ValueError('Company and notice code must be text or unknown')
        old = by_tracing.get(tracing)
        if old is not None and old['native_metadata_sha256'] != digest:
            raise ValueError('Conflicting metadata for the same tracing number')
        if old is not None and (old['symbol_fa'] != normalize_symbol(symbol)
                or old['source_published_at'] != published or old['title'] != row.get('title')
                or old['url'] != url or old['pdf_url'] != pdf_url or old['company_name'] != company
                or old['letter_code'] != letter_code):
            raise ValueError('A notice digest cannot have conflicting derived fields')
        if old is None:
            old = dict(tracing_no=tracing, symbol_fa=normalize_symbol(symbol),
                source_published_at=published, title=row.get('title'),
                url=url, pdf_url=pdf_url, company_name=company, letter_code=letter_code,
                native_metadata_sha256=digest, sources=[])
            by_tracing[tracing] = old
        if source not in old['sources']:
            old['sources'].append(source)

    linked = []
    for tracing, row in sorted(by_tracing.items()):
        pub_day = date.fromisoformat(row['source_published_at'][:10])
        matches = [{**x, 'days_after_publication': (date.fromisoformat(x['date'])-pub_day).days}
                   for x in indexed.get(row['symbol_fa'], [])]
        matches.sort(key=lambda item: (item['date'], item['ins_code']))
        linked.append({**row, 'sources': sorted(row['sources']), 'same_symbol_starts': matches})
    matched = sum(bool(item['same_symbol_starts']) for item in linked)
    return dict(version=VERSION, notices=linked, unique_notices=len(linked),
        notices_with_same_symbol_start=matched, notices_without_same_symbol_start=len(linked)-matched,
        observed_candidate_starts=len(seen),
        same_symbol_establishes_event_identity=False, no_match_proves_no_right=False,
        historical_universe_complete=False, legal_events_count=None, rights_events_count=None,
        discounted_events_count=None, target_failures_count=None, accepted_for_signals=False)
