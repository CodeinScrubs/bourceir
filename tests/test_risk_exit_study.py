"""Full paired-policy reconstruction and a narrow original-registration snapshot."""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from features.store import build_feature_store
from research.holding_horizon import run_holding_study
from research.risk_exit_study import review_risk_exit_study, run_risk_exit_study
from research.run import _sha256, run_research

pytest_plugins = ['tests.test_flow_research']


@pytest.fixture
def parent_case(db, flow_baseline, tmp_path):
    _, data = flow_baseline
    for symbol in ('الف', 'ب', 'ج'):
        sid = db.get_symbol(symbol)['id']
        prices = db.get_daily_prices_df(symbol)
        # Known adverse move followed by a recovery: a risk exit must be allowed to lose the recovery.
        shock = prices.index.to_series().between(110, 129)
        fields = ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'value']
        prices.loc[shock, fields] *= .6
        prices['ref_price_raw'] = prices.close_final_raw.shift().fillna(prices.ref_price_raw.iloc[0])
        db.upsert_daily_prices_df(sid, prices)
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    output = tmp_path/'parents'
    result = run_holding_study(db, source_root=data/'research', source_id=source['experiment_id'], root=output,
        initial_capital_rial=100_000_000, prior_trials=32, min_train_sessions=20, test_sessions=70)
    # The source registry intentionally contains unrelated data that must not be bulk copied.
    db.conn.execute('CREATE TABLE private_probe (secret TEXT)')
    db.conn.execute('INSERT INTO private_probe VALUES (?)', ('test-only-private-note',))
    db.conn.commit()
    path = Path(db.conn.execute('PRAGMA database_list').fetchone()[2])
    return output/result['experiment_id'], path, result


def test_paired_risk_study_keeps_original_paths_and_recomputes_both_policies(db, parent_case, tmp_path, monkeypatch):
    from research import risk_exit_study

    parent, registry, original = parent_case
    before = _sha256(parent/'result.json')
    calls = []
    register = risk_exit_study._register

    def record(*args, **kwargs):
        calls.append(args[1])
        return register(*args, **kwargs)

    replay = risk_exit_study.replay_portfolio

    def registered_first(*args, **kwargs):
        assert calls and calls[0].endswith('-PROTOCOL')
        return replay(*args, **kwargs)

    monkeypatch.setattr(risk_exit_study, '_register', record)
    monkeypatch.setattr(risk_exit_study, 'replay_portfolio', registered_first)
    output = tmp_path/'risk'
    result = run_risk_exit_study(db, parent_folder=parent, parent_db=registry, root=output)
    assert calls == [result['experiment_id']+'-PROTOCOL', result['experiment_id']]
    assert result['protocol']['minimum_disclosed_trials'] == original['protocol']['minimum_disclosed_trials']+27
    assert len(result['results'][0]['modes']) == 54
    for mode, value in original['results'][0]['modes'].items():
        assert result['results'][0]['modes'][mode] == value
    variant = result['results'][0]['modes']['hold60-momentum_20d-risk-exit']
    assert variant['metrics']['risk_exit_request_date'] is not None
    assert variant['metrics']['risk_exit_sell_executions'] > 0
    folder = output/result['experiment_id']
    reviewed = review_risk_exit_study(db, folder)
    assert reviewed['original_paths_recomputed'] == reviewed['new_paths_recomputed'] == 27
    assert reviewed['base_compatibility_verified'] and reviewed['current_code_matches_capture']
    assert not result['accepted_for_signals'] and not result['profitability_proven'] and not result['holdout_eligible']
    with sqlite3.connect(folder/'parent-registry.db') as snapshot:
        assert snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('experiments',)]
        assert snapshot.execute('SELECT COUNT(*) FROM experiments').fetchone()[0] == 2
        rows = snapshot.execute('SELECT experiment_id,created_at FROM experiments ORDER BY experiment_id').fetchall()
    expected = [tuple(db.conn.execute('SELECT experiment_id,created_at FROM experiments WHERE experiment_id=?', (name,)).fetchone())
                for name in sorted((original['experiment_id'], original['experiment_id']+'-PROTOCOL'))]
    assert rows == expected
    assert _sha256(parent/'result.json') == before


def test_risk_execution_tampering_still_fails_after_rehash(db, parent_case, tmp_path):
    parent, registry, _ = parent_case
    result = run_risk_exit_study(db, parent_folder=parent, parent_db=registry, root=tmp_path/'risk')
    folder = tmp_path/'risk'/result['experiment_id']
    name = 'fold-00-hold60-momentum_20d-risk-exit-executions.parquet'
    frame = pd.read_parquet(folder/name)
    assert len(frame) > 1
    frame.loc[0, 'price_rial'] += 1
    frame.to_parquet(folder/name, index=False)
    result['artifacts_sha256'][name] = _sha256(folder/name)
    (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(AssertionError):
        review_risk_exit_study(db, folder)


def test_failed_risk_registration_never_simulates_or_publishes(db, parent_case, tmp_path, monkeypatch):
    from research import risk_exit_study

    parent, registry, _ = parent_case
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    monkeypatch.setattr(risk_exit_study, 'replay_portfolio', lambda *a, **k: pytest.fail('unregistered policy replay'))
    output = tmp_path/'risk'
    with pytest.raises(RuntimeError, match='ثبت پروتکل'):
        run_risk_exit_study(db, parent_folder=parent, parent_db=registry, root=output)
    cases = list(output.iterdir())
    assert len(cases) == 1 and (cases[0]/'protocol.json').exists()
    assert not (cases[0]/'result.json').exists() and not list(cases[0].glob('*.parquet'))
