"""Quality audit describes observed data and leaves missing evidence unknown."""

from datetime import timedelta

import pytest

from ops.intraday_audit import audit_intraday
from tests.test_audit_followup import live_book


def test_empty_audit_does_not_report_perfect_quality(db):
    result = audit_intraday(db)
    assert result['status'] == 'no_data' and result['rows'] == 0
    assert 'provider_fresh_fraction' not in result
    assert not result['own_fill_calibration_available']


def test_audit_counts_stale_quotes_and_observed_capture_gaps(db):
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    clock, book = live_book()
    for elapsed in (0, 10, 120):
        db.insert_snapshot(sid, dict(book, captured_at=(clock + timedelta(seconds=elapsed)).isoformat()))
    result = audit_intraday(db, '2026-10-14')
    assert result['rows'] == 3 and result['symbols'] == 1
    assert result['provider_stale_rows'] == 1 and result['provider_fresh_fraction'] == pytest.approx(2 / 3)
    assert result['observable_ask_rows'] == 2
    assert result['per_symbol'][0]['max_gap_seconds'] == 110
    assert not result['full_session_coverage_known'] and not result['independent_book_timestamp_available']
    assert not result['prospective_independence']


def test_naive_provider_clock_is_unknown_instead_of_fresh(db):
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    _, book = live_book(provider_timestamp='2026-10-14T10:00:00')
    db.insert_snapshot(sid, book)
    result = audit_intraday(db)
    assert result['timestamp_unknown_rows'] == 1 and result['observable_ask_rows'] == 0
    assert result['per_symbol'][0]['provider_age_p95_seconds'] is None


@pytest.mark.parametrize('problem', ['unit', 'future', 'fractional'])
def test_clock_tolerance_is_not_entry_evidence_and_old_bad_rows_remain_visible(db, problem):
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    clock, book = live_book()
    if problem == 'unit':
        book['price_unit'] = 'Toman'
    elif problem == 'future':
        book['provider_timestamp'] = (clock + timedelta(seconds=1)).isoformat()
    else:
        book['volume'] = 1000.5
    assert db.insert_snapshot(sid, book)  # ردیف پیشین حذف یا اصلاح حدسی نمی‌شود.
    before = db.conn.total_changes
    result = audit_intraday(db)
    assert db.conn.total_changes == before
    assert result['rows'] == 1 and result['observable_ask_rows'] == 0
    assert result['provider_fresh_fraction'] == 1
    assert result['provider_after_receive_rows'] == (1 if problem == 'future' else 0)
    assert result['unverified_price_unit_rows'] == (1 if problem == 'unit' else 0)
