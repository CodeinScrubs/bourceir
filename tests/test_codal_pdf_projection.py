import hashlib

import pytest

from core.codal_documents import pdf_has_valid_ending
from research.codal_pdf_projection import recover_codal_pdf_projection


def sample():
    prefix = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\nstartxref\n9\n%%EOF'
    suffix = (b'\n<!DOCTYPE html>\n<html><title>Thread was being aborted.</title>'
              b'<body>customErrors</body></html>')
    body = prefix+suffix
    receipt = dict(status='error', error_reason='invalid_pdf_ending', http_status=200,
        response_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest(),
        source_url='https://codal.ir/DownloadFile.aspx?hs=public&ft=1005',
        effective_url='https://codal.ir/DownloadFile.aspx?hs=public&ft=1005',
        requested_at='2026-11-13T18:00:00+03:30', observed_at='2026-11-13T18:00:01+03:30')
    return prefix, body, receipt


def test_recovery_is_exact_projection_not_normal_document_acceptance():
    prefix, body, receipt = sample()
    assert not pdf_has_valid_ending(body)
    result = recover_codal_pdf_projection(receipt, body)
    assert result['projection'] == prefix
    assert result['projection_sha256'] == hashlib.sha256(prefix).hexdigest()
    assert result['removed_suffix_bytes'] == len(body)-len(prefix)
    assert result['requires_independent_pdf_validation'] and result['requires_visual_review']
    assert not result['original_response_is_valid_document']
    assert not result['accepted_for_financial_facts'] and not result['accepted_for_signals']


@pytest.mark.parametrize('field,value', [
    ('status', 'captured'), ('error_reason', 'timeout'), ('http_status', 206),
    ('response_bytes', 1), ('response_sha256', '0'*64),
    ('source_url', 'https://evil.invalid/DownloadFile.aspx'),
    ('observed_at', '2026-11-13T17:00:00+03:30'),
    ('requested_at', '2026-11-13T18:00:00'),
])
def test_rejects_invalid_source_receipt(field, value):
    _, body, receipt = sample()
    receipt[field] = value
    with pytest.raises(ValueError):
        recover_codal_pdf_projection(receipt, body)


@pytest.mark.parametrize('suffix', [b'\n<html>ordinary error</html>', b'\x00',
    b'\n<!DOCTYPE html><title>Thread was being aborted.</title>customErrors<script>alert(1)</script>',
    b'\n<!DOCTYPE html><title>Thread was being aborted.</title>customErrors%PDF-1.4',
    b'\n<!DOCTYPE html><title>Thread was being aborted.</title>customErrors'+b' '*65536],
    ids=['ordinary-html', 'binary-padding', 'script', 'second-pdf', 'oversized-suffix'])
def test_unknown_trailing_content_is_not_accepted(suffix):
    prefix, _, receipt = sample()
    body = prefix+suffix
    receipt.update(response_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest())
    with pytest.raises(ValueError):
        recover_codal_pdf_projection(receipt, body)
