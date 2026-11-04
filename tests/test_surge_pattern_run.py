import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from research.rare_event_run import run_census
from research.run import _sha256
from research.surge_pattern_run import review_pattern_study, run_pattern_study


def parent_census(db, tmp_path, monkeypatch):
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'dev_start', '2020-01-01')
    days = pd.date_range('2020-01-01', periods=900).strftime('%Y-%m-%d')
    values = 100*np.power(1.002, np.arange(len(days)))
    frame = pd.DataFrame(dict(symbol='الف', date=days, open_raw=values, high_raw=values*1.01,
        low_raw=values*.99, close_raw=values, close_final_raw=values,
        ref_price_raw=np.r_[100., values[:-1]], volume=100, value=100*values))
    path = tmp_path/'prices.parquet'
    frame.to_parquet(path, index=False)
    result = run_census(db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path/'parents', min_symbols=1)
    return tmp_path/'parents'/result['experiment_id']


def test_registered_pipeline_keeps_all_hypotheses_and_records_temporal_boundaries(db, tmp_path, monkeypatch):
    parent = parent_census(db, tmp_path, monkeypatch)
    result, folder = run_pattern_study(db, parent=parent, root=tmp_path/'studies', replications=1000)
    assert len(result['summary']) == 20 and result['temporal_folds']
    assert not result['accepted_for_signals'] and not result['profitability_proven']
    assert result['protocol']['thresholds_optimized'] is False
    assert result['protocol']['embargo_sessions'] == 61
    assert result['protocol']['execution_horizons'] == [19, 59]
    assert not result['protocol']['recent_winners_used_for_selection']
    registry = db.conn.execute('SELECT status FROM experiments WHERE experiment_id=?',
                               (result['experiment_id']+'-PROTOCOL',)).fetchone()
    assert registry[0] == 'registered'
    for name, digest in result['artifact_sha256'].items():
        assert _sha256(folder/name) == digest
    replay = review_pattern_study(db, folder)
    assert replay['features_recomputed'] == len(pd.read_parquet(parent/'cohort.parquet'))
    assert replay['hypothesis_horizon_pairs_recomputed'] == 20
    assert replay['current_code_matches_capture']
    for fold in result['temporal_folds']:
        assert fold['boundaries']['train_end_exclusive'] < fold['boundaries']['test_start']
    features = pd.read_parquet(folder/'features.parquet')
    cohort = pd.read_parquet(parent/'cohort.parquet')
    assert len(features) == len(cohort)
    assert set(map(tuple, features[['symbol', 'date']].to_numpy())) == set(map(tuple, cohort[['symbol', 'date']].to_numpy()))


def test_registration_failure_cannot_start_new_feature_computation(db, tmp_path, monkeypatch):
    parent = parent_census(db, tmp_path, monkeypatch)
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    import research.surge_pattern_run as runner
    monkeypatch.setattr(runner, 'build_pre_move_features', lambda *a, **k: pytest.fail('unregistered computation'))
    with pytest.raises(RuntimeError, match='registration'):
        run_pattern_study(db, parent=parent, root=tmp_path/'studies', replications=1000)
    assert not list((tmp_path/'studies').rglob('features.parquet'))


def test_parent_tampering_is_rejected_before_features_are_computed(db, tmp_path, monkeypatch):
    parent = parent_census(db, tmp_path, monkeypatch)
    with (parent/'cohort.parquet').open('ab') as stream:
        stream.write(b'tamper')
    with pytest.raises(ValueError, match='fingerprint'):
        run_pattern_study(db, parent=parent, root=tmp_path/'studies', replications=1000)


def test_holdout_parent_protocol_and_production_db_are_rejected(db, tmp_path, monkeypatch):
    parent = parent_census(db, tmp_path, monkeypatch)
    path = parent/'result.json'
    result = json.loads(path.read_text(encoding='utf-8'))
    result['protocol']['observed_through'] = settings.RESEARCH_CONFIG['holdout_start']
    path.write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(ValueError, match='sealed'):
        run_pattern_study(db, parent=parent, root=tmp_path/'studies', replications=1000)
    monkeypatch.setattr(db, 'db_path', settings.DB_PATH)
    with pytest.raises(ValueError, match='research DB'):
        run_pattern_study(db, parent=parent, root=tmp_path/'studies', replications=1000)


def test_rehashed_feature_tamper_does_not_evade_recomputation(db, tmp_path, monkeypatch):
    parent = parent_census(db, tmp_path, monkeypatch)
    result, folder = run_pattern_study(db, parent=parent, root=tmp_path/'studies', replications=1000)
    frame = pd.read_parquet(folder/'features.parquet')
    frame.loc[len(frame)-1, 'ret_20'] = 99.
    frame.to_parquet(folder/'features.parquet', index=False)
    result['artifact_sha256']['features.parquet'] = _sha256(folder/'features.parquet')
    (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(AssertionError):
        review_pattern_study(db, folder)
