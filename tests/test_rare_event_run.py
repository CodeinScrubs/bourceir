import json

import pandas as pd
import pytest

from config import settings
from research.rare_event_run import review_census, run_census
from research.run import _sha256


def inputs(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'dev_start', '2024-01-01')
    days = pd.date_range('2024-01-01', periods=65).strftime('%Y-%m-%d')
    values = [100., *[200.]*64]
    prices = pd.DataFrame(dict(symbol='الف', date=days, close_final_raw=values,
        ref_price_raw=[100., *values[:-1]], volume=100))
    path = tmp_path/'input.parquet'
    prices.to_parquet(path, index=False)
    return path


def test_census_registers_then_reconstructs_all_anchors_and_outcomes(db, tmp_path, monkeypatch):
    path = inputs(tmp_path, monkeypatch)
    result = run_census(db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path/'runs', min_symbols=1)
    folder = tmp_path/'runs'/result['experiment_id']
    review = review_census(db, folder)
    assert review['outcomes_recomputed'] == 12 and review['cohort_reconstructed'] == 4
    assert review['current_code_matches_capture'] and not review['accepted_for_signals']
    assert result['protocol']['strategy_trials_added'] == 0
    assert [s['opportunities'] for s in result['summary']] == [4, 4, 4]
    assert result['summary'][-1]['unknown'] == 3


def test_outcome_tamper_and_rehash_does_not_evade_replay(db, tmp_path, monkeypatch):
    path = inputs(tmp_path, monkeypatch)
    result = run_census(db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path/'runs', min_symbols=1)
    folder = tmp_path/'runs'/result['experiment_id']
    outcomes = pd.read_parquet(folder/'outcomes.parquet')
    outcomes.loc[0, 'first_hit_session'] = 8
    outcomes.to_parquet(folder/'outcomes.parquet', index=False)
    result['artifacts_sha256']['outcomes.parquet'] = _sha256(folder/'outcomes.parquet')
    (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(AssertionError):
        review_census(db, folder)


def test_registration_failure_never_computes_outcomes(db, tmp_path, monkeypatch):
    path = inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    import research.rare_event_run as runner
    monkeypatch.setattr(runner, 'compute_rare_events', lambda *args, **kwargs: pytest.fail('outcomes before registration'))
    with pytest.raises(RuntimeError):
        run_census(db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path/'runs', min_symbols=1)
    assert not list((tmp_path/'runs').rglob('outcomes.parquet'))


@pytest.mark.parametrize('field', ['accepted_for_signals', 'profitability_proven'])
def test_forged_acceptance_claim_is_rejected(db, tmp_path, monkeypatch, field):
    path = inputs(tmp_path, monkeypatch)
    result = run_census(db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path/'runs', min_symbols=1)
    folder = tmp_path/'runs'/result['experiment_id']
    result[field] = True
    (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(ValueError, match='قرارداد توصیفی'):
        review_census(db, folder)


def test_holdout_input_is_refused_before_registration(db, tmp_path, monkeypatch):
    path = inputs(tmp_path, monkeypatch)
    prices = pd.read_parquet(path)
    prices.loc[len(prices)-1, 'date'] = settings.RESEARCH_CONFIG['holdout_start']
    prices.to_parquet(path, index=False)
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: pytest.fail('holdout must fail before registration'))
    with pytest.raises(ValueError):
        run_census(db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path/'runs', min_symbols=1)
