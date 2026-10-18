"""تمام داده‌های ساختگی این پرونده فقط آزمون و در حافظه/پوشه و دیتابیس موقت هستند."""

import json
import math

import numpy as np
import pandas as pd
import pytest

from config import settings
from core.probability import scenario_expectancy
from research import probability_run
from research.baselines import Strategy, evaluate_picks, market_benchmark, run_baselines
from research.portfolio_run import verified_baseline_inputs
from research.probability import (
    fit_frequency,
    forecast_opportunities,
    opportunity_features,
    resolved_outcomes,
    score_forecasts,
    select_opportunities,
)


def labels():
    return pd.DataFrame(dict(date=['2023-01-01', '2023-01-02', '2023-01-03', '2023-01-04'],
        symbol=['a', 'b', 'c', 'd'], entry_date=['2023-01-02', '2023-01-03', '2023-01-04', '2023-01-05'],
        entry_executable=[True, False, True, True], label_status_1d=['complete', 'entry_blocked', 'censored', 'complete'],
        exit_date_1d=['2023-01-03', '', '', '2023-02-01'], net_ret_1d=[.04, np.nan, np.nan, .1],
        net_ret_1d_worst=[.02, np.nan, np.nan, -.05], corporate_actions_1d=[0., np.nan, np.nan, 1.], value=1000.))


def ledger():
    frame = resolved_outcomes(labels().iloc[:3], 1, '2023-01-10')
    frame['normal_probability'] = [.8, .8, .2]
    frame['normal_base_probability'] = .5
    return frame


def test_actual_label_resolution_purges_late_exit_and_keeps_known_entry():
    result = resolved_outcomes(labels(), 1, '2023-01-10')
    assert result.state.tolist() == ['complete', 'entry_blocked', 'unknown', 'unknown']
    assert result.entry_state.tolist() == ['filled', 'blocked', 'filled', 'filled']
    assert result.normal_net.iloc[:2].tolist() == [.04, 0.]
    assert result.normal_net.iloc[2:].isna().all()
    fitted = fit_frequency(result, 'normal', min_sessions=2)
    assert fitted['probability'] == .5 and fitted['unresolved'] == 2
    assert fitted['empirical_success_fraction_lower'] == .25
    assert fitted['empirical_success_fraction_upper'] == .75
    assert fitted['max_resolved_at'] == '2023-01-03'
    changed = labels()
    changed.loc[3, 'net_ret_1d'] = 9.
    assert fit_frequency(resolved_outcomes(changed, 1, '2023-01-10'), 'normal', min_sessions=2) == fitted


def test_resolution_must_be_strictly_before_boundary():
    outcome = resolved_outcomes(labels().iloc[:2], 1, '2023-01-03')
    assert outcome.state.eq('unknown').all()
    assert outcome.entry_state.tolist() == ['filled', 'unknown']
    assert fit_frequency(outcome, 'normal', min_sessions=1)['probability'] is None


@pytest.mark.parametrize(('field', 'value'), [('entry_date', '2023-01-01'), ('entry_executable', False),
    ('exit_date_1d', '2023-01-02'), ('net_ret_1d_worst', .5), ('net_ret_1d', -1.1),
    ('corporate_actions_1d', .5), ('label_status_1d', 'invented'), ('date', '2023-1-1'),
    ('entry_executable', 1), ('net_ret_1d', float('inf')), ('net_ret_1d', '.04')])
def test_incoherent_complete_labels_are_rejected(field, value):
    frame = labels().iloc[:1].astype({field: object})
    frame.loc[0, field] = value
    with pytest.raises(ValueError):
        resolved_outcomes(frame, 1, '2023-02-01')


def test_blocked_entry_cannot_carry_a_numeric_profit():
    frame = labels().iloc[1:2].copy()
    frame.loc[frame.index[0], 'net_ret_1d'] = .2
    with pytest.raises(ValueError, match='عدم ورود'):
        resolved_outcomes(frame, 1, '2023-02-01')


def test_no_history_or_too_few_sessions_never_becomes_a_default_probability():
    outcome = resolved_outcomes(labels(), 1, '2023-01-10')
    model = fit_frequency(outcome, 'normal')
    assert model['probability'] is None and model['mean_net_return'] is None
    assert model['status'] == 'insufficient_resolved_history'
    assert fit_frequency(outcome.iloc[:0], 'normal')['probability'] is None


