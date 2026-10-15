"""Regression contracts established by the external review audit; isolated temporary data only."""

import ast
import json
import math
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import psutil
import pytest

from config import settings
from core import market_rules
from core.timeutils import to_jalali
from data_collector.monitor import buyer_power
from features import store
from features.daily import compute_daily_features
from research import dip_run
from research.baselines import Strategy, market_benchmark
from research.dip import DipRule, simulate_symbol
from research.labels import compute_labels

CAL = np.array([f'2024-01-{i:02d}' for i in range(1, 9)])

def prices(n=8):
    return pd.DataFrame({'date': CAL[:n], 'open_raw': 100.0, 'high_raw': 101.0, 'low_raw': 99.0, 'close_raw': 100.0, 'close_final_raw': 100.0, 'ref_price_raw': 100.0, 'volume': 1000, 'value': 100000.0, 'trade_count': 10})

def set_day(frame, i, op, high, low, close, ref, vwap=None):
    frame.loc[i, ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = [op, high, low, close, close, ref]
    frame.loc[i, 'value'] = (vwap if vwap is not None else close) * 1000

def client_types(frame):
    return pd.DataFrame({'date': frame['date'], 'buy_i_value': 60000.0, 'sell_i_value': 40000.0, 'buy_i_volume': 600.0, 'sell_i_volume': 400.0, 'buy_i_count': 10, 'sell_i_count': 10, 'buy_n_volume': 400.0, 'sell_n_volume': 600.0})

def dip_stats(statuses):
    picks = pd.DataFrame({'date': CAL[:len(statuses)], 'symbol': 'x'})
    trades = picks.assign(dip_status=statuses, entry_kind='limit', exit_kind='target', days_held=1, net_ret=[0.1 if status == 'complete' else np.nan for status in statuses])
    result = dip_run.evaluate(trades, picks, pd.Index(picks['date']), 1, 1, pd.Series(0.6, index=picks['date']))
    return result

def test_flow_ratio_uses_the_same_observed_days_in_both_sums():
    frame = prices(6)
    ct = client_types(frame).drop(index=[2, 4])
    result = compute_daily_features(frame, ct)['real_flow_ratio_5d'].iloc[-1]
    expected = 3 * 20000 / (3 * 100000)
    assert result == pytest.approx(expected)

def test_flow_ratio_with_complete_observations_is_unchanged():
    frame = prices(6)
    assert compute_daily_features(frame, client_types(frame))['real_flow_ratio_5d'].iloc[-1] == pytest.approx(0.2)


def test_rolling_buyer_power_uses_paired_observations():
    frame = prices(6)
    ct = client_types(frame)
    ct.loc[2, 'sell_i_count'] = np.nan
    ct.loc[2, 'buy_i_count'] = 1000
    result = compute_daily_features(frame, ct)['buyer_power_5d'].iloc[-1]
    assert result == pytest.approx(1.5)


def test_rolling_buyer_power_keeps_a_known_day_without_real_buys():
    frame = prices(6)
    ct = client_types(frame)
    ct.loc[2, ['buy_i_count', 'buy_i_value', 'buy_i_volume']] = 0
    ct.loc[2, 'buy_n_volume'] = 1000
    ct.loc[2, 'sell_i_count'] = 20
    result = compute_daily_features(frame, ct)['buyer_power_5d'].iloc[-1]
    assert result == pytest.approx((4 * 60000 / 40) / (5 * 40000 / 60))


def test_code_change_during_dip_run_never_publishes_success(db, dip_runner, monkeypatch):
    fingerprints = iter(['before', 'before', 'after'])
    monkeypatch.setattr(dip_run, 'research_code_fingerprint', lambda: next(fingerprints))
    with pytest.raises(RuntimeError, match='کد پژوهش'):
        dip_run.run_dip_research(db, rules=[DipRule()])
    assert not (dip_runner / 'latest-dip.json').exists()
    assert db.get_experiments() == []


def test_unknown_corporate_action_gap_is_not_fabricated_and_chart_is_grouped():
    frame = prices(4)
    frame.loc[2, 'ref_price_raw'] = np.nan
    factors = market_rules.corporate_action_factors(frame)
    assert math.isnan(factors.iloc[2])
    adjusted = market_rules.backward_adjusted_close(frame)
    assert adjusted.iloc[:2].isna().all() and adjusted.iloc[-1] == 100
    multiple = pd.concat([prices(4).assign(symbol='a'), prices(4).assign(symbol='b')], ignore_index=True)
    multiple.loc[multiple.symbol == 'b', ['close_raw', 'close_final_raw', 'ref_price_raw']] = 1000
    assert market_rules.backward_adjusted_close(multiple).tolist() == [100] * 4 + [1000] * 4


def test_last_decision_has_unknown_entry_not_known_idle_cash():
    row = simulate_symbol(prices(), CAL, DipRule()).iloc[-1]
    assert row['dip_status'] == 'censored' and math.isnan(row['entry_filled'])


def test_actual_holding_extrema_match_adjusted_prices_after_long_exit_delay():
    frame = prices()
    set_day(frame, 2, 50, 51, 49, 50, 50)
    set_day(frame, 3, 45, 45, 45, 45, 50)
    set_day(frame, 4, 42, 42, 42, 42, 45)
    set_day(frame, 5, 40, 40, 40, 40, 42)
    set_day(frame, 6, 41, 43, 39, 41, 40)
    row = compute_labels(frame.assign(symbol='x'), [2], calendar=CAL).iloc[0]
    assert row['exit_date_2d'] == CAL[6] and row['exit_delay_2d'] == 3
    entry = min(100 * (1 + settings.BACKTEST_CONFIG['default_slippage_bps'] / 10000), 101)
    factors = market_rules.corporate_action_factors(frame)
    multipliers = 1 / factors.iloc[2:7].cumprod()
    extrema_high = [101] + (frame.loc[2:6, 'high_raw'] * multipliers).tolist()
    extrema_low = [99] + (frame.loc[2:6, 'low_raw'] * multipliers).tolist()
    assert row['mfe_2d'] == pytest.approx(max(extrema_high) / entry - 1)
    assert row['mae_2d'] == pytest.approx(min(extrema_low) / entry - 1)

def test_invalid_dip_outcome_is_unknown_instead_of_idle_cash():
    result = dip_stats(['invalid'])
    assert math.isnan(result['mean_daily_cohort'])

def test_missing_dip_result_is_unknown_instead_of_idle_cash():
    picks = pd.DataFrame({'date': [CAL[0]], 'symbol': ['x']})
    trades = pd.DataFrame(columns=['date', 'symbol', 'dip_status', 'net_ret', 'exit_kind', 'days_held'])
    result = dip_run.evaluate(trades, picks, pd.Index([CAL[0]]), 1, 1, pd.Series(0.6, index=[CAL[0]]))
    assert math.isnan(result['mean_daily_cohort'])

def test_censored_exit_can_still_have_a_known_filled_entry():
    result = dip_stats(['complete', 'censored', 'not_filled'])
    assert result['fill_rate'] == pytest.approx(2 / 3)
    assert result['trades'] == 1 and result['censored'] == 1

def test_missing_open_does_not_invent_a_gap_fill_or_same_day_sequence():
    frame = prices()
    set_day(frame, 1, np.nan, 103, 97.5, 100, 100)
    row = simulate_symbol(frame, CAL, DipRule()).iloc[0]
    assert row['dip_status'] == 'invalid' and math.isnan(row['net_ret'])

def test_mae_includes_the_days_spent_waiting_for_an_actual_exit():
    frame = prices()
    set_day(frame, 2, 97, 97, 97, 97, 100)
    set_day(frame, 3, 94.09, 94.09, 94.09, 94.09, 97)
    set_day(frame, 4, 91.2673, 91.2673, 91.2673, 91.2673, 94.09)
    set_day(frame, 5, 90, 92, 88, 90, 91.2673)
    row = compute_labels(frame.assign(symbol='x'), [1], calendar=CAL).iloc[0]
    slip = settings.BACKTEST_CONFIG['default_slippage_bps'] / 10000
    expected = 88 / (100 * (1 + slip)) - 1
    assert row['exit_delay_1d'] == 3
    assert row['mae_1d'] == pytest.approx(expected)

def test_missing_previous_official_close_makes_the_action_factor_unknown():
    frame = prices(3)
    frame.loc[1, 'close_final_raw'] = np.nan
    frame.loc[2, 'ref_price_raw'] = 50.0
    factor = market_rules.corporate_action_factors(frame).iloc[2]
    assert math.isnan(factor)  # A last trade cannot establish the official/reference bridge.

def test_dip_does_not_complete_a_return_through_an_unknown_reference():
    frame = prices()
    set_day(frame, 1, 100, 100.5, 97.5, 98, 100)
    set_day(frame, 2, 90, 91, 89, 90, np.nan)
    set_day(frame, 3, 90, 91, 89, 90, 90)
    row = simulate_symbol(frame, CAL, DipRule(hold=2)).iloc[0]
    label = compute_labels(frame.assign(symbol='x'), [2], calendar=CAL).iloc[0]
    assert label['label_status_2d'] == 'invalid_data'
    assert row['dip_status'] == 'invalid' and math.isnan(row['net_ret'])

def test_labels_do_not_assume_a_known_action_factor_when_both_previous_closes_are_missing():
    frame = prices()
    set_day(frame, 2, 100, 101, 99, np.nan, 100, vwap=100)
    set_day(frame, 3, 50, 51, 49, 50, 50)
    row = compute_labels(frame.assign(symbol='x'), [2], calendar=CAL).iloc[0]
    assert row['label_status_2d'] == 'invalid_data'

def test_full_market_benchmark_does_not_silently_discard_an_unknown_holding():
    panel = pd.DataFrame({'date': [CAL[0]] * 2, 'symbol': ['x', 'y'], 'net_ret_1d': [0.1, np.nan], 'label_status_1d': ['complete', 'censored']})
    value = market_benchmark(panel, 1).iloc[0]
    assert math.isnan(value)

def test_full_market_benchmark_treats_a_known_blocked_entry_as_cash():
    panel = pd.DataFrame({'date': [CAL[0]] * 2, 'symbol': ['x', 'y'], 'net_ret_1d': [0.1, np.nan], 'label_status_1d': ['complete', 'entry_blocked']})
    assert market_benchmark(panel, 1).iloc[0] == pytest.approx(0.05)

@pytest.fixture
def feature_root(db, tmp_path):
    frame = prices()
    sid = db.upsert_symbol({'symbol_fa': 'آزمون', 'ins_code': '1', 'instrument_type_code': 300})
    db.upsert_daily_prices_df(sid, frame)
    root = tmp_path / 'features'
    store.build_feature_store(db, root=root)
    return root

def tamper_feature_file(root):
    target = store.feature_data_path(store.load_manifest(root=root), root=root)
    data = pd.read_parquet(target)
    data.loc[:, 'ret_1d'] = 0.9
    data.to_parquet(target, index=False)

def test_load_feature_store_rejects_a_valid_but_tampered_parquet(feature_root):
    tamper_feature_file(feature_root)
    with pytest.raises(ValueError, match='اثر انگشت'):
        store.load_feature_store(root=feature_root)

def test_symbol_feature_reader_also_rejects_tampered_data(feature_root):
    tamper_feature_file(feature_root)
    with pytest.raises(ValueError, match='اثر انگشت'):
        store.load_symbol_features('آزمون', root=feature_root)

def test_target_sale_is_blocked_on_a_locked_sell_day():
    frame = prices()
    set_day(frame, 1, 100, 106, 97.5, 106, 100)
    set_day(frame, 2, 102.82, 102.82, 102.82, 102.82, 106)
    set_day(frame, 3, 101, 103, 100, 102, 102.82)
    assert market_rules.lock_flags(frame).loc[2, 'locked_down']
    row = simulate_symbol(frame, CAL, DipRule()).iloc[0]
    assert row['days_held'] == 2 and row['exit_kind'] == 'time_late'

def test_action_factors_do_not_cross_symbols():
    panel = pd.concat([prices(2).assign(symbol='a'), prices(2).assign(symbol='b')], ignore_index=True)
    panel.loc[panel['symbol'] == 'b', ['close_raw', 'close_final_raw', 'ref_price_raw']] = 1000.0
    factors = market_rules.corporate_action_factors(panel)
    assert factors.tolist() == pytest.approx([1.0] * 4)

def dashboard_symbol_daily(frame, ct):
    source = (settings.BASE_DIR / 'dashboard' / 'app.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'symbol_daily'))
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)

    class ReadOnlyTestDB:

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_daily_prices_df(self, symbol):
            return frame.copy()

        def get_client_types_df(self, symbol):
            return ct.copy()
    namespace = {'pd': pd, 'market_rules': market_rules, 'to_jalali': to_jalali, 'ro_db': ReadOnlyTestDB}
    exec(compile(module, str(settings.BASE_DIR / 'dashboard' / 'app.py'), 'exec'), namespace)
    return namespace['symbol_daily']('آزمون')

def test_dashboard_and_feature_table_use_consistent_buyer_power_units():
    frame = prices()
    ct = client_types(frame)
    ct.loc[:, 'buy_i_value'] = 66000.0
    ct.loc[:, 'buy_i_volume'] = 660.0
    ct.loc[:, 'buy_n_volume'] = 340.0
    feature_value = compute_daily_features(frame, ct)['buyer_power'].iloc[-1]
    dashboard_value = dashboard_symbol_daily(frame, ct)['buyer_power'].iloc[-1]
    assert dashboard_value == pytest.approx(feature_value)

def test_zero_sell_count_does_not_always_produce_infinity():
    frame = prices()
    ct = client_types(frame)
    ct.loc[:, 'sell_i_count'] = 0
    result = dashboard_symbol_daily(frame, ct)['buyer_power'].iloc[-1]
    assert math.isnan(result)

def test_dashboard_never_displays_infinite_buyer_power():
    frame = prices()
    ct = client_types(frame)
    ct.loc[:, 'sell_i_value'] = 0
    result = dashboard_symbol_daily(frame, ct)['buyer_power'].iloc[-1]
    assert math.isnan(result)

def test_unknown_live_buy_volume_does_not_become_zero_buyer_power():
    result = buyer_power({'real_buy_volume': None, 'real_buy_count': 10, 'real_sell_volume': 400, 'real_sell_count': 10})
    assert result is None

@pytest.fixture
def dip_runner(db, tmp_path, monkeypatch):
    from research import labels
    frame = prices()
    frame.loc[:, 'low_raw'] = 97.5
    for i, symbol in enumerate(['آزمونالف', 'آزمونب']):
        sid = db.upsert_symbol({'symbol_fa': symbol, 'ins_code': str(i + 1), 'instrument_type_code': 300})
        db.upsert_daily_prices_df(sid, frame)
    monkeypatch.setattr(store, 'FEATURES_ROOT', tmp_path / 'features')
    store.build_feature_store(db)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'output')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'dev_start', str(CAL[0]))
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'min_daily_value_rial', 0)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'top_k', 1)
    monkeypatch.setattr(labels, 'CALENDAR_MIN_SYMBOLS', 2)
    monkeypatch.setattr(dip_run, 'RANDOM_DRAWS', 1)
    monkeypatch.setattr(dip_run, 'STRATEGIES', [Strategy('audit', 'آزمون', lambda f: pd.Series(1.0, index=f.index))])
    return settings.DATA_DIR / 'research'

