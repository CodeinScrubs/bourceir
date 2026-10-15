"""بازتولید مستقل ادعاهای بازبین؛ فقط آزمون و داده موقت، بدون اصلاح کد عملیاتی.

این فایل عمداً test_ نام ندارد و با انتخاب صریح اجرا می‌شود. آزمون‌های ناموفق،
قراردادهای نقض‌شده نسخه 0.5.2 را نشان می‌دهند؛ سبز شدن آزمون‌های قبلی رفع آن‌ها نیست.
این نسخه تاریخی روی commit 230ff8189b5bce991d108a4d3cbf991d51918ae3 معنا دارد؛
قراردادهای اصلاح‌شده فعلی در test_research_integrity.py جمع‌آوری و در CI اجرا می‌شوند.
"""

import ast
import json
import math
import os
import time
from pathlib import Path
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

CAL = np.array([f"2024-01-{i:02d}" for i in range(1, 9)])
OBSERVATIONS = {}


def observe(name, **values):
    OBSERVATIONS[name] = values


def safe_json(value):
    if isinstance(value, dict):
        return {k: safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


@pytest.fixture(scope="session", autouse=True)
def record_observations():
    yield
    output = os.environ.get("BRS_AUDIT_OUTPUT")
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(safe_json(OBSERVATIONS), ensure_ascii=False, indent=2, allow_nan=False),
                          encoding="utf-8")


def prices(n=8):
    return pd.DataFrame({
        "date": CAL[:n], "open_raw": 100.0, "high_raw": 101.0, "low_raw": 99.0,
        "close_raw": 100.0, "close_final_raw": 100.0, "ref_price_raw": 100.0,
        "volume": 1000, "value": 100000.0, "trade_count": 10,
    })


def set_day(frame, i, op, high, low, close, ref, vwap=None):
    frame.loc[i, ["open_raw", "high_raw", "low_raw", "close_raw", "close_final_raw", "ref_price_raw"]] = [
        op, high, low, close, close, ref,
    ]
    frame.loc[i, "value"] = (vwap if vwap is not None else close) * 1000


def client_types(frame):
    return pd.DataFrame({
        "date": frame["date"], "buy_i_value": 60000.0, "sell_i_value": 40000.0,
        "buy_i_volume": 600.0, "sell_i_volume": 400.0, "buy_i_count": 10, "sell_i_count": 10,
        "buy_n_volume": 400.0, "sell_n_volume": 600.0,
    })


def dip_stats(statuses):
    picks = pd.DataFrame({"date": CAL[:len(statuses)], "symbol": "x"})
    trades = picks.assign(dip_status=statuses, entry_kind="limit", exit_kind="target", days_held=1,
                          net_ret=[0.1 if status == "complete" else np.nan for status in statuses])
    result = dip_run.evaluate(trades, picks, pd.Index(picks["date"]), 1, 1,
                              pd.Series(0.6, index=picks["date"]))
    return result


def test_flow_ratio_uses_the_same_observed_days_in_both_sums():
    frame = prices(6)
    ct = client_types(frame).drop(index=[2, 4])
    result = compute_daily_features(frame, ct)["real_flow_ratio_5d"].iloc[-1]
    expected = (3 * 20000) / (3 * 100000)
    observe("flow_missing_days", actual=result, expected=expected)
    assert result == pytest.approx(expected)


def test_flow_ratio_with_complete_observations_is_unchanged():
    frame = prices(6)
    assert compute_daily_features(frame, client_types(frame))["real_flow_ratio_5d"].iloc[-1] == pytest.approx(0.2)


def test_invalid_dip_outcome_is_unknown_instead_of_idle_cash():
    result = dip_stats(["invalid"])
    observe("dip_invalid_cohort", actual=result["mean_daily_cohort"], expected="unknown")
    assert math.isnan(result["mean_daily_cohort"])


def test_missing_dip_result_is_unknown_instead_of_idle_cash():
    picks = pd.DataFrame({"date": [CAL[0]], "symbol": ["x"]})
    trades = pd.DataFrame(columns=["date", "symbol", "dip_status", "net_ret", "exit_kind", "days_held"])
    result = dip_run.evaluate(trades, picks, pd.Index([CAL[0]]), 1, 1, pd.Series(0.6, index=[CAL[0]]))
    observe("dip_missing_result", actual=result["mean_daily_cohort"])
    assert math.isnan(result["mean_daily_cohort"])


def test_censored_exit_can_still_have_a_known_filled_entry():
    result = dip_stats(["complete", "censored", "not_filled"])
    observe("censored_fill_rate", actual=result["fill_rate"], expected=2 / 3,
            completed_trades=result["trades"], censored=result["censored"])
    assert result["fill_rate"] == pytest.approx(2 / 3)
    assert result["trades"] == 1 and result["censored"] == 1