def test_brier_scores_against_past_base_rate_and_unknown_bounds_match_hand_calculation():
    result = score_forecasts(ledger(), 'normal')
    assert result['opportunities'] == 3 and result['resolved'] == 2 and result['unresolved'] == 1
    assert result['blocked_entries'] == 1 and result['losses'] == 0 and result['zero_returns'] == 1
    assert result['proxy_entry_fill_rate'] == pytest.approx(2/3)
    assert result['profit_frequency_completed_trades'] == 1
    assert result['empirical_success_frequency_resolved'] == .5
    assert result['empirical_success_fraction_bounds'] == dict(lower=1/3, upper=2/3)
    assert result['brier_resolved'] == pytest.approx(.34)
    assert result['paired_base_brier'] == .25
    assert result['brier_skill'] == pytest.approx(-.36)
    assert result['brier_all_forecast_bounds']['lower'] == pytest.approx(.24)
    assert result['brier_all_forecast_bounds']['upper'] == pytest.approx(.44)
    assert result['all_opportunity_mean_net_return'] is None
    assert result['daily_brier_advantage'][-1]['mean_brier_advantage'] is None
    assert not result['unknown_bounds_are_confidence_intervals'] and not result['accepted_for_signals']


def test_calibration_bins_keep_zero_one_unknown_and_no_forecast():
    frame = ledger()
    frame['normal_probability'] = [0., 1., np.nan]
    result = score_forecasts(frame, 'normal')
    assert result['forecasts'] == 2 and result['no_forecast'] == 1
    assert result['brier_resolved'] == 1
    assert result['calibration'][0]['forecasts'] == 1
    assert result['calibration'][-1]['forecasts'] == 1
    assert all(r['mean_probability'] is None for r in result['calibration'][1:-1])
    frame['normal_probability'] = np.nan
    result = score_forecasts(frame, 'normal')
    assert result['brier_resolved'] is None and result['brier_skill'] is None
    assert result['brier_all_forecast_bounds'] is None


@pytest.mark.parametrize('value', [1.1, -.1, True, '0.8', 'bad', float('inf')])
def test_probability_contract_does_not_silently_cast_corrupt_forecasts(value):
    frame = ledger().astype({'normal_probability': object})
    frame.loc[0, 'normal_probability'] = value
    with pytest.raises(ValueError):
        score_forecasts(frame, 'normal')


def test_future_labels_never_enter_selection_or_frozen_predictions():
    raw = labels()
    features = raw[['date', 'symbol']].assign(ret_5d=[.1, .2, .3, .4])
    clean = opportunity_features(features, raw)
    selected = select_opportunities(clean, 'momentum_5d', 1)
    changed = raw.assign(net_ret_1d=-.99)
    pd.testing.assert_frame_equal(selected, select_opportunities(opportunity_features(features, changed), 'momentum_5d', 1))
    outcome = resolved_outcomes(raw, 1, '2023-01-10')
    models = {mode: fit_frequency(outcome, mode, min_sessions=2) for mode in ('normal', 'stress')}
    future = pd.DataFrame(dict(date=['2023-03-01'], symbol=['a'], _score=[.2]))
    frozen = forecast_opportunities(future, models, models, fold_index=0, horizon=1, strategy='momentum_5d',
                                    train_end_exclusive='2023-01-10')
    assert frozen.normal_probability.tolist() == [.5]
    models['normal']['max_resolved_at'] = '2023-01-10'
    with pytest.raises(ValueError, match='مرز زمانی'):
        forecast_opportunities(future, models, models, fold_index=0, horizon=1, strategy='momentum_5d',
                               train_end_exclusive='2023-01-10')


def test_missing_labels_duplicate_keys_or_outcomes_in_feature_table_are_rejected():
    raw = labels()
    features = raw[['date', 'symbol']].assign(ret_5d=.1)
    for bad_features, bad_labels in ((features, raw.iloc[1:]), (pd.concat([features, features]), raw),
                                     (features.assign(net_ret_1d=.1), raw)):
        with pytest.raises(ValueError):
            opportunity_features(bad_features, bad_labels)


def test_manual_probability_is_unknown_until_supplied_and_costs_raise_required_success_rate():
    result = scenario_expectancy(entry_rial=10000, stop_rial=9800, target_rial=10300)
    assert result['break_even_target_probability'] > .4
    assert result['expected_net_return'] is None and result['probability_of_real_trade_profit'] is None
    assert not result['accepted_for_signals'] and not result['stop_guaranteed']
    balanced = scenario_expectancy(entry_rial=10000, stop_rial=9800, target_rial=10300,
                                  target_probability=result['break_even_target_probability'])
    assert balanced['expected_net_return'] == pytest.approx(0, abs=1e-14)