def test_failed_experiment_registration_does_not_publish_a_success_pointer(db, dip_runner, monkeypatch):
    dip_runner.mkdir(parents=True)
    pointer = dip_runner / 'latest-dip.json'
    pointer.write_text(json.dumps({'experiment_id': 'previous-success'}), encoding='utf-8')
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    with pytest.raises(RuntimeError, match='ثبت آزمایش'):
        dip_run.run_dip_research(db, rules=[DipRule()])
    latest = json.loads(pointer.read_text(encoding='utf-8'))['experiment_id']
    assert latest == 'previous-success'

def test_dip_experiment_preserves_inputs_and_execution_provenance(db, dip_runner):
    payload = dip_run.run_dip_research(db, rules=[DipRule()])
    params = payload['params']
    assert params['inputs'] and params['code_sha256'] and params['slippage_bps_time_exit'] >= 0
    inputs = list(dip_runner.rglob('*.parquet'))
    assert inputs, 'ورودی قیمت/فیچر همین آزمایش حفظ نشده است'

def test_dip_report_json_has_no_nonstandard_numeric_constants(db, dip_runner, monkeypatch):
    original = dip_run.evaluate

    def evaluate_with_infinite_stat(*args, **kwargs):
        result = original(*args, **kwargs)
        result['mean_trade'] = float('inf')
        return result
    monkeypatch.setattr(dip_run, 'evaluate', evaluate_with_infinite_stat)
    payload = dip_run.run_dip_research(db, log_experiment=False, rules=[DipRule()])
    text = (dip_runner / f"{payload['experiment_id']}.json").read_text(encoding='utf-8')

    def reject_constant(token):
        raise ValueError(f'Non-standard JSON numeric constant: {token}')
    json.loads(text, parse_constant=reject_constant)

