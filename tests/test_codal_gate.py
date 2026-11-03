"""Offline tests for persistent public CODAL rate-limit admission."""

import json

import pytest
import requests

from data_collector import codal_gate
from data_collector.codal_gate import CodalGate, import_diagnostic, record_manual_review, source_status
from data_collector.source_cooldown import SourceDeferred
from ops.codal_universe import create, review
from ops.codal_universe_guarded import guarded_collect

URL = ('https://search.codal.ir/api/search/v2/q?FromDate=1398%2F01%2F01&ToDate=1399%2F01%2F01'
       '&LetterType=28&PageNumber=9')


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(codal_gate.settings, 'DATA_DIR', tmp_path)
    path = tmp_path/'private/codal-registration-chains/CHAIN-test/receipt.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(url=URL, diagnostic_only=True, requested_at='2020-01-01T10:00:00+03:30',
                                    received_at='2020-01-01T10:00:01+03:30', http_status=429,
                                    content_type='text/html', retry_after=None, redirected=False)), encoding='utf-8')
    return path


class Response:
    def __init__(self, status=429, retry_after=None, body=b''):
        self.status_code = status
        self.headers = {'Content-Type': 'text/html'}
        self.url, self.body = URL, body
        if retry_after is not None:
            self.headers['Retry-After'] = retry_after
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=65536):
        yield self.body


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.headers = {}

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


def test_uninitialized_gate_is_blocked_without_network(root):
    session = Session(Response())
    with pytest.raises(SourceDeferred):
        CodalGate().get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert source_status()['blocked'] and session.calls == 0


@pytest.mark.parametrize('options', [{}, {'stream': False}, {'stream': 1}])
def test_direct_caller_must_explicitly_enable_streaming_before_network(root, options):
    session = Session(Response())
    with pytest.raises(ValueError, match='streaming'):
        CodalGate().get(session, URL, timeout=10, allow_redirects=False, **options)
    assert session.calls == 0


def test_import_is_durable_and_review_needs_explicit_time_and_basis(root):
    state = import_diagnostic(root)
    assert state['blocked'] and len(state['blocker_receipts']) == 1
    with pytest.raises(ValueError):
        record_manual_review(not_before='2020-01-01T10:00:01+03:30', review_basis='source reviewed offline')
    with pytest.raises(ValueError):
        record_manual_review(not_before='2020-01-01T10:00:02+03:30', review_basis='short')
    released = record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                                    review_basis='operator checked source access rules')
    assert released['blocked'] is False
    assert (root.parents[2]/'source-health/codal'/state['receipt']).is_file()


def test_later_429_blocks_new_gate_instances_and_preserves_retry_after(root):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')
    session = Session(Response(retry_after='3600'))
    gate = CodalGate()
    assert gate.get(session, URL, timeout=10, allow_redirects=False, stream=True).status_code == 429
    assert gate.last_evidence['status'] == 'limited' and source_status()['blocked']
    with pytest.raises(SourceDeferred):
        CodalGate().get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert session.calls == 1
    receipt = root.parents[2]/'source-health/codal'/gate.last_evidence['blocker_receipt']
    assert json.loads(receipt.read_text(encoding='utf-8'))['retry_after_seconds'] == 3600


def test_verification_json_blocks_gate_without_another_network_read(root):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')
    raw = json.dumps(dict(IsAttacker=True, Letters=[], Page=0, Total=0)).encode()
    response = Response(status=200, body=raw)
    response.headers['Content-Type'] = 'application/json'
    session = Session(response)
    gate = CodalGate()
    buffered = gate.get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert b''.join(buffered.iter_content()) == raw
    assert response.closed and gate.last_evidence['status'] == 'verification_required'
    receipt = root.parents[2]/'source-health/codal'/gate.last_evidence['blocker_receipt']
    assert json.loads(receipt.read_text(encoding='utf-8'))['origin'] == 'verification_required'
    with pytest.raises(SourceDeferred):
        CodalGate().get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert session.calls == 1


def test_guarded_collector_persists_verification_and_stops_all_new_sessions(root, monkeypatch):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')
    folder = create(start='2024-01-01', end_exclusive='2024-04-01')
    raw = json.dumps(dict(IsAttacker=True, Letters=[], Page=0, Total=0)).encode()
    response = Response(status=200, body=raw)
    response.headers['Content-Type'] = 'application/json'

    class Retry:
        def is_retry(self, *_args, **_kwargs):
            return False

    class Adapter:
        max_retries = Retry()

    class FakeHttpSession:
        def __init__(self):
            self.headers = {}

        def get_adapter(self, _url):
            return Adapter()

        def get(self, url, **_options):
            response.url = url
            return response

        def close(self):
            pass

    monkeypatch.setattr(codal_gate.requests, 'Session', FakeHttpSession)
    result = guarded_collect(folder)
    assert result['result'] == 'verification_required' and result['source_attempted']
    assert result['gate_after']['blocked']
    assert review(folder)['stop_reason'] == 'verification_required'


