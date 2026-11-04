"""Retrospective inventory tests use only synthetic temporary market data."""

import hashlib
import json
from pathlib import Path

import pytest

from config import settings
from core.rights_discovery import VERSION as SEARCH_VERSION
from core.rights_discovery import search_request
from core.timeutils import now_tehran_iso
from ops.rights_cohort_inventory import create_inventory, review_inventory
from ops.rights_discovery import capture as capture_search
from ops.rights_discovery import create_plan as create_search_plan
from ops.rights_discovery import review as review_search
from ops.rights_history import capture, create_plan
from tests.test_rights_history_research import observation, traded
from tests.test_rights_history_research import private_history_root as private_history_root


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
            stamp = now_tehran_iso()
            return {'version': SEARCH_VERSION, 'source': 'tsetmc', 'ins_code': code,
                    'parent_symbol': symbol, 'query_symbol': request['query_symbol'], 'url': request['url'],
                    'http_status': 200, 'content': body, 'response_bytes': len(body),
                    'body_sha256': hashlib.sha256(body).hexdigest(), 'requested_at': stamp, 'received_at': stamp}

    folder = create_search_plan(parents, private_history_root / 'search')
    capture_search(SearchProvider(), folder)
    return folder


class HistoryProvider:
    last_transport_evidence = None

    def __init__(self, fail=False):
        self.fail = fail

    def get_development_history_observation(self, code, start, end):
        if self.fail:
            return None
        stamp = now_tehran_iso()
        return observation([traded(20240101, code=code)], code=code, start=start, end=end) | {
            'requested_at': stamp, 'received_at': stamp}


@pytest.fixture
def history(discovery, private_history_root):
    folder = create_plan(discovery, private_history_root / 'history')
    capture(HistoryProvider(), folder)
    return folder


def _payload(record):
    return json.loads((Path(record['folder']) / 'inventory.json').read_text(encoding='utf-8'))


def test_inventory_keeps_ambiguous_parents_and_censored_start_without_prices(history, private_history_root):
    result = create_inventory(history, root=private_history_root / 'inventories')
    payload = _payload(result)
    inventory = payload['inventory']
    assert result['candidate_count'] == result['observed_candidate_start_count'] == 1
    row = inventory['candidate_rows'][0]
    start = inventory['candidate_starts'][0]
    assert len(row['parents']) == 2 and row['parent_ambiguous']
    assert start['left_censored'] and start['date'] == '2024-01-01'
    assert not start['issue_identity_verified'] and not start['initial_trading_window_verified']
    assert inventory['retrospective_after_prior_data_exposure']
    assert not inventory['independent_blind_preregistration']
    assert not inventory['historical_universe_complete'] and not inventory['accepted_for_signals']
    raw = json.dumps(payload, ensure_ascii=False)
    assert 'priceFirst' not in raw and 'closingPriceDaily' not in raw and 'net_ret' not in raw
    assert review_inventory(result['folder'])['status'] == 'reviewed'


def test_unattempted_candidate_is_preserved_not_treated_as_zero_or_absent(discovery, private_history_root):
    history = create_plan(discovery, private_history_root / 'history')
    result = create_inventory(history, root=private_history_root / 'inventories')
    inventory = _payload(result)['inventory']
    assert inventory['history_status_counts'] == {'not_attempted': 1}
    assert inventory['candidate_rows'][0]['history_quality'] is None
    assert inventory['candidate_rows'][0]['gap_discovery_status'] is None
    assert inventory['candidate_rows'][0]['observed_start_count'] is None
    assert inventory['candidate_starts'] == []


def test_failed_source_is_preserved_in_denominator(discovery, private_history_root):
    history = create_plan(discovery, private_history_root / 'history')
    capture(HistoryProvider(fail=True), history)
    result = create_inventory(history, root=private_history_root / 'inventories')
    inventory = _payload(result)['inventory']
    assert inventory['candidate_count'] == 1
    assert inventory['history_status_counts'] == {'unavailable': 1}
    assert inventory['candidate_rows'][0]['attempt_count'] == 1
    assert inventory['candidate_starts'] == []


