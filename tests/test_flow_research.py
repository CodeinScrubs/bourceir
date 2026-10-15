"""Registration, genuine candidates, matched controls and no holdout access."""

import json

import pandas as pd
import pytest

from config import settings
from core.flow_filters import RESEARCH_FILTERS
from research.flow_run import run_flow_research
from tests.test_flow_filters import history


@pytest.fixture
def flow_baseline(db, tmp_path, monkeypatch):
    from features import store
    from research import labels as label_module
    from research import run
    p, c = history(160)
    for index, price in [(112, 1020.), (145, 1030.)]:
        p.loc[index, ['volume', 'value', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw']] = [400_000_000., 400_000_000.*(price-10), 1000., price, price, price-10]
        p.loc[index+1, 'ref_price_raw'] = price-10
        c.loc[index, ['buy_i_volume', 'buy_n_volume', 'sell_i_volume']] = [320_000_000., 80_000_000., 200_000_000.]
        c.loc[index, ['buy_i_value', 'sell_i_value']] = [p.loc[index, 'value']*.9, p.loc[index, 'value']*.2]
    c = c.drop(index=[120])
    for code, symbol in [(300, 'الف'), (303, 'ب'), (300, 'ج')]:
        sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=symbol, instrument_type_code=code))
        db.upsert_daily_prices_df(sid, p.drop(columns='symbol'))
        db.upsert_client_types_df(sid, c.drop(columns='symbol'))
    root = tmp_path/'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    monkeypatch.setattr(store, 'FEATURES_ROOT', root/'features')
    monkeypatch.setattr(run, 'RESEARCH_ROOT', root/'research')
    monkeypatch.setattr(run, 'LABELS_ROOT', root/'labels')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'dev_start', '2024-01-01')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2024-08-01')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'min_daily_value_rial', 0)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [2, 3])
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'top_k', 2)
    monkeypatch.setattr(label_module, 'CALENDAR_MIN_SYMBOLS', 2)
    store.build_feature_store(db)
    source = run.run_research(db, random_trials=1)
    return source, root


def test_registered_flow_research_has_real_candidates_and_matched_controls(db, flow_baseline):
    from research.run import _sha256
    source, root = flow_baseline
    result = run_flow_research(db, min_train_sessions=104, test_sessions=30)
    assert result['protocol']['source_id'] == source['experiment_id']
    assert not result['accepted_for_signals']
    assert not (root/'research'/'holdout-unsealed.json').exists()
    folder = root/'research'/result['experiment_id']
    metrics = pd.read_parquet(folder/'metrics.parquet')
    assert metrics.date.max() < settings.RESEARCH_CONFIG['holdout_start']
    assert result['filter_coverage']['special_original']['matches'] == 0
    for name in RESEARCH_FILTERS:
        seen = 0
        for file in folder.glob(f'fold-*-{name}-2d-picks.parquet'):
            chosen = pd.read_parquet(file)
            random = pd.read_parquet(file.with_name(file.name.replace('-2d-picks', '-2d-random-picks')))
            stress = pd.read_parquet(file.with_name(file.name.replace('-2d-picks', '-2d-stress-picks')))
            pd.testing.assert_frame_equal(chosen, stress)
            pd.testing.assert_series_equal(chosen.groupby('date').size(), random.groupby('date').size())
            seen += len(chosen)
        assert seen > 0, name
    for file, digest in result['artifacts_sha256'].items():
        assert _sha256(folder/file) == digest
    assert json.loads((root/'research'/'latest-flow.json').read_text(encoding='utf-8'))['experiment_id'] == result['experiment_id']
    experiments = {e['experiment_id'] for e in db.get_experiments()}
    assert result['experiment_id']+'-PROTOCOL' in experiments and result['experiment_id'] in experiments
    assert result['comparisons']['retail_acceleration-2d']['all_folds_known']


def test_revised_raw_client_data_cannot_be_mixed_with_old_baseline(db, flow_baseline):
    _, root = flow_baseline
    with db.conn:
        db.conn.execute('UPDATE client_types_daily SET buy_i_value=buy_i_value*2 WHERE date=?', ('2024-02-01',))
    with pytest.raises(ValueError, match='منشأ فیچر'):
        run_flow_research(db, min_train_sessions=104, test_sessions=30)
    assert not (root/'research'/'latest-flow.json').exists()


def test_failed_protocol_registration_never_evaluates_or_publishes(db, flow_baseline, monkeypatch):
    _, root = flow_baseline
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    with pytest.raises(RuntimeError, match='ثبت پروتکل'):
        run_flow_research(db, min_train_sessions=104, test_sessions=30)
    assert not (root/'research'/'latest-flow.json').exists()
