"""داده مصنوعی فقط در آزمون؛ علیت، سود خالص و وزن برابر روزها را می‌آزماید."""

import json
import sqlite3
import zipfile

import numpy as np
import pandas as pd
import pytest

from config import settings
from core.probability import outcome_expectancy
from ops.research_bundle import export_research_bundle
from ops.research_review import read_development_parquet, sha256
from research.expectancy import PREDICTORS, fit_expectancy, fixed_hypothesis_masks, forecast_expectancy
from research.expectancy_run import expected_modes, review_expectancy, run_expectancy

pytest_plugins = ['tests.test_flow_research']


def sample():
    rng = np.random.default_rng(23)
    x = pd.DataFrame(rng.uniform(-.05, .05, (360, len(PREDICTORS))), columns=PREDICTORS)
    x['date'] = np.repeat(pd.date_range('2023-01-01', periods=120).strftime('%Y-%m-%d'), 3)
    x['symbol'] = ['الف', 'ب', 'ج'] * 120
    y = x[['date', 'symbol']].copy()
    y['normal_net'] = x.rel_ret_5d * .5 - .002
    y['stress_net'] = y.normal_net - .002
    y['state'], y['resolved_at'] = 'complete', '2023-05-02'
    return x, y


def test_conditional_predictions_distinguish_stock_context_without_future_outcomes():
    x, y = sample()
    model = fit_expectancy(x, y, train_end_exclusive='2023-05-03')
    test = x.iloc[:2].copy()
    test['date'] = '2023-05-04'
    test['rel_ret_5d'] = [-.04, .04]
    predicted = forecast_expectancy(test, model)
    assert model['status'] == 'fitted' and model['probability_of_profit'] is None
    assert predicted.normal_expected_net.iloc[0] < 0 < predicted.normal_expected_net.iloc[1]
    assert (predicted.stress_expected_net < predicted.normal_expected_net).all()
    test['net_ret_2d'] = 999.
    with pytest.raises(ValueError, match='نتیجه'):
        forecast_expectancy(test, model)
    y.loc[0, 'resolved_at'] = '2023-05-03'
    with pytest.raises(ValueError, match='خروج آموزش'):
        fit_expectancy(x, y, train_end_exclusive='2023-05-03')


def test_more_symbols_on_one_day_do_not_overweight_that_day():
    x, y = sample()
    first = fit_expectancy(x, y, train_end_exclusive='2023-05-03')
    extra_x, extra_y = [], []
    for i in range(15):
        a, b = x.iloc[:3].copy(), y.iloc[:3].copy()
        a['symbol'] += str(i)
        b['symbol'] += str(i)
        extra_x.append(a)
        extra_y.append(b)
    second = fit_expectancy(pd.concat([x, *extra_x], ignore_index=True), pd.concat([y, *extra_y], ignore_index=True), train_end_exclusive='2023-05-03')
    np.testing.assert_allclose(first['coefficients'], second['coefficients'], atol=1e-12)


def test_unknown_and_out_of_support_inputs_are_not_imputed_or_given_a_base_forecast():
    x, y = sample()
    x['buyer_power_5d'] = 2.
    model = fit_expectancy(x, y, train_end_exclusive='2023-05-03')
    test = x.iloc[:3].copy()
    test['date'] = '2023-05-04'
    test.loc[0, 'ret_5d'] = np.nan
    test.loc[1, 'buyer_power_5d'] = 3.
    result = forecast_expectancy(test, model)
    assert result.status.tolist() == ['unknown_features', 'outside_training_support', 'forecasted']
    assert result.normal_expected_net.iloc[:2].isna().all()
    y.loc[0, ['normal_net', 'stress_net', 'resolved_at']] = [np.nan, np.nan, None]
    y.loc[0, 'state'] = 'unknown'
    unknown = fit_expectancy(x, y, train_end_exclusive='2023-05-03')
    assert unknown['unresolved_outcomes'] == 1 and unknown['fitted_rows'] == 359
    too_small = fit_expectancy(x.iloc[:3], y.iloc[:3], train_end_exclusive='2023-05-03')
    assert too_small['coefficients'] is None
    assert forecast_expectancy(test, too_small).normal_expected_net.isna().all()