def test_high_win_rate_does_not_imply_positive_expectancy():
    result = scenario_expectancy(entry_rial=10000, stop_rial=8000, target_rial=10200, target_probability=.8)
    assert result['expected_net_return'] < 0 and result['break_even_target_probability'] > .8
    poor = scenario_expectancy(entry_rial=10000, stop_rial=9800, target_rial=10050, target_probability=1.)
    assert poor['expected_net_return'] < 0 and poor['break_even_target_probability'] is None


@pytest.mark.parametrize('value', [True, np.bool_(True), -1., 1.01, float('nan'), float('inf'), '0.5'])
def test_invalid_assumed_probability_is_rejected(value):
    with pytest.raises(ValueError):
        scenario_expectancy(entry_rial=100, stop_rial=98, target_rial=103, target_probability=value)


def test_scenario_rejects_overflowing_cost_arithmetic():
    with pytest.raises(ValueError, match='دامنه محاسبه'):
        scenario_expectancy(entry_rial=1.795e308, stop_rial=1.7e308, target_rial=1.797e308)


def test_market_entry_fill_rate_is_not_completed_exit_rate():
    frame = labels().iloc[:3].copy()
    market = market_benchmark(frame, 1)
    result = run_baselines(frame, [1], 1, 1, strategies=[Strategy('test', 'test', lambda f: f.value)])['1']['market']
    assert result['fill_rate'] == pytest.approx(2/3)  # نه یک معامله بسته‌شده از سه فرصت
    assert result['filled_entries'] == 2 and result['unknown_entries'] == 0
    frame.loc[2, 'label_status_1d'] = 'invalid_data'
    frame.loc[2, 'entry_executable'] = False
    result = evaluate_picks(frame, market, 1, 1)
    assert result['fill_rate'] == .5 and result['unknown_entries'] == 1


def test_corrupt_numeric_return_for_blocked_or_invalid_entry_cannot_boost_baseline():
    frame = labels().iloc[1:2].copy()
    frame['net_ret_1d'] = 9.
    market = market_benchmark(frame, 1)
    assert market.isna().all()
    result = evaluate_picks(frame, market, 1, 1)
    assert result['trades'] == 0 and result['unknown_cohorts'] == 1 and math.isnan(result['mean_trade'])


@pytest.fixture
def probability_source(db, tmp_path, monkeypatch):
    from features import store
    from research import labels as label_module
    from research import run
    from tests import test_features

    monkeypatch.setattr(test_features, 'N_DAYS', 790)
    for i, name in enumerate(('a', 'b', 'c')):
        prices, clients = test_features.synthetic_history(seed=i)
        dates = pd.bdate_range('2020-01-01', periods=790).strftime('%Y-%m-%d')
        mapping = dict(zip(prices.date, dates))
        prices['date'], clients['date'] = prices.date.map(mapping), clients.date.map(mapping)
        sid = db.upsert_symbol(dict(symbol_fa=name, ins_code=str(i), instrument_type_code=300))
        db.upsert_daily_prices_df(sid, prices)
        db.upsert_client_types_df(sid, clients)
    root = tmp_path / 'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    monkeypatch.setattr(store, 'FEATURES_ROOT', root / 'features')
    monkeypatch.setattr(run, 'RESEARCH_ROOT', root / 'research')
    monkeypatch.setattr(run, 'LABELS_ROOT', root / 'labels')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'dev_start', '2020-01-01')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2024-01-01')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'min_daily_value_rial', 0)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'top_k', 2)
    monkeypatch.setattr(label_module, 'CALENDAR_MIN_SYMBOLS', 2)
    store.build_feature_store(db)
    source = run.run_research(db, random_trials=1)
    return root / 'research', source['experiment_id']


