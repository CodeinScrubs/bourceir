"""Only temporary synthetic source rows are used here."""

import hashlib
import json

import pytest

from config import settings
from core.rights_discovery import VERSION as SEARCH_VERSION
from core.rights_discovery import search_request
from core.rights_history import assess_history, candidate_starts
from ops.rights_discovery import capture as capture_search
from ops.rights_discovery import create_plan as create_search_plan
from ops.rights_history import capture, create_plan, review


def observation(rows, *, code='12', start='2024-01-01', end='2025-03-21'):
    payload = {'closingPriceDaily': rows}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'source': 'tsetmc', 'price_unit': 'Rial', 'ins_code': code,
            'url': f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{code}/0',
            'start_date': start, 'end_date_exclusive': end,
            'http_status': 200, 'response_bytes': 100, 'body_sha256': 'a' * 64,
            'response_scope': 'selected_native_rows_pre_holdout', 'source_body_archived': False,
            'unselected_numeric_rows_not_archived': True, 'selected_row_count': len(rows),
            'selected_payload_sha256': hashlib.sha256(canonical).hexdigest(), 'payload': payload,
            'requested_at': '2026-01-01T10:00:00+03:30', 'received_at': '2026-01-01T10:00:01+03:30'}


def evaluate(value):
    return assess_history('12', '2024-01-01', '2025-03-21', value)


def traded(day, code='12', volume=1):
    return {'dEven': day, 'insCode': code, 'qTotTran5J': volume, 'priceFirst': 100,
            'priceMin': 90, 'priceMax': 110, 'pDrCotVal': 105, 'priceYesterday': 100,
            'zTotTran': 1, 'qTotCap': volume * 100}


def test_history_counts_zero_and_positive_rows_without_claiming_completeness():
    value = observation([{'dEven': 20240101, 'insCode': '12', 'qTotTran5J': 0, 'zTotTran': 0, 'qTotCap': 0},
                         traded(20240102, volume=12)])
    result = evaluate(value)
    assert result['status'] == 'observed' and result['positive_volume_rows'] == 1
    assert result['zero_volume_rows'] == 1 and result['historical_completeness_verified'] is False
    assert result['original_source_body_archived'] is False


def test_duplicate_date_and_bad_volume_stay_explicitly_unknown():
    value = observation([traded(20240101),
                         {'dEven': 20240101, 'insCode': '12', 'qTotTran5J': None}])
    result = evaluate(value)
    assert result['status'] == 'observed_with_gaps'
    assert result['duplicate_dates'] == 1 and result['invalid_volume_rows'] == 1


def test_mutated_digest_or_row_identity_and_holdout_are_rejected():
    row = traded(20240101, volume=5)
    value = observation([row])
    value['payload']['closingPriceDaily'][0]['qTotTran5J'] = 6
    with pytest.raises(ValueError):
        evaluate(value)
    with pytest.raises(ValueError):
        evaluate(observation([row | {'insCode': '13'}]))
    with pytest.raises(ValueError):
        evaluate(observation([row | {'dEven': 20250321}]))
    sealed = observation([row])
    sealed['payload']['closingPriceDaily'][0] = {'dEven': 20250321, 'insCode': '12', 'qTotTran5J': float('nan')}
    with pytest.raises(ValueError, match='out-of-scope date'):
        evaluate(sealed)
    with pytest.raises(ValueError):
        assess_history('12', '2024-01-01', '2025-03-22', None)


def test_selected_row_cannot_postdate_receipt_in_tehran_before_numbers_are_read():
    record = observation([traded(20240102)])
    # One minute before midnight in Tehran, although the requested row is tomorrow.
    record['requested_at'] = '2024-01-01T20:28:00+00:00'
    record['received_at'] = '2024-01-01T20:29:00+00:00'
    record['payload']['closingPriceDaily'][0]['qTotTran5J'] = object()
    with pytest.raises(ValueError, match='after the Tehran receipt date'):
        evaluate(record)


def test_selected_row_on_tehran_receipt_date_allowed_even_if_utc_or_request_is_previous_day():
    record = observation([traded(20240102)])
    record['requested_at'] = '2024-01-01T20:29:00+00:00'
    record['received_at'] = '2024-01-01T20:30:00+00:00'  # 00:00 Tehran, next date.
    assert evaluate(record)['status'] == 'observed'


def test_boolean_native_date_cannot_be_treated_as_an_integer():
    record = observation([traded(20240102)])
    record['payload']['closingPriceDaily'][0]['dEven'] = True
    with pytest.raises(ValueError, match='native date'):
        evaluate(record)


def test_missing_source_is_not_an_empty_price_history():
    assert evaluate(None)['status'] == 'unavailable'
    assert evaluate(observation([]))['status'] == 'observed'