def test_discovery_no_match_parent_remains_visible(private_history_root):
    parents = [{'parent_code': '12', 'parent_symbol': 'فخاس', 'type_code': 300, 'is_active': True},
               {'parent_code': '13', 'parent_symbol': 'دالبر', 'type_code': 300, 'is_active': False}]

    class SearchProvider:
        last_transport_evidence = None

        def get_research_rights_search_observation(self, code, symbol):
            request = search_request(code, symbol)
            matches = [{'insCode': '99', 'lVal18AFC': request['query_symbol']}] if code == '12' else []
            body = json.dumps({'instrumentSearch': matches}, ensure_ascii=False).encode('utf-8')
            stamp = now_tehran_iso()
            return {'version': SEARCH_VERSION, 'source': 'tsetmc', 'ins_code': code,
                    'parent_symbol': symbol, 'query_symbol': request['query_symbol'], 'url': request['url'],
                    'http_status': 200, 'content': body, 'response_bytes': len(body),
                    'body_sha256': hashlib.sha256(body).hexdigest(), 'requested_at': stamp, 'received_at': stamp}

    search = create_search_plan(parents, private_history_root / 'search')
    capture_search(SearchProvider(), search)
    history = create_plan(search, private_history_root / 'history')
    result = create_inventory(history, root=private_history_root / 'inventories')
    inventory = _payload(result)['inventory']
    assert inventory['source_parent_count'] == 2 and inventory['candidate_count'] == 1
    assert [row['status'] for row in inventory['source_parent_rows']] == ['observed_matches', 'observed_no_match']
    assert inventory['source_parent_rows'][1]['parent']['is_active'] is False
    assert not inventory['historical_universe_complete']


def test_unknown_volume_makes_gap_count_unknown_not_zero(discovery, private_history_root):
    class BadVolumeProvider:
        last_transport_evidence = None

        def get_development_history_observation(self, code, start, end):
            stamp = now_tehran_iso()
            return observation([traded(20240101, code=code) | {'qTotTran5J': None}],
                               code=code, start=start, end=end) | {'requested_at': stamp, 'received_at': stamp}

    history = create_plan(discovery, private_history_root / 'history')
    capture(BadVolumeProvider(), history)
    result = create_inventory(history, root=private_history_root / 'inventories')
    row = _payload(result)['inventory']['candidate_rows'][0]
    assert row['history_status'] == 'observed_with_gaps'
    assert row['gap_discovery_status'] == 'unusable_for_gap_discovery'
    assert row['observed_start_count'] is None


def test_inventory_rejects_source_changes_after_capture(history, private_history_root):
    result = create_inventory(history, root=private_history_root / 'inventories')
    source = next((history / 'observations').glob('*/*.json'))
    source.write_bytes(source.read_bytes() + b' ')
    with pytest.raises(ValueError):
        review_inventory(result['folder'])


def test_rehashed_inventory_cannot_promote_completeness_or_drop_starts(history, private_history_root):
    result = create_inventory(history, root=private_history_root / 'inventories')
    folder = Path(result['folder'])
    path = folder / 'inventory.json'
    payload = _payload(result)
    payload['inventory']['historical_universe_complete'] = True
    payload['inventory']['candidate_starts'] = []
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    path.write_bytes(body)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['inventory_sha256'] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='does not replay'):
        review_inventory(folder)


@pytest.mark.parametrize('dependency', ['core/source_cooldown.py', 'core/daily_publications.py'])
def test_changed_transitive_transport_validator_invalidates_replay(history, private_history_root, monkeypatch, dependency):
    from ops import rights_cohort_inventory as module

    result = create_inventory(history, root=private_history_root / 'inventories')
    assert dependency in _payload(result)['code_sha256']
    original = module._code_fingerprints
    monkeypatch.setattr(module, '_code_fingerprints', lambda names: original(names) | {dependency: 'f' * 64})
    with pytest.raises(ValueError, match='code fingerprint'):
        review_inventory(result['folder'])


def test_public_destination_or_source_overlap_is_rejected_before_output(history, tmp_path, private_history_root):
    with pytest.raises(ValueError):
        create_inventory(history, root=tmp_path / 'public')
    with pytest.raises(ValueError):
        create_inventory(history, root=history / 'inside')
    assert not (history / 'inside').exists()


def test_destination_cannot_mutate_the_discovery_archive(history, discovery):
    with pytest.raises(ValueError):
        create_inventory(history, root=discovery / 'inside')
    assert not (discovery / 'inside').exists()


def test_missing_or_changed_source_cannot_be_presented_as_complete(history, private_history_root):
    result = create_inventory(history, root=private_history_root / 'inventories')
    plan = history / 'plan.json'
    plan.write_bytes(plan.read_bytes() + b' ')
    with pytest.raises(ValueError, match='plan changed'):
        review_inventory(result['folder'])


def test_changed_discovery_receipt_cannot_be_silently_rebound(history, discovery, private_history_root):
    result = create_inventory(history, root=private_history_root / 'inventories')
    source = next((discovery / 'observations').glob('*/*.json'))
    source.write_bytes(source.read_bytes() + b' ')
    with pytest.raises(ValueError):
        review_inventory(result['folder'])


