import hashlib
import json
from datetime import timedelta

import pytest

from config import settings
from core.rights_retrospective_history import assess_history
from core.timeutils import now_tehran, now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from ops import rights_retrospective_history as ops


def native_row(day, code='42'):
    return dict(dEven=int(day.replace('-', '')), insCode=int(code), qTotTran5J=10, zTotTran=1,
                qTotCap=10000, priceFirst=1000, priceMin=1000, priceMax=1000,
                pDrCotVal=1000, priceYesterday=1000)


def native_observation(code, rows):
    content = json.dumps({'closingPriceDaily': rows}).encode()
    now = now_tehran_iso()
    return dict(source='tsetmc', ins_code=code,
                url=f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{code}/0',
                requested_at=now, received_at=now, http_status=200, response_bytes=len(content),
                body_sha256=hashlib.sha256(content).hexdigest(), content=content)


def test_provider_explicit_scope_and_date_selection(monkeypatch):
    provider = TsetmcProvider(realtime=True)
    today = now_tehran().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    start = (today - timedelta(days=2)).isoformat()
    source = native_observation('42', [native_row(start), native_row(yesterday), native_row(today.isoformat())])
    monkeypatch.setattr(provider, '_get_quote_context_json', lambda *a, **k: source)
    with pytest.raises(ValueError):
        provider.get_retrospective_rights_history_observation('42', start, today.isoformat())
    with pytest.raises(ValueError):
        provider.get_retrospective_rights_history_observation('42', start,
             (today + timedelta(days=1)).isoformat(), retrospective_rights_only=True)
    result = provider.get_retrospective_rights_history_observation('42', start, today.isoformat(),
                                                                    retrospective_rights_only=True)
    assert result['selected_row_count'] == 2
    assert [x['dEven'] for x in result['payload']['closingPriceDaily']] == [
        int(start.replace('-', '')), int(yesterday.replace('-', ''))]
    assert result['source_body_archived'] is False
    assert assess_history('42', start, today.isoformat(), result)['status'] == 'observed'
    assert assess_history('42', '2008-03-10', '2008-03-11', None)['status'] == 'unavailable'


def test_provider_rejects_future_native_date(monkeypatch):
    provider = TsetmcProvider(realtime=True)
    today = now_tehran().date()
    source = native_observation('42', [native_row((today + timedelta(days=1)).isoformat())])
    monkeypatch.setattr(provider, '_get_quote_context_json', lambda *a, **k: source)
    assert provider.get_retrospective_rights_history_observation('42',
        (today - timedelta(days=2)).isoformat(), today.isoformat(), retrospective_rights_only=True) is None


def test_provider_rejects_wrong_native_identity(monkeypatch):
    provider = TsetmcProvider(realtime=True)
    today = now_tehran().date()
    day = (today - timedelta(days=1)).isoformat()
    monkeypatch.setattr(provider, '_get_quote_context_json', lambda *a, **k: native_observation('42', [native_row(day, '43')]))
    assert provider.get_retrospective_rights_history_observation('42', day, today.isoformat(),
                                                                  retrospective_rights_only=True) is None


def test_empty_history_does_not_prove_no_trading(monkeypatch):
    provider = TsetmcProvider(realtime=True)
    today = now_tehran().date()
    day = (today - timedelta(days=1)).isoformat()
    monkeypatch.setattr(provider, '_get_quote_context_json', lambda *a, **k: native_observation('42', []))
    result = provider.get_retrospective_rights_history_observation('42', day, today.isoformat(),
                                                                   retrospective_rights_only=True)
    assert assess_history('42', day, today.isoformat(), result)['status'] == 'observed_empty_unverified'


def test_frozen_capture_failure_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(ops, 'review_universe', lambda folder: {
        'holdout_contaminated_for_rights_hypothesis': True,
        'candidates': {'42': {'names': [{'symbol': 'ABCح', 'instrument_id': 'IRR1TEST0101'}]}}})
    folder = tmp_path / 'private' / 'history'
    with pytest.raises(ValueError):
        ops.create_plan(tmp_path / 'private' / 'universe', folder, '2008-03-10', '2008-03-11')
    ops.create_plan(tmp_path / 'private' / 'universe', folder, '2008-03-10', '2008-03-11',
                    retrospective_rights_only=True)

    class Fake:
        last_transport_evidence = None
        def get_retrospective_rights_history_observation(self, code, start, end, **kwargs):
            return {'bad': 'response'}

    assert ops.capture(Fake(), folder)['stop_reason'] == 'source_unavailable_or_invalid'
    r = ops.review(folder)
    assert r['status_counts'] == {'invalid_response': 1}
    assert r['holdout_contaminated_for_rights_hypothesis'] is True
    assert r['accepted_for_signals'] is False