def test_registered_probability_run_freezes_forecasts_before_outcomes_and_keeps_all_rules(db, probability_source, monkeypatch):
    root, source = probability_source
    original = probability_run.fit_frequency
    counts = db.table_counts()

    def registered_first(*args, **kwargs):
        records = db.get_experiments()
        assert records[0]['experiment_id'].endswith('-PROTOCOL')
        assert records[0]['strategy_name'] == 'probability-protocol'
        return original(*args, **kwargs)

    monkeypatch.setattr(probability_run, 'fit_frequency', registered_first)
    report = probability_run.run_probability_research(db, source_id=source, root=root)
    assert len(report['summary']) == 16 and report['model_records'] == 16
    assert not report['accepted_for_signals'] and not report['protocol']['holdout_opened']
    assert report['protocol']['reported_scenario_comparisons'] == 32
    assert report['probability_of_real_trade_profit'] is None
    assert {k: v for k, v in db.table_counts().items() if k != 'experiments'} == {k: v for k, v in counts.items() if k != 'experiments'}
    folder = root / report['experiment_id']
    forecasts = pd.read_parquet(folder / 'fold-00-forecasts.parquet')
    assert not {'normal_net', 'stress_net', 'state', 'resolved_at'} & set(forecasts)
    assert forecasts.normal_probability.notna().any()
    assert report['protocol']['prospective_independence'] is False
    verified = probability_run.load_probability_report(db, report['experiment_id'], root=root)
    assert verified['summary'] == report['summary'] and verified['scores_recomputed']
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)

    def probability_page():
        from dashboard.probability import page_probability
        page_probability(lambda *args: None)

    app = AppTest.from_function(probability_page, default_timeout=60).run()
    assert not app.exception and not app.error
    assert any('پژوهشی' in w.value for w in app.warning)
    for field, value in zip(app.number_input[:3], (1000., 980., 1030.)):
        field.set_value(value)
    app.button[0].click().run()
    assert not app.exception
    assert any('احتمال وارد نشده' in item.value for item in app.info)
    app.radio[0].set_value('خرید در سقف / فروش در کف').run()
    assert not app.exception and not app.error
    assert any(m.label == 'بازده خالص در هدف' for m in app.metric)
    with (folder / 'fold-00-models.json').open('a', encoding='utf-8') as stream:
        stream.write('\n')
    with pytest.raises(ValueError, match='شواهد'):
        probability_run.load_probability_report(db, report['experiment_id'], root=root)


def test_code_change_or_failed_registration_never_publishes_probability_success(db, probability_source, monkeypatch):
    root, source = probability_source
    fingerprints = iter(['before', 'after'])
    monkeypatch.setattr(probability_run, 'research_code_fingerprint', lambda: next(fingerprints))
    with pytest.raises(ValueError, match='کد احتمال'):
        probability_run.run_probability_research(db, source_id=source, root=root)
    assert not (root / 'latest-probability.json').exists()
    monkeypatch.setattr(db, 'log_experiment', lambda *a, **k: None)
    monkeypatch.setattr(probability_run, 'research_code_fingerprint', lambda: 'before')
    with pytest.raises(RuntimeError, match='ثبت پروتکل'):
        probability_run.run_probability_research(db, source_id=source, root=root)
    assert not (root / 'latest-probability.json').exists()


@pytest.mark.parametrize('field', ['slippage_bps_each_side', 'lock_min_move_pct'])
def test_old_label_execution_assumptions_cannot_be_relabelled_as_current(probability_source, field):
    root, source = probability_source
    path = root / source / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest[field] *= 2
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='لغزش یا آستانه صف'):
        verified_baseline_inputs(root, source)


def test_sealed_dates_are_rejected_before_value_columns_are_read(probability_source, monkeypatch):
    from research.run import _sha256

    root, source = probability_source
    path = root / source / 'features.parquet'
    frame = pd.read_parquet(path)
    frame.loc[0, 'date'] = settings.RESEARCH_CONFIG['holdout_start']
    frame.to_parquet(path, index=False)
    report_path = root / (source + '.json')
    payload = json.loads(report_path.read_text(encoding='utf-8'))
    payload['params']['features']['sha256'] = _sha256(path)
    report_path.write_text(json.dumps(payload), encoding='utf-8')
    calls = []
    original = pd.read_parquet

    def guarded(*args, **kwargs):
        calls.append(kwargs.get('columns'))
        return original(*args, **kwargs)

    monkeypatch.setattr(pd, 'read_parquet', guarded)
    with pytest.raises(ValueError, match='دوره نهایی'):
        verified_baseline_inputs(root, source)
    assert calls == [['date']]


def test_rehashed_replacement_labels_cannot_bypass_registered_source_generation(probability_source):
    from research.run import _sha256

    root, source = probability_source
    path = root / source / 'labels.parquet'
    frame = pd.read_parquet(path)
    frame.loc[frame.label_status_1d == 'complete', 'net_ret_1d'] += .01
    frame.to_parquet(path, index=False)
    manifest_path = root / source / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['parquet_sha256'] = _sha256(path)
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='نسل برچسب'):
        verified_baseline_inputs(root, source)
