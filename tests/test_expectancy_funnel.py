"""Recorded opportunities, selection and non-execution remain distinct."""

import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from config import settings
from ops.research_review import sha256
from research.expectancy import fit_expectancy, forecast_expectancy
from research.expectancy_funnel import entry_funnel, forecast_funnel, inspect_archive
from research.expectancy_run import CANDIDATES
from tests.test_expectancy import sample


def forecasts():
    return pd.DataFrame(dict(date=['2024-01-05']*5+['2024-01-09'],
        symbol=['الف', 'ب', 'ج', 'د', 'ه', 'و'],
        normal_expected_net=[.03, .03, .02, -.01, np.nan, .04],
        stress_expected_net=[.01, .01, -.01, -.02, np.nan, .02],
        status=['forecasted']*4+['unknown_features', 'forecasted']))


def test_unknown_nonpositive_stress_timing_and_rank_exclusions_are_disjoint():
    counts, picks = forecast_funnel(forecasts(), selection_end='2024-01-09', max_positions=1)
    assert picks.to_dict('records') == [dict(date='2024-01-05', symbol='الف', rank=0)]
    assert counts['recorded_opportunities'] == 6
    assert counts['unknown_forecast_rows'] == counts['nonpositive_normal_rows'] == 1
    assert counts['positive_normal_nonpositive_stress_rows'] == counts['positive_but_too_late_rows'] == 1
    assert counts['excluded_by_rank_rows'] == counts['selected_rows'] == 1
    assert counts['probability_of_profit'] is None and not counts['accepted_for_signals']


def test_unavailable_model_never_becomes_a_zero_forecast_or_positive_signal():
    frame = forecasts().iloc[[4]].copy()
    frame['status'] = 'model_unavailable'
    counts, picks = forecast_funnel(frame, selection_end='2024-01-09', max_positions=1)
    assert picks.empty and counts['maximum_normal_expected_net'] is None
    assert counts['maximum_stress_expected_net'] is None and counts['unknown_forecast_rows'] == 1


@pytest.mark.parametrize('change', ['unknown_number', 'incoherent_pair', 'boolean', 'duplicate', 'status', 'future_outcome'])
def test_incoherent_or_leaked_forecast_rows_are_rejected(change):
    frame = forecasts()
    if change == 'unknown_number':
        frame.loc[4, 'normal_expected_net'] = .1
    elif change == 'incoherent_pair':
        frame.loc[0, 'stress_expected_net'] = .1
    elif change == 'boolean':
        frame['normal_expected_net'] = frame.normal_expected_net.astype(object)
        frame.loc[0, 'normal_expected_net'] = True
    elif change == 'duplicate':
        frame = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
    elif change == 'status':
        frame.loc[4, 'status'] = 'guaranteed'
    else:
        frame['net_ret_2d'] = 1.
    with pytest.raises(ValueError):
        forecast_funnel(frame, selection_end='2024-01-09', max_positions=1)


def test_selected_but_locked_is_different_from_no_selection_and_missing_attribution():
    _, picks = forecast_funnel(forecasts(), selection_end='2024-01-09', max_positions=1)
    days = ['2024-01-05', '2024-01-06']
    locked = pd.DataFrame([dict(date=days[1], symbol='الف', decision_date=days[0], status='locked_buy')])
    result = entry_funnel(picks, pd.DataFrame(), locked, days)
    assert result['selected_rows'] == 1 and result['buy_execution_rows'] == 0
    assert result['explicit_nonexecution_reasons'] == {'locked_buy': 1}
    assert result['attribution_complete'] and not result['live_fills_verified']
    absent = entry_funnel(picks, pd.DataFrame(), pd.DataFrame(), days)
    assert absent['unattributed_selected_rows'] == 1 and not absent['attribution_complete']
    buys = pd.DataFrame([dict(date=days[1], symbol='الف', side='buy')])
    assert entry_funnel(picks, buys, pd.DataFrame(), days)['buy_execution_rows'] == 1
    with pytest.raises(ValueError, match='conflicts'):
        entry_funnel(picks, buys, locked, days)
    with pytest.raises(ValueError, match='next-session'):
        entry_funnel(picks, buys.assign(date=days[0]), pd.DataFrame(), days)


