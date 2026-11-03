"""Synthetic-only coverage for the public universe collector."""

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from core.codal_universe import assess_registration, registration_request
from core.timeutils import now_tehran_iso
from data_collector.codal import CodalProvider
from ops import codal_universe

START = '2024-01-01'
END = '2024-04-01'
CUTOFF = '2025-03-21'


def row(number=1, **changes):
    value = dict(TracingNo=number, Symbol='آزمون', CompanyName='ناشر آزمون', Title='آگهی آزمونی',
                 LetterCode='ن-۶۷', PublishDateTime='1402/12/23 12:00:00',
                 SentDateTime='1402/12/23 11:59:59', IsEstimate=False,
                 Url='/Reports/Decision.aspx?LetterSerial=test', PdfUrl='', AttachmentUrl='')
    value.update(changes)
    return value


def observed(page=1, *, rows=None, total=2, native_pages=2, attacker=False):
    body = json.dumps(dict(IsAttacker=attacker, Letters=[row(page)] if rows is None else rows,
                           Page=native_pages, Total=total), ensure_ascii=False).encode()
    return dict(source='codal', url=registration_request(start=START, end_exclusive=END,
                cutoff=CUTOFF, page=page), requested_at=now_tehran_iso(), received_at=now_tehran_iso(),
                response_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest(), content=body)


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get_registration_universe_observation(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


@pytest.fixture
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(codal_universe.settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(codal_universe.settings, 'RATE_LIMIT_DELAY', 0)
    monkeypatch.setitem(codal_universe.settings.RESEARCH_CONFIG, 'holdout_start', CUTOFF)
    return tmp_path


def test_request_has_no_symbol_and_works_before_2012():
    url = registration_request(start='2000-01-01', end_exclusive='2000-02-01', cutoff=CUTOFF, page=2)
    query = parse_qs(urlsplit(url).query)
    assert 'Symbol' not in query and query['LetterType'] == ['28'] and query['PageNumber'] == ['2']


def test_post_holdout_requires_explicit_descriptive_mode(private_data):
    with pytest.raises(ValueError):
        codal_universe.create(start='2025-03-21', end_exclusive='2025-04-01')
    folder = codal_universe.create(start='2025-03-21', end_exclusive='2025-04-01', descriptive=True)
    state = codal_universe.review(folder)
    assert state['plan']['mode'] == 'descriptive_retrospective'
    assert state['independent_holdout_for_this_hypothesis'] is False
    with pytest.raises(ValueError):
        codal_universe.create(start='2099-01-01', end_exclusive='2099-02-01', descriptive=True)


def test_provider_rejects_post_holdout_without_opt_in_before_network(private_data):
    class NoNetwork:
        headers = {}

        def get(self, *_args, **_kwargs):
            pytest.fail('forbidden request reached the network')

    provider = CodalProvider(session=NoNetwork())
    with pytest.raises(ValueError):
        provider.get_registration_universe_observation(start='2025-03-21', end_exclusive='2025-04-01')
    with pytest.raises(ValueError):
        provider.get_registration_universe_observation(start=START, end_exclusive=END, descriptive='yes')
    with pytest.raises(ValueError):
        provider.get_registration_universe_observation(start='2099-01-01', end_exclusive='2099-02-01',
                                                       descriptive=True)


@pytest.mark.parametrize('changes', [{'page': True}, {'page': 101}, {'page': 0},
                                    {'start': 'bad'}, {'end_exclusive': '2025-03-22'}])
def test_bad_or_holdout_request_rejected(changes):
    values = dict(start=START, end_exclusive=END, cutoff=CUTOFF, page=1)
    values.update(changes)
    with pytest.raises(ValueError):
        registration_request(**values)


def test_assess_preserves_unknown_status_and_validates_hash():
    obs = observed()
    body = obs.pop('content')
    result = assess_registration(body, obs, start=START, end_exclusive=END, cutoff=CUTOFF, page=1)
    assert result['letters'][0]['capital_change_verified'] is False
    obs['body_sha256'] = '0'*64
    with pytest.raises(ValueError):
        assess_registration(body, obs, start=START, end_exclusive=END, cutoff=CUTOFF, page=1)


@pytest.mark.parametrize('payload', [
    observed(rows=[row(PublishDateTime='1404/01/01 10:00:00')]),
    observed(rows=[row(TracingNo=5), row(TracingNo=5)]),
    observed(rows=[row(Symbol='')]),
])
def test_rejects_sealed_invalid_and_duplicate_rows(payload):
    body = payload.pop('content')
    with pytest.raises(ValueError):
        assess_registration(body, payload, start=START, end_exclusive=END, cutoff=CUTOFF, page=1)


def test_resumes_distinct_pages_and_capacity_does_not_claim_completeness(private_data):
    folder = codal_universe.create(start=START, end_exclusive=END, max_pages=2)
    p = Provider([observed(1), observed(2)])
    state = codal_universe.collect(folder, p, max_new=1)
    assert state['pages_attempted'] == 1 and state['stop_reason'] is None
    state = codal_universe.collect(folder, p, max_new=1)
    assert state['document_rows_observed'] == 2 and state['stop_reason'] == 'page_capacity_reached'
    assert state['legal_event_count'] is None and state['historical_coverage_verified'] is False
    assert codal_universe.collect(folder, p)['pages_attempted'] == 2 and len(p.calls) == 2


@pytest.mark.parametrize('response,reason', [(None, 'unavailable'),
                                              (observed(attacker=True), 'verification_required')])
def test_failure_is_archived_and_not_retried(private_data, response, reason):
    folder = codal_universe.create(start=START, end_exclusive=END, max_pages=2)
    p = Provider([response])
    state = codal_universe.collect(folder, p, max_new=2)
    assert state['stop_reason'] == reason and len(p.calls) == 1
    assert codal_universe.collect(folder, p)['stop_reason'] == reason and len(p.calls) == 1


def test_orphan_body_and_tampering_block_resume(private_data):
    folder = codal_universe.create(start=START, end_exclusive=END)
    p = Provider([observed()])
    codal_universe.collect(folder, p)
    (folder/'page-001.bin').write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        codal_universe.collect(folder, p)
    other = codal_universe.create(start=START, end_exclusive=END)
    (other/'page-001.bin').write_bytes(b'interrupted')
    with pytest.raises(ValueError):
        codal_universe.review(other)


def test_cross_page_duplicate_stops_before_next_request(private_data):
    folder = codal_universe.create(start=START, end_exclusive=END)
    p = Provider([observed(1), observed(2, rows=[row(1)])])
    state = codal_universe.collect(folder, p, max_new=3)
    assert state['stop_reason'] == 'pagination_conflict'
    assert (folder/'page-002.bin').is_file()
    assert codal_universe.review(folder)['stop_reason'] == 'pagination_conflict'
    assert len(p.calls) == 2


def test_early_empty_page_is_not_treated_as_success(private_data):
    folder = codal_universe.create(start=START, end_exclusive=END)
    p = Provider([observed(1), observed(2, rows=[])])
    state = codal_universe.collect(folder, p, max_new=3)
    assert state['stop_reason'] == 'pagination_conflict'
    assert state['document_rows_observed'] == 1
    assert len(p.calls) == 2