def test_missing_open_does_not_invent_a_gap_fill_or_same_day_sequence():
    frame = prices()
    set_day(frame, 1, np.nan, 103, 97.5, 100, 100)
    row = simulate_symbol(frame, CAL, DipRule()).iloc[0]
    observe("missing_open", status=row["dip_status"], return_value=row["net_ret"])
    assert row["dip_status"] == "invalid" and math.isnan(row["net_ret"])


def test_mae_includes_the_days_spent_waiting_for_an_actual_exit():
    frame = prices()
    set_day(frame, 2, 97, 97, 97, 97, 100)
    set_day(frame, 3, 94.09, 94.09, 94.09, 94.09, 97)
    set_day(frame, 4, 91.2673, 91.2673, 91.2673, 91.2673, 94.09)
    set_day(frame, 5, 90, 92, 88, 90, 91.2673)
    row = compute_labels(frame.assign(symbol="x"), [1], calendar=CAL).iloc[0]
    slip = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10000
    expected = 88 / (100 * (1 + slip)) - 1
    observe("delayed_exit_mae", actual=row["mae_1d"], expected=expected, net_return=row["net_ret_1d"],
            delay=row["exit_delay_1d"], actual_exit=row["exit_date_1d"])
    assert row["exit_delay_1d"] == 3
    assert row["mae_1d"] == pytest.approx(expected)


def test_missing_official_close_does_not_automatically_make_factors_nan():
    frame = prices(3)
    frame.loc[1, "close_final_raw"] = np.nan
    frame.loc[2, "ref_price_raw"] = 50.0
    factor = market_rules.corporate_action_factors(frame).iloc[2]
    observe("official_close_fallback", factor=factor, last_trade=frame.loc[1, "close_raw"])
    assert factor == pytest.approx(0.5)


def test_dip_does_not_complete_a_return_through_an_unknown_reference():
    frame = prices()
    set_day(frame, 1, 100, 100.5, 97.5, 98, 100)
    set_day(frame, 2, 90, 91, 89, 90, np.nan)
    set_day(frame, 3, 90, 91, 89, 90, 90)
    row = simulate_symbol(frame, CAL, DipRule(hold=2)).iloc[0]
    label = compute_labels(frame.assign(symbol="x"), [2], calendar=CAL).iloc[0]
    observe("unknown_reference_in_hold", dip_status=row["dip_status"], dip_return=row["net_ret"],
            labels_status=label["label_status_2d"], factor=market_rules.corporate_action_factors(frame).iloc[2])
    assert row["dip_status"] == "invalid" and math.isnan(row["net_ret"])


def test_labels_do_not_assume_a_known_action_factor_when_both_previous_closes_are_missing():
    frame = prices()
    set_day(frame, 2, 100, 101, 99, np.nan, 100, vwap=100)
    set_day(frame, 3, 50, 51, 49, 50, 50)
    row = compute_labels(frame.assign(symbol="x"), [2], calendar=CAL).iloc[0]
    observe("missing_previous_closes", status=row["label_status_2d"], net_return=row["net_ret_2d"],
            factor=market_rules.corporate_action_factors(frame).iloc[3])
    assert row["label_status_2d"] == "invalid_data"


def test_full_market_benchmark_does_not_silently_discard_an_unknown_holding():
    panel = pd.DataFrame({"date": [CAL[0]] * 2, "symbol": ["x", "y"], "net_ret_1d": [0.1, np.nan],
                          "label_status_1d": ["complete", "censored"]})
    value = market_benchmark(panel, 1).iloc[0]
    observe("market_incomplete_basket", full_basket=value, surviving_subset=0.1)
    assert math.isnan(value)


def test_full_market_benchmark_treats_a_known_blocked_entry_as_cash():
    panel = pd.DataFrame({"date": [CAL[0]] * 2, "symbol": ["x", "y"], "net_ret_1d": [0.1, np.nan],
                          "label_status_1d": ["complete", "entry_blocked"]})
    assert market_benchmark(panel, 1).iloc[0] == pytest.approx(0.05)


@pytest.fixture
def feature_root(db, tmp_path):
    frame = prices()
    sid = db.upsert_symbol({"symbol_fa": "آزمون", "ins_code": "1", "instrument_type_code": 300})
    db.upsert_daily_prices_df(sid, frame)
    root = tmp_path / "features"
    store.build_feature_store(db, root=root)
    return root


def tamper_feature_file(root):
    target = store.store_dir(root=root) / "daily.parquet"
    data = pd.read_parquet(target)
    data.loc[:, "ret_1d"] = 0.9
    data.to_parquet(target, index=False)