@pytest.fixture
def archive(tmp_path, db):
    source_id = 'EXP-2024-01-15-EXPECTANCY-'+'a'*32
    folder = tmp_path/source_id
    folder.mkdir()
    days = pd.date_range('2024-01-05', periods=7).strftime('%Y-%m-%d').tolist()
    x, y = sample()
    model = fit_expectancy(x, y, train_end_exclusive='2024-01-03')
    frame = x.iloc[:14].copy()
    frame['date'], frame['symbol'] = np.repeat(days, 2), ['الف', 'ب']*7
    frame['rel_ret_5d'] = .04
    forecast = forecast_expectancy(frame, model)
    _, picks = forecast_funnel(forecast, selection_end=days[-3], max_positions=1)
    next_day = dict(zip(days[:-1], days[1:], strict=True))
    decisions = pd.DataFrame([dict(date=next_day[row.date], symbol=row.symbol,
        decision_date=row.date, status='locked_buy') for row in picks.itertuples(index=False)])
    artifacts = {}

    def parquet(name, value):
        value.to_parquet(folder/name, index=False)
        artifacts[name] = sha256(folder/name)

    parquet('features.parquet', frame)
    parquet('fold-00-forecasts.parquet', forecast)
    parquet('fold-00-idle_cash-equity.parquet', pd.DataFrame(dict(date=days, equity_rial=1.)))
    modes = {}
    for mode in ('conditional-2d', 'conditional-1d', 'conditional-2d-stress', 'conditional-1d-stress'):
        parquet('fold-00-'+mode+'-picks.parquet', picks)
        parquet('fold-00-'+mode+'-executions.parquet', pd.DataFrame())
        parquet('fold-00-'+mode+'-decisions.parquet', decisions)
        modes[mode] = dict(metrics=dict(executions=0))
    source_files = {}
    for name in ('research/expectancy.py', 'research/expectancy_run.py'):
        target = folder/'code'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n'))
        source_files[name] = sha256(target)
    fold = dict(train_start='2023-01-01', train_end_exclusive='2024-01-03', test_start=days[0], test_end_exclusive='2024-01-12')
    protocol = dict(version='expectancy-portfolio-v3', candidate_modes=list(CANDIDATES), holdout_start='2025-03-21',
        predictors=model['predictors'], folds=[fold], config=dict(max_positions=1),
        source_files=source_files, inputs=dict(features=artifacts['features.parquet']))
    model_file = folder/'fold-00-model.json'
    model_file.write_text(json.dumps(model), encoding='utf-8')
    payload = dict(experiment_id=source_id, protocol=protocol, summary={}, accepted_for_signals=False,
        holdout_eligible=False, artifacts_sha256=artifacts, models_sha256={model_file.name: sha256(model_file)},
        results=[dict(fold=fold, modes=modes)])
    (folder/'protocol.json').write_text(json.dumps(protocol), encoding='utf-8')
    folder.with_suffix('.json').write_text(json.dumps(payload), encoding='utf-8')
    for name in (source_id, source_id+'-PROTOCOL'):
        assert db.log_experiment(name, 'test-only-funnel', 'fictional tests only', json.dumps(protocol), '{}') is not None
    return db, source_id, tmp_path, folder, payload


def test_archived_forecasts_and_all_four_conditional_pick_paths_recompute_without_account_access(archive):
    db, source_id, root, _, _ = archive

    def authorize(action, table, column, *args):
        if action == sqlite3.SQLITE_READ and table != 'experiments':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.conn.set_authorizer(authorize)
    try:
        result = inspect_archive(db, source_id, root=root)
    finally:
        db.conn.set_authorizer(None)
    row = result['rows'][0]
    assert row['forecasts']['selected_rows'] > 0
    assert all(value['buy_execution_rows'] == 0 and value['attribution_complete'] for value in row['entries'].values())
    assert result['forecast_rows_recomputed'] and result['conditional_picks_recomputed']
    assert not result['model_retrained'] and not result['execution_replayed']
    assert not result['accepted_for_signals'] and result['probability_of_profit'] is None


def test_rewritten_feature_hash_does_not_override_registered_input_binding(archive):
    db, source_id, root, folder, payload = archive
    payload['artifacts_sha256']['features.parquet'] = '0'*64
    folder.with_suffix('.json').write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='registered source input'):
        inspect_archive(db, source_id, root=root)


@pytest.mark.parametrize('artifact', ['forecasts', 'conditional-2d-picks'])
def test_self_consistent_hash_cannot_substitute_for_recomputed_forecasts_or_picks(archive, artifact):
    db, source_id, root, folder, payload = archive
    name = 'fold-00-'+artifact+'.parquet'
    frame = pd.read_parquet(folder/name)
    if artifact == 'forecasts':
        frame.loc[0, 'normal_expected_net'] += .001
    else:
        frame.loc[0, 'rank'] += 1
    frame.to_parquet(folder/name, index=False)
    payload['artifacts_sha256'][name] = sha256(folder/name)
    folder.with_suffix('.json').write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(AssertionError):
        inspect_archive(db, source_id, root=root)


def test_sealed_fold_is_rejected_before_any_numerical_parquet_read(archive, monkeypatch):
    db, source_id, root, folder, payload = archive
    payload['protocol']['folds'][0]['test_end_exclusive'] = '2025-03-22'
    folder.with_suffix('.json').write_text(json.dumps(payload), encoding='utf-8')
    (folder/'protocol.json').write_text(json.dumps(payload['protocol']), encoding='utf-8')
    reads = []
    monkeypatch.setattr('research.expectancy_funnel.read_development_parquet', lambda *a, **k: reads.append(a))
    with pytest.raises(ValueError, match='sealed folds'):
        inspect_archive(db, source_id, root=root)
    assert reads == []