def test_current_holdout_command_does_not_accept_the_dip_rule(db):
    from research.run import run_research
    with pytest.raises(ValueError, match='روش نهایی'):
        run_research(db, unseal_holdout=True, holdout_strategy=DipRule().name, holdout_horizon=1)

def test_stop_job_handles_a_child_that_ignores_termination(monkeypatch):
    from ops import jobs

    class ResistantChild:
        running = True

        def terminate(self):
            pass

        def kill(self):
            self.running = False
    child = ResistantChild()
    parent = SimpleNamespace(children=lambda recursive: [child], terminate=lambda: None, wait=lambda timeout: 0, kill=lambda: None)
    owned = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(jobs, '_OWNED_PROCESSES', {'audit': owned})
    monkeypatch.setattr(jobs, '_read_state', lambda name: {'pid': 1, 'created_at': 1})
    monkeypatch.setattr(jobs, '_is_alive', lambda *args: True)
    monkeypatch.setattr(jobs.psutil, 'Process', lambda pid: parent)
    monkeypatch.setattr(jobs.psutil, 'wait_procs', lambda procs, timeout: ([], [p for p in procs if getattr(p, 'running', False)]))
    monkeypatch.setattr(owned, 'wait', lambda timeout: 0, raising=False)
    assert jobs.stop_job('audit')
    assert not child.running

def test_windows_job_stop_terminates_a_real_owned_process_tree(tmp_path, monkeypatch):
    from ops import jobs
    script = tmp_path / 'audit_process_tree.py'
    ready = tmp_path / 'child_pid.txt'
    script.write_text("import subprocess, sys, time\nfrom pathlib import Path\nflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'], creationflags=flags)\nPath(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\ntime.sleep(90)\n", encoding='utf-8')
    monkeypatch.setattr(jobs, 'BRS_SCRIPT', script)
    monkeypatch.setattr(jobs, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(jobs.settings, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(jobs, '_OWNED_PROCESSES', {})
    child = None
    try:
        assert jobs.start_job('audit-tree', [str(ready)])
        owned = jobs._OWNED_PROCESSES['audit-tree']
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        child = psutil.Process(int(ready.read_text(encoding='utf-8')))
        assert jobs.stop_job('audit-tree')
        owned.wait(timeout=5)
        deadline = time.monotonic() + 3
        while child.is_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not child.is_running()
    finally:
        owned = jobs._OWNED_PROCESSES.get('audit-tree')
        if owned is not None and owned.poll() is None:
            owned.kill()
            owned.wait(timeout=5)
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=5)
