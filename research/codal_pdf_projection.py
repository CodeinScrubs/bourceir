"""Quarantined PDF-prefix recovery for one observed CODAL server error.

This does NOT change normal document acceptance. The returned exact byte slice
still needs an independent PDF parser and visual review. It is a projection of
an invalid source response, never a silently repaired original document.
"""
import hashlib
import re
from datetime import datetime

from core.codal_documents import pdf_has_valid_ending, public_document_url

VERSION = 'codal-pdf-prefix-projection-v1'
MAX_BYTES = 20*1024*1024


def recover_codal_pdf_projection(receipt, body):
    if (not isinstance(receipt, dict) or not isinstance(body, bytes)
            or not 0 < len(body) <= MAX_BYTES or not body.startswith(b'%PDF-')
            or receipt.get('status') != 'error' or receipt.get('error_reason') != 'invalid_pdf_ending'
            or type(receipt.get('http_status')) is not int or receipt['http_status'] != 200
            or type(receipt.get('response_bytes')) is not int or receipt['response_bytes'] != len(body)
            or receipt.get('response_sha256') != hashlib.sha256(body).hexdigest()):
        raise ValueError('Exact bounded invalid-PDF response and source receipt are required')
    for key in ('source_url', 'effective_url'):
        public_document_url(receipt.get(key))
    stamps = []
    for key in ('requested_at', 'observed_at'):
        try:
            stamp = datetime.fromisoformat(receipt[key])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('Aware source observation timestamps are required') from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('Source timestamps cannot be naive')
        stamps.append(stamp)
    if stamps[1] < stamps[0]:
        raise ValueError('Source receipt time precedes request time')
    ending = list(re.finditer(rb'startxref\s+[0-9]+\s+%%EOF', body))
    if not ending:
        raise ValueError('An explicit PDF xref trailer is required')
    end = ending[-1].end()
    suffix = body[end:]
    # Deliberately narrow. Do not accept arbitrary HTML, scripts, unknown
    # trailing binary data, or another PDF document as the same source error.
    if (not 0 < len(suffix) <= 65536 or not suffix.lstrip().lower().startswith(b'<!doctype html>')
            or b'<title>Thread was being aborted.</title>' not in suffix
            or b'customErrors' not in suffix or b'<script' in suffix.lower()
            or b'%PDF-' in suffix):
        raise ValueError('Unsupported trailing response; do not relax normal PDF guards')
    prefix = body[:end]
    if not pdf_has_valid_ending(prefix):
        raise ValueError('Projected byte slice has no valid PDF ending')
    return dict(version=VERSION, projection=prefix,
        source_response_sha256=receipt['response_sha256'], source_response_bytes=len(body),
        projection_sha256=hashlib.sha256(prefix).hexdigest(), projection_bytes=len(prefix),
        removed_suffix_sha256=hashlib.sha256(suffix).hexdigest(), removed_suffix_bytes=len(suffix),
        source_url=receipt['source_url'], effective_url=receipt['effective_url'],
        requested_at=receipt['requested_at'], observed_at=receipt['observed_at'],
        original_response_is_valid_document=False, exact_source_prefix=True,
        requires_independent_pdf_validation=True, requires_visual_review=True,
        accepted_for_financial_facts=False, accepted_for_signals=False,
        historical_availability_verified=False, html_executed=False)
