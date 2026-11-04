"""Research instrument search is not an exhaustive historical membership source."""

import hashlib
import json

import pytest

from config import settings
from core.rights_discovery import VERSION, assess_search, search_request
from data_collector.tsetmc import TsetmcProvider
from ops import rights_discovery
from ops.rights_discovery import _read_attempt, capture, create_plan, review


def response(request, rows):
    body = json.dumps({'instrumentSearch': rows}, ensure_ascii=False).encode('utf-8')
    return {'version': VERSION, 'source': 'tsetmc', 'ins_code': request['parent_code'],
            'parent_symbol': request['parent_symbol'], 'query_symbol': request['query_symbol'],
            'url': request['url'], 'http_status': 200, 'content': body, 'response_bytes': len(body),
            'body_sha256': hashlib.sha256(body).hexdigest(), 'requested_at': '2024-01-01T10:00:00+03:30',
            'received_at': '2024-01-01T10:00:01+03:30'}


def test_search_keeps_dated_and_inactive_candidates_without_claiming_completeness():
    request = search_request('12', 'فخاس')
    data = response(request, [{'insCode': '100', 'lVal18AFC': 'فخاسح', 'lastDate': 0},
                              {'insCode': 200, 'lVal18AFC': 'فخاسح', 'lastDate': 1},
                              {'insCode': '300', 'lVal18AFC': 'فخاس'}])
    result = assess_search(request, data)
    assert result['candidate_codes'] == ['100', '200']
    assert result['status'] == 'observed_matches'
    assert result['historical_universe_complete'] is False and result['candidate_identity_verified'] is False


def test_search_no_match_is_not_proof_of_no_historical_rights():
    request = search_request('12', 'دالبر')
    result = assess_search(request, response(request, []))
    assert result['status'] == 'observed_no_match' and result['historical_universe_complete'] is False
    assert assess_search(request, None)['status'] == 'unavailable'


@pytest.mark.parametrize('code,symbol', [('0', 'فخاس'), ('01', 'فخاس'), ('1', 'فخاس/'),
                                          ('1', 'فخاس\n'), ('1', 'فخاس%'), (True, 'فخاس')])
def test_invalid_request_cannot_construct_a_source_url(code, symbol):
    with pytest.raises(ValueError):
        search_request(code, symbol)


def test_search_rejects_tampered_body_and_marks_bad_matching_rows():
    request = search_request('12', 'فخاس')
    observed = response(request, [{'insCode': None, 'lVal18AFC': 'فخاسح'}])
    assert assess_search(request, observed)['status'] == 'observed_with_invalid_rows'
    with pytest.raises(ValueError):
        assess_search(request, observed | {'body_sha256': 'bad'})
    with pytest.raises(ValueError):
        assess_search(request, observed | {'query_symbol': 'دالبرح'})


def test_provider_constructs_only_the_expected_bounded_public_search(monkeypatch):
    provider = TsetmcProvider()
    request = search_request('12', 'دالبر')
    calls = []
    def fetch(code, url, **kwargs):
        calls.append((code, url, kwargs))
        observed = response(request, [])
        return {key: value for key, value in observed.items() if key not in ('version', 'parent_symbol', 'query_symbol')}
    monkeypatch.setattr(provider, '_get_quote_context_json', fetch)
    observed = provider.get_research_rights_search_observation('12', 'دالبر')
    assert assess_search(request, observed)['status'] == 'observed_no_match'
    assert calls[0][0:2] == ('12', request['url'])
    assert calls[0][2]['max_bytes'] > 0 and calls[0][2]['validate_content'] is False


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    monkeypatch.setattr(settings, 'DB_PATH', root / 'brs.db')
    return root / 'private'


PARENTS = [{'parent_code': '12', 'parent_symbol': 'فخاس', 'type_code': 300, 'is_active': True},
           {'parent_code': '13', 'parent_symbol': 'دالبر', 'type_code': 300, 'is_active': False}]


