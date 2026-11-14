"""Dated zero activity resolves execution without inventing price candles."""

import base64
import json
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from research.strategy_execution import load_publications, review_execution_followup, run_execution_followup
from research.strategy_suite import SuitePlan, _sha256, run_suite
from tests.test_portfolio import flat_publication
from tests.test_strategy_suite import inputs


def publication_payload(code, symbol, day):
    observation = flat_publication(code, day, mark=100, reference=100)
    observation['http_status'] = 200
    observation['requested_at'] = observation['received_at'] = '2024-06-01T14:00:00+03:30'
    body = observation.pop('content')
    observation['content_base64'] = base64.b64encode(body).decode('ascii')
    return dict(kind='priority_missing_session_publications', records=[dict(
        request=dict(ins_code=code, symbol=symbol, date=day), observation=observation)])


@pytest.fixture
def parent(tmp_path, monkeypatch, db):
    import research.labels as labels
    import research.strategy_suite as suite

    root = tmp_path/'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    monkeypatch.setattr(labels, 'CALENDAR_MIN_SYMBOLS', 2)
    raw, clients, registry, days = inputs(110, ('a', 'b', 'c'))
    # First decision at 60+2, entry at 63. A native zero-activity receipt
    # resolves the missing entry; no zero OHLC row is inserted into raw data.
    missing = days[63]
    raw = raw.loc[~(raw.symbol.eq('a') & raw.date.eq(missing))].reset_index(drop=True)
    clients = clients.merge(raw[['symbol', 'date']], on=['symbol', 'date'], validate='one_to_one')
    monkeypatch.setattr(suite, 'snapshot_inputs', lambda *_: (raw, clients, registry))
    plan = SuitePlan(start=days[0], end_exclusive='2024-05-01', initial_capital_rial=10000,
        min_daily_value_rial=0, max_positions=2, min_train_sessions=60, test_sessions=30)
    result = run_suite(db, plan=plan, case_names=['base:buyer_power:h1'])
    target = root/'private'/'observations.json'
    target.write_text(json.dumps(publication_payload('1', 'a', missing)), encoding='utf-8')
    return Path(result['folder']), target, missing, registry, days


def test_native_receipt_resolves_entry_and_complete_replay_is_reproducible(parent):
    folder, observations, missing, _, _ = parent
    original = json.loads((folder/'results.json').read_bytes())
    assert any(r['audit']['observed_path_model_return'] is None for r in original['records'])
    result = run_execution_followup(folder, observations)
    assert result['invariant_failures'] == 0
    assert all(r['audit']['observed_path_model_return'] is not None for r in result['records']), [
        (r['audit'], r['decision_diagnostics']) for r in result['records'] if r['audit']['observed_path_model_return'] is None]
    assert any(r['decision_diagnostics']['status_counts'].get('observed_no_trade_entry', 0)
               for r in result['records'])
    assert len(pd.read_parquet(folder/'prices.parquet').query('symbol == "a" and date == @missing')) == 0
    reviewed = review_execution_followup(result['folder'], reexecute=True)
    assert reviewed['financial_reexecution_performed'] and not reviewed['accepted_for_signals']


def test_parallel_fold_archive_matches_serial_replay(parent, monkeypatch, db):
    import zipfile

    import research.strategy_suite as suite

    _, observations, missing, _, _ = parent
    raw, clients, registry, days = inputs(150, ('a', 'b', 'c'))
    raw = raw.loc[~(raw.symbol.eq('a') & raw.date.eq(missing))].reset_index(drop=True)
    clients = clients.merge(raw[['symbol', 'date']], on=['symbol', 'date'], validate='one_to_one')
    monkeypatch.setattr(suite, 'snapshot_inputs', lambda *_: (raw, clients, registry))
    source = run_suite(db, plan=SuitePlan(start=days[0], end_exclusive='2024-07-01',
        initial_capital_rial=10000, min_daily_value_rial=0, max_positions=2,
        min_train_sessions=60, test_sessions=30), case_names=['base:buyer_power:h1'])
    serial = run_execution_followup(source['folder'], observations, use_cache=False)
    parallel = run_execution_followup(source['folder'], observations, workers=2, use_cache=False, allow_repeat=True)
    assert parallel['expected_replays'] >= 4
    assert all(serial[k] == parallel[k] for k in ('records', 'summary', 'residual_requests', 'publication_coverage'))
    with (zipfile.ZipFile(Path(serial['folder'])/'replays.zip') as one,
          zipfile.ZipFile(Path(parallel['folder'])/'replays.zip') as two):
        assert one.namelist() == two.namelist()
        assert all(one.read(n) == two.read(n) for n in one.namelist())
    assert review_execution_followup(parallel['folder'])['replays'] == parallel['expected_replays']


@pytest.mark.parametrize('defect', ['identity', 'duplicate', 'hash', 'http'])
def test_invalid_native_inputs_never_become_no_trade_marks(defect):
    _, _, registry, days = inputs(8, ('a',))
    payload = publication_payload('1', 'a', days[1])
    if defect == 'identity':
        payload['records'][0]['request']['ins_code'] = '2'
    elif defect == 'duplicate':
        payload['records'].append(payload['records'][0])
    elif defect == 'hash':
        payload['records'][0]['observation']['body_sha256'] = 'f'*64
    else:
        payload['records'][0]['observation']['http_status'] = 206
    contents = json.dumps(payload).encode()
    if defect in ('identity', 'duplicate'):
        with pytest.raises(ValueError):
            load_publications(contents, registry, days, '2025-03-21')
    else:
        entry, marks, coverage = load_publications(contents, registry, days, '2025-03-21')
        assert not entry and not marks and coverage[0]['status'] == 'unverified_native_response'


def test_reviewer_preserves_full_execution_denominator_even_after_rehash(parent):
    folder, observations, *_ = parent
    result = run_execution_followup(folder, observations)
    root = Path(result['folder'])
    payload = json.loads((root/'results.json').read_bytes())
    payload['records'].pop()
    (root/'results.json').write_text(json.dumps(payload), encoding='utf-8')
    manifest = json.loads((root/'manifest.json').read_bytes())
    manifest['results.json'] = _sha256(root/'results.json')
    (root/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='denominator'):
        review_execution_followup(root)


def test_execution_review_detects_tampered_native_input(parent):
    folder, observations, *_ = parent
    result = run_execution_followup(folder, observations)
    root = Path(result['folder'])
    (root/'publications.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='manifest'):
        review_execution_followup(root)
