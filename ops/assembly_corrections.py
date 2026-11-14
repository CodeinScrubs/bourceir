"""Capture the entire discovered assembly revision inventory within a declared budget."""

import uuid
from datetime import datetime
from pathlib import Path

from config import settings
from core.assembly_corrections import MAX_NOTICES, resolve_assembly_corrections
from core.assembly_dividend import FAMILY
from core.codal_documents import public_document_url
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, now_tehran_iso
from ops.codal_documents import capture_codal_document, read_codal_document_file
from ops.validation_run import write_state


def capture_assembly_corrections(letters, *, end_exclusive, document_budget=64, provider=None, root=None):
    """Every discovered version remains in the denominator, even if not captured.

    Letters must be the dated, identity-checked discovery output. A collector
    limit never asserts finality. Public HTML is hashed in transport, then only
    visible text is archived by the existing document archive privacy contract.
    """
    if (not isinstance(letters, list) or len(letters) > MAX_NOTICES
            or type(document_budget) is not int or not 1 <= document_budget <= MAX_NOTICES
            or not is_canonical_iso_date(end_exclusive)):
        raise ValueError('Bounded discovered letters, document budget and cutoff required')
    unique = {}
    for letter in letters:
        if not isinstance(letter, dict) or type(letter.get('tracing_no')) is not int or letter['tracing_no'] <= 0:
            raise ValueError('Native positive tracing identity required')
        if not isinstance(letter.get('title'), str) or FAMILY not in letter['title']:
            raise ValueError('Only the declared ordinary assembly family is supported')
        published = datetime.fromisoformat(letter['source_published_at'])
        if published.utcoffset() is None or published.astimezone(TEHRAN_TZ).date().isoformat() >= end_exclusive:
            raise ValueError('Discovery metadata must precede the sealed boundary')
        public_document_url(letter['url'])
        tracing = letter['tracing_no']
        if tracing in unique and unique[tracing] != letter:
            raise ValueError('Conflicting discovery metadata for the same notice')
        unique[tracing] = letter
    private = (settings.DATA_DIR / 'private').resolve()
    base = Path(root or private / 'assembly-corrections').resolve()
    if not base.is_relative_to(private):
        raise ValueError('Evidence must stay in data/private')
    folder = base / ('ASSEMBLY-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    write_state(folder / 'protocol.json', dict(created_at=now_tehran_iso(), expected_tracing_numbers=sorted(unique),
        document_budget=document_budget, end_exclusive=end_exclusive, letters=list(unique.values()),
        discovery_completeness_verified=False, entitlement_verified=False, settlement_verified=False))
    records, attempted, paused = [], 0, False
    for tracing, letter in sorted(unique.items()):
        notice = dict(tracing_no=tracing, symbol_fa=letter['symbol_fa'], company_name=letter['company_name'],
            title=letter['title'], published_at=letter['source_published_at'])
        record = dict(notice=notice, text=None, capture_status='not_attempted_budget', capture=None)
        if paused:
            record['capture_status'] = 'not_attempted_after_verification_request'
        elif attempted < document_budget:
            attempted += 1
            capture = capture_codal_document(letter['url'], provider=provider, root=folder / 'documents')
            record.update(capture=capture, capture_status=capture['status'])
            if capture['status'] == 'captured' and capture['format'] == 'text':
                record['text'] = read_codal_document_file(capture['folder'], 'report.txt').decode('utf-8')
            paused = capture['status'] == 'verification_required'
        records.append(record)
        write_state(folder / 'progress.json', dict(records=records, requests=attempted, paused_for_verification=paused))
    resolved = resolve_assembly_corrections(records, expected_tracing_numbers=sorted(unique),
                                           end_exclusive=end_exclusive)
    result = dict(folder=str(folder), requests=attempted, expected_notices=len(unique), records=records,
        resolution=resolved, paused_for_verification=paused, unattempted=sum(r['capture'] is None for r in records),
        received_cash_verified=False, accepted_for_signals=False)
    write_state(folder / 'result.json', result)
    return result