@pytest.mark.parametrize('failure', [requests.ConnectionError, requests.Timeout])
def test_guarded_collector_records_attempt_when_transport_fails(root, monkeypatch, failure):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')
    folder = create(start='2024-01-01', end_exclusive='2024-04-01')

    class Retry:
        def is_retry(self, *_args, **_kwargs):
            return False

    class Adapter:
        max_retries = Retry()

    class BrokenSession:
        def __init__(self):
            self.headers = {}

        def get_adapter(self, _url):
            return Adapter()

        def get(self, _url, **_options):
            raise failure('do not retain transport details')

        def close(self):
            pass

    monkeypatch.setattr(codal_gate.requests, 'Session', BrokenSession)
    result = guarded_collect(folder)
    assert result['result'] == 'unavailable'
    assert result['source_attempted'] is True
    assert result['transport']['status'] == 'transport_error'
    assert result['transport']['error_type'] == failure.__name__
    assert result['transport']['http_status'] is None
    assert 'do not retain transport details' not in json.dumps(result)
    assert review(folder)['stop_reason'] == 'unavailable'


@pytest.mark.parametrize(('status', 'content_type', 'body', 'url', 'reason'), [
    (200, 'text/html', b'<html>verification</html>', URL, 'unexpected_content_type'),
    (302, 'text/html', b'', URL, 'unexpected_http_status'),
    (403, 'text/html', b'', URL, 'unexpected_http_status'),
    (200, 'application/json', b'{', URL, 'invalid_or_ambiguous_verification_json'),
    (200, 'application/json', b'{"IsAttacker":false,"IsAttacker":true}', URL,
     'invalid_or_ambiguous_verification_json'),
    (200, 'application/json', b'{"IsAttacker":null}', URL, 'invalid_or_ambiguous_verification_json'),
    (200, 'application/json', b'{"IsAttacker":false}', URL, 'invalid_response_structure'),
    (200, 'application/json', b'{"IsAttacker":false,"Letters":[],"Page":true,"Total":0}', URL,
     'invalid_response_structure'),
    (200, 'application/json', b'{"IsAttacker":false}', 'https://search.codal.ir/other',
     'unexpected_response_url'),
])
def test_ambiguous_response_persistently_blocks_future_requests(root, status, content_type, body, url, reason):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')
    response = Response(status=status, body=body)
    response.url = url
    response.headers['Content-Type'] = content_type
    session = Session(response)
    gate = CodalGate()
    gate.get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert gate.last_evidence['reason'] == reason
    receipt = root.parents[2]/'source-health/codal'/gate.last_evidence['blocker_receipt']
    observed = json.loads(receipt.read_text(encoding='utf-8'))
    assert observed['http_status'] == status and observed['content_type'] == content_type
    with pytest.raises(SourceDeferred):
        CodalGate().get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert session.calls == 1


def test_incomplete_json_body_blocks_before_next_request(root):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')

    class BrokenResponse(Response):
        def iter_content(self, chunk_size=65536):
            raise OSError('synthetic interrupted read')

    response = BrokenResponse(status=200)
    response.headers['Content-Type'] = 'application/json'
    session = Session(response)
    gate = CodalGate()
    with pytest.raises(OSError):
        gate.get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert response.closed and gate.last_evidence['reason'] == 'body_read_or_validation_failed'
    with pytest.raises(SourceDeferred):
        CodalGate().get(session, URL, timeout=10, allow_redirects=False, stream=True)
    assert session.calls == 1


def test_collector_rejected_row_also_creates_shared_blocker(root, monkeypatch):
    import_diagnostic(root)
    record_manual_review(not_before='2020-01-01T10:00:02+03:30',
                         review_basis='operator checked source access rules')
    folder = create(start='2024-01-01', end_exclusive='2024-04-01')
    raw = json.dumps(dict(IsAttacker=False, Letters=[{}], Page=1, Total=1)).encode()
    response = Response(status=200, body=raw)
    response.headers['Content-Type'] = 'application/json'

    class Retry:
        def is_retry(self, *_args, **_kwargs):
            return False

    class Adapter:
        max_retries = Retry()

    class FakeHttpSession:
        def __init__(self):
            self.headers = {}

        def get_adapter(self, _url):
            return Adapter()

        def get(self, url, **_options):
            response.url = url
            return response

        def close(self):
            pass

    monkeypatch.setattr(codal_gate.requests, 'Session', FakeHttpSession)
    result = guarded_collect(folder)
    assert result['result'] == 'invalid_response' and result['gate_after']['blocked']
    assert result['transport']['status'] == 'invalid_response'


def test_rejects_wrong_origin_and_redirects(root):
    session = Session(Response())
    for url, options in [('https://example.com/', {'timeout': 10, 'allow_redirects': False}),
                         (URL, {'timeout': 10, 'allow_redirects': True})]:
        with pytest.raises(ValueError):
            CodalGate().get(session, url, **options)
    assert session.calls == 0


def test_rejected_call_does_not_reuse_prior_transport_evidence(root):
    session = Session(Response())
    gate = CodalGate()
    gate.last_evidence = {'status': 'response_received', 'http_status': 200}
    with pytest.raises(ValueError):
        gate.get(session, 'https://example.com/', timeout=10, allow_redirects=False, stream=True)
    assert gate.last_evidence is None
    assert session.calls == 0


def test_corrupt_persisted_receipt_blocks_before_network(root):
    state = import_diagnostic(root)
    path = root.parents[2]/'source-health/codal'/state['receipt']
    path.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='hash mismatch'):
        source_status()


def test_guarded_collect_does_not_consume_or_modify_archive_while_blocked(root):
    import_diagnostic(root)
    folder = create(start='2024-01-01', end_exclusive='2024-04-01')
    before = review(folder)
    result = guarded_collect(folder)
    assert result['result'] == 'source_blocked' and result['source_attempted'] is False
    assert review(folder) == before