def test_positive_volume_with_missing_or_out_of_range_first_price_is_quarantined():
    result = evaluate(observation([traded(20240101) | {'priceFirst': 0}]))
    assert result['status'] == 'observed_with_gaps' and result['invalid_traded_rows'] == 1
    zero = {'dEven': 20240102, 'insCode': '12', 'qTotTran5J': 0, 'zTotTran': 1, 'qTotCap': 0}
    assert evaluate(observation([zero]))['inconsistent_zero_volume_rows'] == 1


def test_gap_candidates_keep_first_observation_censored_and_do_not_prove_an_issue():
    rows = [traded(20240101), traded(20240201), traded(20240701)]
    result = candidate_starts('12', '2024-01-01', '2025-03-21', observation(rows))
    assert [item['date'] for item in result['candidate_starts']] == ['2024-01-01', '2024-07-01']
    assert result['candidate_starts'][0]['left_censored'] is True
    assert result['candidate_starts'][1]['gap_calendar_days'] == 151
    assert result['issue_identity_verified'] is False and result['calendar_completeness_verified'] is False
    ambiguous = observation(rows + [traded(20240201)])
    assert candidate_starts('12', '2024-01-01', '2025-03-21', ambiguous)['status'] == 'unusable_for_gap_discovery'


@pytest.fixture
def discovery(private_history_root):
    parents = [{'parent_code': '12', 'parent_symbol': 'فخاس', 'type_code': 300, 'is_active': True},
               {'parent_code': '13', 'parent_symbol': 'دالبر', 'type_code': 300, 'is_active': True}]

    class SearchProvider:
        last_transport_evidence = None
        def get_research_rights_search_observation(self, code, symbol):
            request = search_request(code, symbol)
            body = json.dumps({'instrumentSearch': [{'insCode': '99', 'lVal18AFC': request['query_symbol']}]},
                              ensure_ascii=False).encode('utf-8')
            return {'version': SEARCH_VERSION, 'source': 'tsetmc', 'ins_code': code,
                    'parent_symbol': symbol, 'query_symbol': request['query_symbol'], 'url': request['url'],
                    'http_status': 200, 'content': body, 'response_bytes': len(body),
                    'body_sha256': hashlib.sha256(body).hexdigest(),
                    'requested_at': '2024-01-01T10:00:00+03:30', 'received_at': '2024-01-01T10:00:01+03:30'}

    folder = create_search_plan(parents, private_history_root / 'search')
    capture_search(SearchProvider(), folder)
    return folder


@pytest.fixture
def private_history_root(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    monkeypatch.setattr(settings, 'DB_PATH', root / 'brs.db')
    return root / 'private'


class HistoryProvider:
    last_transport_evidence = None
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []
    def get_development_history_observation(self, code, start, end):
        self.calls.append((code, start, end))
        if self.fail:
            return None
        return observation([traded(20240101, code=code)],
                           code=code, start=start, end=end)


def test_history_capture_preserves_ambiguous_parent_links_and_replays(discovery, private_history_root):
    folder = create_plan(discovery, private_history_root / 'history')
    initial = review(folder)
    assert initial['candidate_count'] == 1 and initial['status_counts'] == {'not_attempted': 1}
    assert len(initial['rows'][0]['candidate']['parents']) == 2
    assert initial['historical_instrument_identity_verified'] is False
    provider = HistoryProvider()
    assert capture(provider, folder)['stop_reason'] == 'plan_processed'
    checked = review(folder)
    assert checked['status_counts'] == {'observed': 1}
    assert checked['candidate_observed_starts'] == 1 and checked['left_censored_first_starts'] == 1
    assert len(provider.calls) == 1
    assert capture(provider, folder)['new_attempts'] == 0
    assert len(provider.calls) == 1
    assert review(folder)['original_source_bodies_archived'] is False


def test_history_capture_retains_failure_and_needs_explicit_retry(discovery, private_history_root):
    folder = create_plan(discovery, private_history_root / 'history')
    assert capture(HistoryProvider(fail=True), folder)['stop_reason'] == 'source_unavailable_or_invalid'
    provider = HistoryProvider()
    assert capture(provider, folder)['stop_reason'] == 'prior_failed_attempt_requires_explicit_retry'
    assert not provider.calls
    assert capture(provider, folder, retry_failed=True)['stop_reason'] == 'plan_processed'
    path = next((folder / 'observations').glob('*/0002.json'))
    item = json.loads(path.read_text(encoding='utf-8'))
    item['observation']['payload']['closingPriceDaily'][0]['qTotTran5J'] = 7
    path.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError):
        review(folder)
