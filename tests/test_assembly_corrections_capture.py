"""The collection budget and human challenge must never hide a later revision."""

import pytest

from config import settings
from ops import assembly_corrections as collector
from tests.test_assembly_corrections import document


def letters():
    return [dict(**r['notice'], source_published_at=r['notice']['published_at'],
                 url='https://codal.ir/Reports/Decision.aspx?LetterSerial=' + str(i))
            for i, r in enumerate([document(1, amount='0'), document(2, 1, '188'), document(3, 2, '188')], 1)]


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    rows = {str(i): r for i, r in enumerate(
        [document(1, amount='0'), document(2, 1, '188'), document(3, 2, '188')], 1)}
    called = []

    def fetch(url, **_):
        key = url.rsplit('=', 1)[-1]
        called.append(key)
        return dict(status='captured', format='text', folder=key)

    monkeypatch.setattr(collector, 'capture_codal_document', fetch)
    monkeypatch.setattr(collector, 'read_codal_document_file', lambda key, _: rows[key]['text'].encode('utf-8'))
    return called


def test_all_three_documents_are_captured_without_the_old_two_document_slice(capture):
    result = collector.capture_assembly_corrections(letters(), end_exclusive='2025-03-21')
    assert capture == ['1', '2', '3']
    assert result['expected_notices'] == result['requests'] == 3
    assert result['resolution']['groups'][0]['dps_rial'] == '188'


def test_budget_limit_preserves_pending_later_version_as_unresolved(capture):
    result = collector.capture_assembly_corrections(letters(), end_exclusive='2025-03-21', document_budget=2)
    assert result['requests'] == 2 and result['expected_notices'] == 3 and result['unattempted'] == 1
    assert result['resolution']['status'] == 'unresolved'
    assert all(g['dps_rial'] is None for g in result['resolution']['groups'])


def test_challenge_keeps_later_inventory_and_stops_further_requests(capture, monkeypatch):
    monkeypatch.setattr(collector, 'capture_codal_document', lambda *_, **__: dict(status='verification_required'))
    result = collector.capture_assembly_corrections(letters(), end_exclusive='2025-03-21')
    assert result['requests'] == 1 and result['paused_for_verification'] and result['expected_notices'] == 3
    assert result['resolution']['status'] == 'unresolved'


def test_discovery_conflict_is_rejected_before_any_request(capture):
    supplied = letters()
    supplied.append(dict(supplied[0], title=supplied[0]['title'] + ' تعارض'))
    with pytest.raises(ValueError):
        collector.capture_assembly_corrections(supplied, end_exclusive='2025-03-21')
    assert not capture


def test_post_cutoff_discovery_is_rejected_before_retrieval(capture):
    with pytest.raises(ValueError, match='sealed'):
        collector.capture_assembly_corrections(letters(), end_exclusive='2023-07-21')
    assert not capture