@pytest.mark.parametrize('column,value', [('ret_5d', True), ('ret_1d', '0.01'), ('rel_ret_5d', float('inf'))])
def test_invalid_feature_types_fail_closed(column, value):
    x, y = sample()
    x[column] = x[column].astype(object)
    x.loc[0, column] = value
    with pytest.raises(ValueError):
        fit_expectancy(x, y, train_end_exclusive='2023-05-03')


def test_time_exit_tail_can_destroy_a_high_target_frequency_and_hold_has_no_second_buy_fee():
    scenarios = [dict(name='target', probability=.7, exit_rial=10300),
                 dict(name='time', probability=.2, exit_rial=9800),
                 dict(name='late', probability=.1, exit_rial=8500)]
    result = outcome_expectancy(outcomes=scenarios, entry_rial=10000, exit_now_rial=10000, exit_now_executable=True)
    assert result['expected_entry_net_per_share_rial'] < 0
    more_expensive_original_entry = outcome_expectancy(outcomes=scenarios, entry_rial=12000,
        exit_now_rial=10000, exit_now_executable=True)
    assert result['expected_hold_advantage_per_share_rial'] == more_expensive_original_entry['expected_hold_advantage_per_share_rial']
    assert result['expected_entry_net_per_share_rial'] != more_expensive_original_entry['expected_entry_net_per_share_rial']
    blocked = outcome_expectancy(outcomes=scenarios, exit_now_rial=10000, exit_now_executable=False)
    assert blocked['expected_hold_advantage_per_share_rial'] is None and not blocked['accepted_for_signals']
    scenarios[2]['probability'] = None
    unknown = outcome_expectancy(outcomes=scenarios, entry_rial=10000)
    assert unknown['unresolved_distribution'] and unknown['expected_entry_net_return'] is None


@pytest.mark.parametrize('rows', [
    [dict(name='target', probability=.7, exit_rial=10300)],
    [dict(name='target', probability=1.1, exit_rial=10300)],
    [dict(name='target', probability=True, exit_rial=10300)],
    [dict(name='x', probability=.5, exit_rial=10300), dict(name='x', probability=.5, exit_rial=9800)]])
def test_incomplete_or_incoherent_probability_mass_is_not_a_two_outcome_win(rows):
    with pytest.raises(ValueError):
        outcome_expectancy(outcomes=rows, entry_rial=10000)