def test_rehashed_inventory_cannot_backdate_its_creation(history, private_history_root):
    result = create_inventory(history, root=private_history_root / 'inventories')
    folder = Path(result['folder'])
    inventory_path = folder / 'inventory.json'
    payload = _payload(result)
    payload['created_at'] = '2001-01-01T00:00:00+03:30'
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    inventory_path.write_bytes(body)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['inventory_sha256'] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='timestamp'):
        review_inventory(folder)


@pytest.mark.parametrize('which', ['history_attempt_before_registration', 'history_future_observation',
                                    'discovery_receipt_before_registration'])
def test_source_receipt_clock_cannot_predate_registration_or_be_in_future(
        history, discovery, private_history_root, which):
    archive = discovery if which.startswith('discovery') else history
    path = next((archive / 'observations').glob('*/*.json'))
    record = json.loads(path.read_text(encoding='utf-8'))
    if which == 'history_attempt_before_registration':
        record['attempted_at'] = '2001-01-01T00:00:00+03:30'
    elif which == 'history_future_observation':
        record['observation']['requested_at'] = '2099-01-01T00:00:00+03:30'
        record['observation']['received_at'] = '2099-01-01T00:00:00+03:30'
    else:
        record['observation']['requested_at'] = '2001-01-01T00:00:00+03:30'
        record['observation']['received_at'] = '2001-01-01T00:00:00+03:30'
    path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='time|Time'):
        create_inventory(history, root=private_history_root / 'inventories')


def test_history_plan_cannot_predate_discovery_receipts(history, private_history_root):
    path = history / 'plan.json'
    plan = json.loads(path.read_text(encoding='utf-8'))
    plan['registered_at'] = '2001-01-01T00:00:00+03:30'
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='precedes discovery'):
        create_inventory(history, root=private_history_root / 'inventories')


def test_unexpected_history_file_cannot_silently_disappear(history, private_history_root):
    target = history / 'observations' / '999'
    target.mkdir()
    (target / '0001.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected'):
        create_inventory(history, root=private_history_root / 'inventories')


def test_source_change_before_publish_never_creates_a_completed_inventory(history, private_history_root, monkeypatch):
    from ops import rights_cohort_inventory as module

    original = module._source_receipt
    calls = []

    def changed(*args, **kwargs):
        receipt = original(*args, **kwargs)
        calls.append(True)
        return receipt | {'files_sha256': '0' * 64} if len(calls) == 5 else receipt

    monkeypatch.setattr(module, '_source_receipt', changed)
    root = private_history_root / 'inventories'
    with pytest.raises(ValueError, match='before publication'):
        create_inventory(history, root=root)
    assert not list(root.glob('COHORT-*'))


def test_old_discovery_digest_without_two_code_fields_is_not_misreported_as_exact(discovery, private_history_root):
    history = create_plan(discovery, private_history_root / 'history')
    review = review_search(discovery)
    legacy = {key: value for key, value in review.items()
              if key not in ('capture_code_fingerprint_present', 'current_code_matches_capture_plan')}
    plan_path = history / 'plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    body = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    plan['source_review_sha256'] = hashlib.sha256(body).hexdigest()
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    record = create_inventory(history, root=private_history_root / 'inventories')
    receipt = _payload(record)['source_receipt']
    assert receipt['recorded_discovery_review_digest_matches_current'] is False
    assert receipt['recorded_discovery_review_digest_matches_without_code_fields'] is True
    assert not _payload(record)['inventory']['historical_universe_complete']


def test_rehashed_history_plan_cannot_drop_a_parent(discovery, private_history_root):
    history = create_plan(discovery, private_history_root / 'history')
    plan_path = history / 'plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    plan['candidates'][0]['parents'] = plan['candidates'][0]['parents'][:1]
    canonical = json.dumps(plan['candidates'], ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
    plan['candidate_snapshot_sha256'] = hashlib.sha256(canonical).hexdigest()
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='reviewed discovery'):
        create_inventory(history, root=private_history_root / 'inventories')


def test_sealed_history_plan_rejected_before_source_receipts(discovery, private_history_root, monkeypatch):
    from ops import rights_cohort_inventory as module

    history = create_plan(discovery, private_history_root / 'history')
    plan_path = history / 'plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    plan['end_date_exclusive'] = '2025-03-22'
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(module, '_source_receipt', lambda *args: pytest.fail('must not read source receipts'))
    with pytest.raises(ValueError):
        create_inventory(history, root=private_history_root / 'inventories')


def test_cli_uses_private_destination_and_no_database(history, private_history_root, capsys):
    from ops.rights_cohort_inventory import main

    assert main(['create', '--history', str(history)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['candidate_count'] == 1 and not output['accepted_for_signals']
    assert Path(output['folder']).is_relative_to(settings.DATA_DIR / 'private')
    assert not settings.DB_PATH.exists()