def test_plan_merges_pre_holdout_and_retrospective_with_full_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    pre = tmp_path / 'private' / 'daily'
    retro = tmp_path / 'private' / 'retro'
    monkeypatch.setattr(ops, 'review_pre_holdout_universe', lambda folder: {'candidates': {
        '42': {'names': [{'symbol': 'ABCح', 'instrument_id': 'IRR1TEST0101'}]},
        '43': {'names': [{'symbol': 'OLDح', 'instrument_id': 'IRR1OLD0101'}]}}})
    monkeypatch.setattr(ops, 'review_universe', lambda folder: {
        'holdout_contaminated_for_rights_hypothesis': True, 'candidates': {
            '42': {'names': [{'symbol': 'ABCح', 'instrument_id': 'IRR1OTHER0101'}]},
            '44': {'names': [{'symbol': 'NEWح', 'instrument_id': None}]}}})
    folder = tmp_path / 'private' / 'history'
    ops.create_plan(retro, folder, '2008-03-10', '2008-03-11', retrospective_rights_only=True,
                    pre_holdout_universe_folders=[pre])
    plan = ops._read_plan(folder)
    rows = {row['ins_code']: row for row in plan['candidates']}
    assert set(rows) == {'42', '43', '44'}
    assert rows['42']['sources'] == sorted([str(pre.resolve()), str(retro.resolve())])
    assert rows['42']['identity_ambiguous'] is True
    assert {x['source_folder'] for x in rows['42']['identities']} == {str(pre.resolve()), str(retro.resolve())}
    assert rows['43']['identity_ambiguous'] is False
    assert rows['44']['identity_ambiguous'] is True
    assert {x['kind'] for x in plan['source_universes']} == {'pre_holdout', 'retrospective'}
    result = ops.review(folder)
    assert result['candidate_count'] == 3
    assert result['historical_universe_complete'] is False
    assert result['holdout_contaminated_for_rights_hypothesis'] is True


def test_pre_holdout_only_and_duplicate_source_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    pre = tmp_path / 'private' / 'daily'
    monkeypatch.setattr(ops, 'review_pre_holdout_universe', lambda folder: {'candidates': {
        '42': {'names': [{'symbol': 'ABCح', 'instrument_id': 'IRR1TEST0101'}]}}})
    folder = tmp_path / 'private' / 'history'
    ops.create_plan(None, folder, '2008-03-10', '2008-03-11', retrospective_rights_only=True,
                    pre_holdout_universe_folders=[pre])
    assert ops.review(folder)['candidate_count'] == 1
    with pytest.raises(ValueError, match='Distinct'):
        ops.create_plan(None, tmp_path / 'private' / 'other', '2008-03-10', '2008-03-11',
                        retrospective_rights_only=True, pre_holdout_universe_folders=[pre, pre])


def test_legacy_plan_keeps_its_fingerprint_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(ops, 'review_universe', lambda folder: {
        'holdout_contaminated_for_rights_hypothesis': True,
        'candidates': {'42': {'names': [{'symbol': 'ABCح', 'instrument_id': 'IRR1TEST0101'}]}}})
    folder = tmp_path / 'private' / 'history'
    ops.create_plan(tmp_path / 'private' / 'retro', folder, '2008-03-10', '2008-03-11',
                    retrospective_rights_only=True)
    path = folder / 'plan.json'
    plan = json.loads(path.read_text(encoding='utf-8'))
    plan['version'] = ops.LEGACY_VERSION
    plan['candidates'] = [{'ins_code': '42', 'identities': [{'symbol': 'ABCح', 'instrument_id': 'IRR1TEST0101'}]}]
    plan['candidate_snapshot_sha256'] = hashlib.sha256(ops._canonical(plan['candidates'])).hexdigest()
    plan['capture_code_sha256'] = ops._code_fingerprints(ops.LEGACY_SOURCE_FILES)
    path.write_text(json.dumps(plan), encoding='utf-8')
    result = ops.review(folder)
    assert result['version'] == ops.LEGACY_VERSION
    assert result['current_code_matches_capture_plan'] is True


def test_reviewed_name_search_preserves_ambiguous_parent_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    discovery = tmp_path / 'private' / 'discovery'
    monkeypatch.setattr(ops, 'review_name_search', lambda folder: {
        'current_code_matches_capture_plan': None,
        'rows': [{'parent': {'parent_symbol': 'نام'}, 'status': 'observed_matches', 'candidate_codes': ['42']},
                 {'parent': {'parent_symbol': 'دیگر'}, 'status': 'observed_with_invalid_rows',
                  'candidate_codes': ['42', '43']},
                 {'parent': {'parent_symbol': 'خالی'}, 'status': 'observed_no_match', 'candidate_codes': []}]})
    folder = tmp_path / 'private' / 'history'
    ops.create_plan(None, folder, '2008-03-10', '2008-03-11', retrospective_rights_only=True,
                    discovery_folders=[discovery])
    plan = ops._read_plan(folder)
    rows = {row['ins_code']: row for row in plan['candidates']}
    assert set(rows) == {'42', '43'}
    assert {item['symbol'] for item in rows['42']['identities']} == {'نامح', 'دیگرح'}
    assert rows['42']['identity_ambiguous'] is True
    assert rows['43']['identity_ambiguous'] is True  # ISIN was not reported by this search contract.
    assert plan['source_universes'][0]['kind'] == 'name_search'
    assert plan['source_universes'][0]['current_code_matches_capture_plan'] is None
    assert ops.review(folder)['historical_universe_complete'] is False
    with pytest.raises(ValueError, match='Distinct'):
        ops.create_plan(None, tmp_path / 'private' / 'dup', '2008-03-10', '2008-03-11',
                        retrospective_rights_only=True, discovery_folders=[discovery, discovery])