def test_load_feature_store_rejects_a_valid_but_tampered_parquet(feature_root):
    tamper_feature_file(feature_root)
    with pytest.raises(ValueError, match="اثر انگشت"):
        store.load_feature_store(root=feature_root)
    observe("feature_store_integrity", rejected=True)


def test_symbol_feature_reader_also_rejects_tampered_data(feature_root):
    tamper_feature_file(feature_root)
    observed = store.load_symbol_features("آزمون", root=feature_root)
    observe("symbol_reader_integrity", displayed_ret=float(observed.iloc[-1]["ret_1d"]))
    with pytest.raises(ValueError, match="اثر انگشت"):
        store.load_symbol_features("آزمون", root=feature_root)


def test_target_sale_is_blocked_on_a_locked_sell_day():
    frame = prices()
    set_day(frame, 1, 100, 106, 97.5, 106, 100)
    set_day(frame, 2, 102.82, 102.82, 102.82, 102.82, 106)
    set_day(frame, 3, 101, 103, 100, 102, 102.82)
    assert market_rules.lock_flags(frame).loc[2, "locked_down"]
    row = simulate_symbol(frame, CAL, DipRule()).iloc[0]
    observe("target_in_sell_lock", exit_kind=row["exit_kind"], held=row["days_held"], return_value=row["net_ret"],
            factors=market_rules.corporate_action_factors(frame).tolist())
    assert row["days_held"] == 2 and row["exit_kind"] == "time_late"


def test_action_factors_do_not_cross_symbols():
    panel = pd.concat([prices(2).assign(symbol="a"), prices(2).assign(symbol="b")], ignore_index=True)
    panel.loc[panel["symbol"] == "b", ["close_raw", "close_final_raw", "ref_price_raw"]] = 1000.0
    factors = market_rules.corporate_action_factors(panel)
    observe("cross_symbol_factors", actual=factors.tolist(), expected=[1.0] * 4)
    assert factors.tolist() == pytest.approx([1.0] * 4)


def dashboard_symbol_daily(frame, ct):
    # فقط تابع واقعی استخراج می‌شود؛ import کل dashboard موجب باز کردن دیتابیس عملیاتی می‌شود.
    source = (settings.BASE_DIR / "dashboard" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "symbol_daily")
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

    namespace = {"pd": pd, "market_rules": market_rules, "to_jalali": to_jalali, "ro_db": ReadOnlyTestDB}
    exec(compile(module, str(settings.BASE_DIR / "dashboard" / "app.py"), "exec"), namespace)
    return namespace["symbol_daily"]("آزمون")


def test_dashboard_and_feature_table_use_consistent_buyer_power_units():
    frame = prices()
    ct = client_types(frame)
    ct.loc[:, "buy_i_value"] = 66000.0
    feature_value = compute_daily_features(frame, ct)["buyer_power"].iloc[-1]
    dashboard_value = dashboard_symbol_daily(frame, ct)["buyer_power"].iloc[-1]
    observe("buyer_power_units", dashboard=dashboard_value, feature=feature_value)
    assert dashboard_value == pytest.approx(feature_value)


def test_zero_sell_count_does_not_always_produce_infinity():
    frame = prices()
    ct = client_types(frame)
    ct.loc[:, "sell_i_count"] = 0
    result = dashboard_symbol_daily(frame, ct)["buyer_power"].iloc[-1]
    observe("zero_sell_count", actual=result)
    assert result == 0 and not math.isinf(result)


def test_dashboard_never_displays_infinite_buyer_power():
    frame = prices()
    ct = client_types(frame)
    ct.loc[:, "sell_i_volume"] = 0
    result = dashboard_symbol_daily(frame, ct)["buyer_power"].iloc[-1]
    observe("zero_sell_volume", is_infinite=bool(math.isinf(result)))
    assert math.isnan(result)


def test_unknown_live_buy_volume_does_not_become_zero_buyer_power():
    result = buyer_power({"real_buy_volume": None, "real_buy_count": 10,
                          "real_sell_volume": 400, "real_sell_count": 10})
    observe("additional_unknown_live_volume", actual=result, expected="unknown")
    assert result is None


@pytest.fixture
def dip_runner(db, tmp_path, monkeypatch):
    from research import labels

    frame = prices()
    frame.loc[:, "low_raw"] = 97.5
    for i, symbol in enumerate(["آزمونالف", "آزمونب"]):
        sid = db.upsert_symbol({"symbol_fa": symbol, "ins_code": str(i + 1), "instrument_type_code": 300})
        db.upsert_daily_prices_df(sid, frame)
    monkeypatch.setattr(store, "FEATURES_ROOT", tmp_path / "features")
    store.build_feature_store(db)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "output")
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "dev_start", str(CAL[0]))
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "min_daily_value_rial", 0)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "top_k", 1)
    monkeypatch.setattr(labels, "CALENDAR_MIN_SYMBOLS", 2)
    monkeypatch.setattr(dip_run, "RANDOM_DRAWS", 1)
    monkeypatch.setattr(dip_run, "STRATEGIES", [Strategy("audit", "آزمون", lambda f: pd.Series(1.0, index=f.index))])
    return settings.DATA_DIR / "research"


