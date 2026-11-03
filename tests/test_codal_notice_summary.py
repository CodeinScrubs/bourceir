"""Synthetic notice contents are confined to tests."""

import hashlib

import pytest

from core.codal_notice_summary import EXCERPT_LIMIT, summarize_codal_notice
from tests.test_rights_registration import CUTOFF, LICENSE, TEXT, URL, notice


def manifest(body, kind='text'):
    return dict(format=kind, source_url=URL, content_sha256=hashlib.sha256(body).hexdigest(),
                available_at='2026-11-02T09:00:00+03:30', observed_at='2026-11-02T08:59:00+03:30')


def test_exact_capital_and_rights_amounts_are_hints_not_verified_facts():
    body = (LICENSE+TEXT).encode('utf-8')
    summary = summarize_codal_notice(manifest(body), body, notice=notice(), end_exclusive=CUTOFF)
    assert summary['symbol'] == 'آزمون' and summary['tracing_no'] == 321
    assert summary['rights_registration']['claims_and_cash_rial'] == 400_000_000
    assert summary['capital_registration']['capital_before_rial'] == 1_000_000_000
    assert summary['source_available_at'] == '2026-11-02T09:00:00+03:30'
    assert summary['review_reason'] == 'parsed_amounts_still_unverified'
    assert not any(summary[key] for key in ('numeric_facts_reviewed', 'publication_time_verified',
        'historical_identity_verified', 'summary_complete', 'feature_eligible',
        'source_bytes_recoverable', 'accepted_for_signals'))


def test_superseded_notice_is_flagged_without_claiming_newer_notice_was_reviewed():
    body = (TEXT+'\nاین اطلاعیه نسخه جدیدتری به شماره ( ۱۲۳۴ ) دارد.').encode('utf-8')
    summary = summarize_codal_notice(manifest(body), body, notice=notice(), end_exclusive=CUTOFF)
    assert summary['superseding_tracing_numbers'] == [1234]
    assert summary['review_reason'] == 'newer_version_referenced'
    assert summary['supersession_absence_proves_finality'] is False
    assert summary['accepted_for_signals'] is False


def test_pdf_is_never_treated_as_extracted_or_summarized_financial_data():
    body = b'%PDF-1.4\n%%EOF'
    summary = summarize_codal_notice(manifest(body, 'pdf'), body, end_exclusive=CUTOFF)
    assert summary['review_reason'] == 'pdf_content_not_interpreted'
    assert summary['excerpts'] == [] and summary['capital_registration'] is None
    assert summary['requires_manual_review'] is True


def test_metadata_absent_is_unknown_and_url_mismatch_rejected():
    body = TEXT.encode('utf-8')
    summary = summarize_codal_notice(manifest(body), body, end_exclusive=CUTOFF)
    assert summary['symbol'] == 'آزمون' and summary['tracing_no'] is None
    assert summary['metadata_origin'] == 'unverified_document_heading'
    assert summary['historical_identity_verified'] is False
    assert summary['rights_registration'] is None and summary['review_reason'] == 'notice_metadata_missing'
    with pytest.raises(ValueError, match='source URL'):
        summarize_codal_notice(manifest(body), body, notice=notice(url=URL+'x'), end_exclusive=CUTOFF)


def test_excerpt_is_bounded_and_does_not_claim_completeness():
    body = ('سود '+('نمونه '*3000)).encode('utf-8')
    summary = summarize_codal_notice(manifest(body), body, end_exclusive=CUTOFF)
    assert sum(len(line)+1 for line in summary['excerpts']) <= EXCERPT_LIMIT
    assert summary['excerpt_truncated'] and not summary['summary_complete']


def test_invalid_text_is_not_silently_replaced():
    body = b'\xff'
    with pytest.raises(ValueError, match='UTF-8'):
        summarize_codal_notice(manifest(body), body, end_exclusive=CUTOFF)


def test_multiple_headings_never_choose_an_issuer():
    body = (TEXT+'\nشرکت دیگری - نماد: دیگر').encode('utf-8')
    summary = summarize_codal_notice(manifest(body), body, end_exclusive=CUTOFF)
    assert summary['symbol'] is None and summary['company_name'] is None
    assert summary['metadata_origin'] == 'unavailable'