class Provider:
    last_transport_evidence = None

    def __init__(self, *, fail=False):
        self.fail, self.calls = fail, []

    def get_research_rights_search_observation(self, code, symbol):
        self.calls.append((code, symbol))
        if self.fail:
            return None
        request = search_request(code, symbol)
        return response(request, [{'insCode': str(int(code) + 100), 'lVal18AFC': request['query_symbol']}])


def test_capture_is_resumable_and_does_not_change_existing_attempts(private_root):
    folder = create_plan(PARENTS, private_root / 'run')
    provider = Provider()
    assert capture(provider, folder, max_new=1)['stop_reason'] == 'batch_complete'
    assert len(provider.calls) == 1
    partial = review(folder)
    assert partial['status_counts'] == {'observed_matches': 1, 'not_attempted': 1}
    assert partial['historical_universe_complete'] is False
    assert capture(provider, folder, max_new=1)['new_attempts'] == 1
    assert len(provider.calls) == 2
    assert capture(provider, folder, max_new=1)['stop_reason'] == 'plan_processed'
    assert len(provider.calls) == 2
    assert len(list((folder / 'observations').glob('*/*.json'))) == 2
    assert review(folder)['unique_candidate_codes'] == 2


def test_failure_stops_and_never_automatically_retries(private_root):
    folder = create_plan(PARENTS, private_root / 'run')
    bad = Provider(fail=True)
    assert capture(bad, folder)['stop_reason'] == 'source_unavailable_or_invalid'
    assert len(bad.calls) == 1
    good = Provider()
    assert capture(good, folder)['stop_reason'] == 'prior_failed_attempt_requires_explicit_retry'
    assert not good.calls
    assert capture(good, folder, retry_failed=True)['stop_reason'] == 'plan_processed'
    assert len(good.calls) == 2
    assert len(list((folder / 'observations').glob('*/*.json'))) == 3


def test_changed_plan_tampered_receipt_and_parallel_lock_fail_closed(private_root):
    folder = create_plan(PARENTS, private_root / 'run')
    provider = Provider()
    capture(provider, folder, max_new=1)
    path = next((folder / 'observations').glob('*/*.json'))
    record = json.loads(path.read_text(encoding='utf-8'))
    request = record['request']
    record['body_base64'] = 'e30='
    path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError):
        _read_attempt(path, request, record['plan_sha256'])
    with pytest.raises(ValueError):
        capture(provider, folder)
    with pytest.raises(ValueError):
        review(folder)
    lock = folder / '.capture.lock'
    lock.write_text('previous run', encoding='utf-8')
    with pytest.raises(ValueError, match='lock exists'):
        capture(provider, folder)


def test_even_an_invalid_source_response_keeps_its_original_body(private_root):
    class InvalidProvider(Provider):
        def get_research_rights_search_observation(self, code, symbol):
            request = search_request(code, symbol)
            result = response(request, [])
            result['content'] = b'{}'
            result['response_bytes'] = 2
            result['body_sha256'] = hashlib.sha256(b'{}').hexdigest()
            return result

    folder = create_plan(PARENTS, private_root / 'run')
    assert capture(InvalidProvider(), folder)['stop_reason'] == 'source_unavailable_or_invalid'
    path = next((folder / 'observations').glob('*/*.json'))
    record = json.loads(path.read_text(encoding='utf-8'))
    record['body_base64'] = 'eyJ4IjoxfQ=='
    path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='bytes differ'):
        review(folder)


def test_new_plan_detects_capture_code_drift_before_request(private_root, monkeypatch):
    folder = create_plan(PARENTS, private_root / 'run')
    monkeypatch.setattr(rights_discovery, '_code_fingerprints', lambda names: {name: '0' * 64 for name in names})
    provider = Provider()
    result = capture(provider, folder)
    assert result['stop_reason'] == 'capture_code_changed' and not provider.calls
    assert review(folder)['current_code_matches_capture_plan'] is False