def test_parquet_review_checks_sealed_dates_before_reading_numeric_values_and_uses_same_bytes(tmp_path, monkeypatch):
    path = tmp_path / 'curve.parquet'
    pd.DataFrame(dict(date=['2025-03-21'], equity_rial=[999.])).to_parquet(path)
    original, calls = pd.read_parquet, []
    def watched(*args, **kwargs):
        calls.append(kwargs.get('columns'))
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, 'read_parquet', watched)
    with pytest.raises(ValueError, match='مهروموم'):
        read_development_parquet(path, sha256(path), '2025-03-21')
    assert calls == [['date']]
    valid = pd.DataFrame(dict(date=['2024-01-01'], equity_rial=[100.]))
    valid.to_parquet(path)
    def replaced(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('columns') == ['date']:
            pd.DataFrame(dict(date=['2025-03-21'], equity_rial=[999.])).to_parquet(path)
        return result
    monkeypatch.setattr(pd, 'read_parquet', replaced)
    pd.testing.assert_frame_equal(read_development_parquet(path, sha256(path), '2025-03-21'), valid)


def test_pipeline_freezes_models_and_picks_then_replays_all_controls_and_reviews(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research.run import run_research

    _, base = flow_baseline
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    for symbol in ('الف', 'ب', 'ج'):
        sid = db.get_symbol(symbol)['id']
        p, c = db.get_daily_prices_df(symbol), db.get_client_types_df(symbol)
        price = 1000 * 1.02 ** np.arange(len(p))
        p['open_raw'], p['low_raw'], p['high_raw'] = price*.996, price*.99, price*1.01
        p['close_raw'], p['close_final_raw'], p['ref_price_raw'] = price, price, price/1.02
        p['volume'], p['value'] = 100_000_000., 100_000_000.*price
        c = c.merge(p[['date', 'value']].rename(columns={'value': 'market_value'}), on='date')
        c['buy_i_value'], c['sell_i_value'] = c.market_value*.8, c.market_value*.5
        db.upsert_daily_prices_df(sid, p)
        db.upsert_client_types_df(sid, c)
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    order, original = [], db.log_experiment
    def registered(*args, **kwargs):
        order.append(args[0] if args else kwargs['experiment_id'])
        return original(*args, **kwargs)
    monkeypatch.setattr(db, 'log_experiment', registered)
    result = run_expectancy(db, source_id=source['experiment_id'], trials_attempted=100, replications=1000,
        min_train_sessions=104, test_sessions=30)
    assert len(expected_modes()) == 35 and result['protocol']['primary_candidate'] == 'conditional-2d'
    assert order == [result['experiment_id']+'-PROTOCOL', result['experiment_id']]
    assert all(set(row['modes']) == expected_modes() for row in result['results'])
    folder = base / 'research' / result['experiment_id']
    forecasts = list(folder.glob('fold-*-forecasts.parquet'))
    assert forecasts and any(pd.read_parquet(p).status.eq('forecasted').any() for p in forecasts)
    assert sum(r['modes']['conditional-2d']['metrics']['executions'] for r in result['results']) > 0
    for index in range(len(result['results'])):
        selected = pd.read_parquet(folder / f'fold-{index:02d}-conditional-2d-picks.parquet')
        for seed in (41, 97, 173):
            control = pd.read_parquet(folder / f'fold-{index:02d}-conditional-2d-random{seed}-picks.parquet')
            assert selected.groupby('date').size().to_dict() == control.groupby('date').size().to_dict()
    checked = review_expectancy(db, result['experiment_id'])
    assert checked['curve_metrics_recomputed'] and checked['model_retrained_by_review'] and checked['forecasts_and_errors_recomputed']
    assert not (base / 'research' / 'holdout-unsealed.json').exists()
    report_file = base / 'research' / (result['experiment_id']+'.json')
    original_report = report_file.read_bytes()
    checkpoint_file = folder / 'fold-00-checkpoint.json'
    original_checkpoint = checkpoint_file.read_bytes()
    assert len(result['checkpoints_sha256']) == len(result['results'])
    changed_checkpoint = json.loads(original_checkpoint)
    changed_checkpoint['prediction_error']['fold'] = 1000
    checkpoint_file.write_text(json.dumps(changed_checkpoint), encoding='utf-8')
    changed_report = json.loads(original_report)
    changed_report['checkpoints_sha256'][checkpoint_file.name] = sha256(checkpoint_file)
    report_file.write_text(json.dumps(changed_report), encoding='utf-8')
    with pytest.raises(ValueError, match='نتیجه میانی'):
        review_expectancy(db, result['experiment_id'])
    checkpoint_file.write_bytes(original_checkpoint)
    report_file.write_bytes(original_report)
    # تغییر هم زمان دو افق و تازه کردن هش نباید انتخاب نامرتبط را معتبر کند.
    picks_files = [folder / f'fold-00-conditional-{h}d-picks.parquet' for h in (1, 2)]
    originals = {file: file.read_bytes() for file in picks_files}
    changed_report = json.loads(original_report)
    for file in picks_files:
        rows = pd.read_parquet(file)
        assert not rows.empty
        rows['rank'] += 1000
        rows.to_parquet(file, index=False)
        changed_report['artifacts_sha256'][file.name] = sha256(file)
    report_file.write_text(json.dumps(changed_report), encoding='utf-8')
    with pytest.raises(ValueError, match='انتخاب محفوظ'):
        review_expectancy(db, result['experiment_id'])
    for file, contents in originals.items():
        file.write_bytes(contents)
    report_file.write_bytes(original_report)
    uncertainty_file = folder / 'uncertainty.json'
    original_uncertainty = uncertainty_file.read_bytes()
    changed_uncertainty = json.loads(original_uncertainty)
    changed_uncertainty[0]['result']['trials_attempted'] += 1
    uncertainty_file.write_text(json.dumps(changed_uncertainty), encoding='utf-8')
    changed_report = json.loads(original_report)
    changed_report['uncertainty_sha256'] = sha256(uncertainty_file)
    report_file.write_text(json.dumps(changed_report), encoding='utf-8')
    with pytest.raises(ValueError, match='عدم قطعیت'):
        review_expectancy(db, result['experiment_id'])
    uncertainty_file.write_bytes(original_uncertainty)
    report_file.write_bytes(original_report)
    model_file = sorted(folder.glob('fold-*-model.json'))[0]
    original_model = model_file.read_bytes()
    changed_model = json.loads(original_model)
    changed_model['coefficients'][0][0] += .01
    model_file.write_text(json.dumps(changed_model), encoding='utf-8')
    changed_report = json.loads(original_report)
    changed_report['models_sha256'][model_file.name] = sha256(model_file)
    report_file.write_text(json.dumps(changed_report), encoding='utf-8')
    with pytest.raises(ValueError, match='مدل با برازش مجدد'):
        review_expectancy(db, result['experiment_id'])
    model_file.write_bytes(original_model)
    report_file.write_bytes(original_report)
    with (folder / 'prediction-errors.json').open('a') as handle:
        handle.write(' ')
    with pytest.raises(ValueError, match='گزارش جانبی'):
        review_expectancy(db, result['experiment_id'])


def test_market_export_reads_no_account_tables_and_exports_only_allowlisted_files(db, flow_baseline):
    source, base = flow_baseline
    def guard(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table != 'experiments' else sqlite3.SQLITE_OK
    db.conn.set_authorizer(guard)
    try:
        result = export_research_bundle(db, source['experiment_id'])
    finally:
        db.conn.set_authorizer(None)
    with zipfile.ZipFile(result['path']) as archive:
        names = archive.namelist()
        assert len(names) == 7 and all(not n.endswith(('.db', '.py', '.env')) for n in names)
        manifest = json.loads(archive.read('bundle-manifest.json'))
        assert not manifest['account_tables_included']
        assert len(manifest['files_sha256']) == 6
    with pytest.raises(ValueError, match='ZIP جدید'):
        export_research_bundle(db, source['experiment_id'], output=result['path'])
    with pytest.raises(ValueError):
        export_research_bundle(db, source['experiment_id'], output=base / 'outside.zip')


def test_invalid_expectancy_cli_cannot_create_an_empty_database(tmp_path, monkeypatch):
    from research.expectancy_run import main

    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'must-not-exist.db')
    with pytest.raises(ValueError, match='منبع BASELINES'):
        main([])
    assert not settings.DB_PATH.exists()


def test_empty_development_fold_set_cannot_register_an_experiment(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research.flow_v4_run import run_flow_v4
    from research.probability_run import run_probability_research
    from research.run import run_research

    _, base = flow_baseline
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    before = db.conn.total_changes
    with pytest.raises(ValueError, match='جلسه کافی'):
        run_flow_v4(db, source_id=source['experiment_id'], min_train_sessions=10000)
    with pytest.raises(ValueError, match='جلسه کافی'):
        run_probability_research(db, source_id=source['experiment_id'])
    assert db.conn.total_changes == before
    assert not (base / 'research' / 'latest-flow-v4.json').exists()


def test_v4_random_control_shortfall_stops_instead_of_silently_dropping_signals(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research import flow_v4_run
    from research.run import run_research

    _, base = flow_baseline
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    def selected(metrics, diagnostic, k):
        chosen = metrics[['date', 'symbol']].drop_duplicates('date').copy()
        chosen['rank'] = 0
        return chosen
    original = flow_v4_run._picks
    def short_pool(panel, strategy, k, random_seed=None):
        chosen = original(panel, strategy, k, random_seed)
        return chosen.iloc[:0] if random_seed is not None else chosen
    monkeypatch.setattr(flow_v4_run, '_rank_diagnostics', selected)
    monkeypatch.setattr(flow_v4_run, '_picks', short_pool)
    with pytest.raises(ValueError, match='کنترل تصادفی'):
        flow_v4_run.run_flow_v4(db, source_id=source['experiment_id'], min_train_sessions=104, test_sessions=30, replications=1000)
    assert not (base / 'research' / 'latest-flow-v4.json').exists()


def test_market_export_rejects_extra_nonmarket_columns_even_with_updated_input_hash(db, flow_baseline):
    from research.run import _sha256

    source, base = flow_baseline
    root, identity = base / 'research', source['experiment_id']
    path = root / identity / 'features.parquet'
    frame = pd.read_parquet(path)
    frame['private_probe'] = 'test-only-column'
    frame.to_parquet(path, index=False)
    report = json.loads((root / (identity + '.json')).read_text(encoding='utf-8'))
    report['params']['features']['sha256'] = _sha256(path)
    (root / (identity + '.json')).write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    with pytest.raises(ValueError, match='allowlist'):
        export_research_bundle(db, identity)
    assert not (base / 'private' / 'research-exports').exists()


def test_market_export_projects_metadata_instead_of_copying_private_free_text(db, flow_baseline, tmp_path):
    from research.portfolio_run import verified_baseline_inputs

    source, base = flow_baseline
    identity = source['experiment_id']
    report_file = base / 'research' / (identity + '.json')
    report = json.loads(report_file.read_text(encoding='utf-8'))
    needle = 'private-test-only-not-a-market-field'
    report['params']['research_provenance']['owner_note'] = needle
    report_file.write_text(json.dumps(report), encoding='utf-8')
    with db.conn:
        db.conn.execute('UPDATE experiments SET parameters_json=? WHERE experiment_id=?',
                        (json.dumps(report['params']), identity))
    result = export_research_bundle(db, identity)
    extracted = tmp_path / 'exported'
    with zipfile.ZipFile(result['path']) as archive:
        for name in archive.namelist():
            if name.endswith('.json'):
                assert needle not in archive.read(name).decode('utf-8')
        archive.extractall(extracted)
        manifest = json.loads(archive.read('bundle-manifest.json'))
        assert not manifest['arbitrary_source_metadata_included']
        assert not manifest['contains_local_research_metadata_paths']
        assert not manifest['full_original_report_or_registration']
    sid, frames, hashes = verified_baseline_inputs(extracted, identity)
    assert sid == identity and set(frames) == set(hashes) == {'features', 'labels', 'prices'}


@pytest.mark.parametrize('value,volume,excluded', [(0., 0., True), (np.nan, 0., False), (0., np.nan, False), (100., 0., False), (100., 1., False)])
def test_only_verified_zero_value_zero_volume_is_nontrading_not_an_unknown_outcome(value, volume, excluded):
    from research.probability import opportunity_features

    features = pd.DataFrame(dict(symbol=['الف', 'ب'], date=['2024-01-01'] * 2, ret_5d=[.1, .2]))
    labels = pd.DataFrame(dict(symbol=['الف'], date=['2024-01-01'], value=[100.]))
    prices = pd.DataFrame(dict(symbol=['الف', 'ب'], date=['2024-01-01'] * 2, value=[100., value], volume=[1., volume]))
    if excluded:
        selected = opportunity_features(features, labels, prices=prices)
        assert selected.symbol.tolist() == ['الف']
        assert selected.attrs['observed_nontrading_feature_rows'] == 1
    else:
        with pytest.raises(ValueError, match='برچسب'):
            opportunity_features(features, labels, prices=prices)


def test_unknown_raw_lock_state_does_not_pass_even_when_legacy_flag_is_zero():
    frame = pd.DataFrame(dict(symbol=['الف'], date=['2024-01-01'], mkt_breadth_5d=[.6], dist_sma_20d=[.02],
        buyer_power_5d=[1.5], rel_ret_5d=[.03], ret_3d=[-.01], close_position=[.8], locked_up=[0]))
    prices = pd.DataFrame(dict(symbol=['الف'], date=['2024-01-01'], open_raw=[100.], low_raw=[99.], high_raw=[101.],
        close_raw=[100.], ref_price_raw=[100.], volume=[1000.]))
    assert all(mask.iloc[0] for mask in fixed_hypothesis_masks(frame, prices).values())
    prices['close_raw'] = np.nan
    assert all(not mask.iloc[0] for mask in fixed_hypothesis_masks(frame, prices).values())