def test_failed_experiment_registration_does_not_publish_a_success_pointer(db, dip_runner, monkeypatch):
    dip_runner.mkdir(parents=True)
    pointer = dip_runner / "latest-dip.json"
    pointer.write_text(json.dumps({"experiment_id": "previous-success"}), encoding="utf-8")
    monkeypatch.setattr(db, "log_experiment", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="ثبت آزمایش"):
        dip_run.run_dip_research(db, rules=[DipRule()])
    latest = json.loads(pointer.read_text(encoding="utf-8"))["experiment_id"]
    observe("failed_dip_publication", latest=latest, expected="previous-success")
    assert latest == "previous-success"


def test_dip_experiment_preserves_inputs_and_execution_provenance(db, dip_runner):
    payload = dip_run.run_dip_research(db, rules=[DipRule()])
    params = payload["params"]
    inputs = list(dip_runner.rglob("*.parquet"))
    observe("dip_provenance", params=list(params), pinned_parquet_files=len(inputs),
            persisted_experiments=len(db.get_experiments()))
    assert inputs, "ورودی قیمت/فیچر همین آزمایش حفظ نشده است"


def test_dip_report_json_has_no_nonstandard_numeric_constants(db, dip_runner, monkeypatch):
    original = dip_run.evaluate

    def evaluate_with_infinite_stat(*args, **kwargs):
        result = original(*args, **kwargs)
        result["mean_trade"] = float("inf")
        return result

    monkeypatch.setattr(dip_run, "evaluate", evaluate_with_infinite_stat)
    payload = dip_run.run_dip_research(db, log_experiment=False, rules=[DipRule()])
    text = (dip_runner / f"{payload['experiment_id']}.json").read_text(encoding="utf-8")
    observe("dip_json_infinity", has_infinity="Infinity" in text)

    def reject_constant(token):
        raise ValueError(f"Non-standard JSON numeric constant: {token}")

    json.loads(text, parse_constant=reject_constant)


def test_current_holdout_command_does_not_accept_the_dip_rule(db):
    from research.run import run_research

    with pytest.raises(ValueError, match="روش نهایی"):
        run_research(db, unseal_holdout=True, holdout_strategy=DipRule().name, holdout_horizon=1)
    observe("dip_holdout_command", rejected=True)


def test_stop_job_handles_a_child_that_ignores_termination(monkeypatch):
    from ops import jobs

    class ResistantChild:
        running = True

        def terminate(self):
            pass

        def kill(self):
            self.running = False

    child = ResistantChild()
    parent = SimpleNamespace(children=lambda recursive: [child], terminate=lambda: None,
                             wait=lambda timeout: 0, kill=lambda: None)
    owned = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(jobs, "_OWNED_PROCESSES", {"audit": owned})
    monkeypatch.setattr(jobs, "_read_state", lambda name: {"pid": 1, "created_at": 1})
    monkeypatch.setattr(jobs, "_is_alive", lambda *args: True)
    monkeypatch.setattr(jobs.psutil, "Process", lambda pid: parent)
    assert jobs.stop_job("audit")
    observe("resistant_child_cleanup", child_still_running=child.running)
    assert not child.running


def test_windows_job_stop_terminates_a_real_owned_process_tree(tmp_path, monkeypatch):
    from ops import jobs

    script = tmp_path / "audit_process_tree.py"
    ready = tmp_path / "child_pid.txt"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'], creationflags=flags)\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(90)\n", encoding="utf-8",
    )
    monkeypatch.setattr(jobs, "BRS_SCRIPT", script)
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs.settings, "LOG_DIR", tmp_path)
    monkeypatch.setattr(jobs, "_OWNED_PROCESSES", {})
    child = None
    try:
        assert jobs.start_job("audit-tree", [str(ready)])
        owned = jobs._OWNED_PROCESSES["audit-tree"]
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        child = psutil.Process(int(ready.read_text(encoding="utf-8")))
        assert jobs.stop_job("audit-tree")
        owned.wait(timeout=5)
        deadline = time.monotonic() + 3
        while child.is_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        observe("real_process_cleanup", parent_returncode=owned.returncode, child_running=child.is_running())
        assert not child.is_running()
    finally:
        owned = jobs._OWNED_PROCESSES.get("audit-tree")
        if owned is not None and owned.poll() is None:
            owned.kill()
            owned.wait(timeout=5)
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=5)
