# -*- coding: utf-8 -*-
"""
آزمون‌های پژوهش فاز ۱-ج: برچسب خالص قابل اجرا و ارزیابی استراتژی‌های پایه (بدون اینترنت)
"""

import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from research.baselines import Strategy, evaluate_picks, newey_west_t, pick_top, random_benchmark, run_baselines
from research.labels import compute_labels

BUY, SELL = settings.FEES["buy"], settings.FEES["sell"]
SLIP = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10_000
CAL = [f"2026-06-{d:02d}" for d in range(1, 11)]


def _rows(symbol, spec):
    """spec: لیست (تاریخ، کمترین، بیشترین، میانگین موزون، پایانی، مرجع)؛ حجم ثابت ۱۰۰۰."""
    return [{"symbol": symbol, "date": d, "low_raw": lo, "high_raw": hi, "close_raw": c, "close_final_raw": c,
             "ref_price_raw": ref, "volume": 1000, "value": vw * 1000} for d, lo, hi, vw, c, ref in spec]


def _net(entry_vwap, exit_vwap, mult=1.0):
    return exit_vwap * (1 - SLIP) * (1 - SELL) * mult / (entry_vwap * (1 + SLIP) * (1 + BUY)) - 1


def test_label_buys_next_session_vwap_and_sells_h_sessions_later():
    # قیمت مرجع هر روز = پایانی دیروز (روز عادی، بدون اقدام شرکتی)
    spec = [(d, 99 + i, 101 + i, 100 + i, 100 + i, 100 + max(i - 1, 0)) for i, d in enumerate(CAL)]
    lab = compute_labels(pd.DataFrame(_rows("x", spec)), horizons=[1, 3], calendar=CAL).set_index("date")
    assert lab.loc[CAL[0], "net_ret_1d"] == pytest.approx(_net(101, 102))     # ورود ۰۲ (۱۰۱)، خروج ۰۳ (۱۰۲)
    assert lab.loc[CAL[0], "net_ret_3d"] == pytest.approx(_net(101, 104))
    # بدترین اجرا: خرید در بیشترین قیمت روز ورود (۱۰۱+۱) و فروش در کمترین قیمت روز خروج (۹۹+۲)
    assert lab.loc[CAL[0], "net_ret_1d_worst"] == pytest.approx(101 * (1 - SELL) / (102 * (1 + BUY)) - 1)
    assert lab.loc[CAL[0], "mfe_1d"] == pytest.approx(103 / (101 * (1 + SLIP)) - 1)
    assert pd.isna(lab.loc[CAL[-1], "net_ret_1d"]) and not lab.loc[CAL[-1], "entry_executable"]   # فردایی نیست


def test_no_entry_into_buy_queue_or_halted_symbol():
    spec = [(d, 99, 101, 100, 100, 100) for d in CAL]
    spec[1] = (CAL[1], 103, 103, 103, 103, 100)          # روز ورود: صف خرید قفل (تک‌قیمت +۳٪)
    prices = pd.DataFrame(_rows("x", spec[:4] + spec[5:]))  # ۰۵: نماد متوقف (بدون ردیف)
    lab = compute_labels(prices, horizons=[1], calendar=CAL).set_index("date")
    assert not lab.loc[CAL[0], "entry_executable"] and pd.isna(lab.loc[CAL[0], "net_ret_1d"])
    assert not lab.loc[CAL[3], "entry_executable"]       # فردای ۰۴، نماد معامله نداشت
    assert lab.loc[CAL[1], "entry_executable"]


def test_exit_rolls_past_locked_sell_queue_and_reports_delay():
    spec = [(d, 99, 101, 100, 100, 100) for d in CAL]
    spec[2] = (CAL[2], 97, 97, 97, 97, 100)               # روز خروج: صف فروش قفل
    spec[3] = (CAL[3], 96, 101, 100, 100, 97)             # مرجع فردا = پایانی روز قفل
    prices = pd.DataFrame(_rows("x", spec))
    lab = compute_labels(prices, horizons=[1], calendar=CAL).set_index("date")
    assert lab.loc[CAL[0], "exit_delay_1d"] == 1          # یک جلسه گیر افتاد
    assert lab.loc[CAL[0], "net_ret_1d"] == pytest.approx(_net(100, 100))   # خروج ۰۴ به ۱۰۰


def test_corporate_action_inside_holding_is_value_preserving():
    spec = [(d, 99, 101, 100, 100, 100) for d in CAL[:3]] + [(d, 49, 51, 50, 50, 50) for d in CAL[3:]]
    prices = pd.DataFrame(_rows("x", spec))               # روز ۰۴: افزایش سرمایه ۱۰۰٪ (مرجع ۱۰۰ → ۵۰)
    lab = compute_labels(prices, horizons=[3], calendar=CAL).set_index("date")
    assert lab.loc[CAL[0], "net_ret_3d"] == pytest.approx(_net(100, 50, mult=2.0))   # فقط هزینه‌ها، نه −۵۰٪
    assert lab.loc[CAL[0], "mae_3d"] == pytest.approx(49 * 2 / (100 * (1 + SLIP)) - 1)


def test_newey_west_matches_simple_t_without_lag():
    x = pd.Series(np.random.default_rng(1).normal(0.01, 0.02, 400))
    simple = x.mean() / (x.std(ddof=0) / np.sqrt(len(x)))
    assert newey_west_t(x, lag=0) == pytest.approx(simple)


def _panel():
    rng = np.random.default_rng(3)
    rows = []
    for d in CAL:
        for i in range(8):
            rows.append({"date": d, "symbol": f"s{i}", "value": 1e10 + i, "score": float(i),
                         "net_ret_1d": rng.normal(0, 0.01), "net_ret_1d_worst": -0.03})
    panel = pd.DataFrame(rows)
    panel.loc[(panel["symbol"] == "s7") & (panel["date"] == CAL[0]), "net_ret_1d"] = np.nan   # غیرقابل خرید
    return panel


def test_pick_top_and_unfilled_picks_cost_capital():
    panel = _panel()
    picks = pick_top(panel, panel["score"], k=2)
    assert picks.groupby("date").size().eq(2).all() and set(picks["symbol"]) == {"s7", "s6"}
    market = panel.groupby("date")["net_ret_1d"].mean()
    stats = evaluate_picks(picks, market, horizon=1, k=2)
    assert stats["fill_rate"] == pytest.approx(19 / 20)
    day0 = picks[picks["date"] == CAL[0]]["net_ret_1d"].fillna(0).sum() / 2
    assert stats["picks"] == 20 and stats["trades"] == 19 and day0 is not None
    assert stats["mean_trade_worst"] == pytest.approx(-0.03)


def test_random_benchmark_is_reproducible_and_run_baselines_shape():
    panel = _panel()
    assert random_benchmark(panel, 1, 2, trials=5) == random_benchmark(panel, 1, 2, trials=5)
    out = run_baselines(panel, [1], k=2, random_trials=3,
                        strategies=[Strategy("score", "امتیاز", lambda f: f["score"])])
    assert set(out["1"]) == {"market", "random", "strategies"} and "score" in out["1"]["strategies"]


def test_research_end_to_end_with_sealed_holdout(db, tmp_path, monkeypatch):
    """زنجیره کامل: دیتابیس ← فیچر ← برچسب ← ارزیابی ← ثبت آزمایش. دوره بیرون از نمونه گزارش نمی‌شود."""
    from features import store
    from research import labels as labels_mod
    from research import run as run_mod
    from tests.test_features import synthetic_history

    for i, symbol in enumerate(["فولاد", "خودرو", "شپنا"]):
        prices, ct = synthetic_history(seed=i)
        sym_id = db.upsert_symbol({"symbol_fa": symbol, "ins_code": str(i), "instrument_type_code": 300})
        db.upsert_daily_prices_df(sym_id, prices)
        db.upsert_client_types_df(sym_id, ct)
    store.build_feature_store(db, root=tmp_path / "features")
    monkeypatch.setattr(run_mod, "load_feature_snapshot", lambda **kwargs: store.load_feature_snapshot(root=tmp_path / "features", **kwargs))
    monkeypatch.setattr(run_mod, "LABELS_ROOT", tmp_path / "labels")
    monkeypatch.setattr(run_mod, "RESEARCH_ROOT", tmp_path / "research")
    monkeypatch.setattr(labels_mod, "CALENDAR_MIN_SYMBOLS", 2)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "min_daily_value_rial", 0)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "dev_start", "2025-01-01")
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "holdout_start", "2025-06-01")
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "horizons", [1, 3])
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "top_k", 2)

    out = run_mod.run_research(db, random_trials=3)
    assert set(out["results"]) == {"development"}                        # مهر و موم: holdout گزارش نمی‌شود
    assert out["results"]["development"]["end"] == "2025-06-01"
    exp = db.get_experiments()[0]
    assert exp["experiment_id"] == out["experiment_id"] and exp["dataset_version"].startswith(f"{labels_mod.LABEL_SET_VERSION}:")
    assert json.loads(exp["parameters_json"])["features"]["sha256"]       # بازتولیدپذیری: اثر انگشت داده‌ها
    report = (tmp_path / "research" / f"{out['experiment_id']}.md").read_text(encoding="utf-8")
    assert "نگهداری 3 جلسه" in report and "بیرون از نمونه" not in report.split("## محدودیت‌ها")[0]
    assert run_mod.run_research(db, random_trials=3, unseal_holdout=True, holdout_strategy="momentum_5d", holdout_horizon=1)["results"].keys() == {"development", "holdout"}


def test_t_statistic_is_unknown_if_calendar_gaps_cannot_be_modelled():
    """Unknown sessions cannot be deleted to make the next observed session adjacent."""
    from research.baselines import tolerant_t

    x = pd.Series(np.random.default_rng(5).normal(0.002, 0.01, 1000))
    assert np.isfinite(tolerant_t(x, lag=3))
    with_gaps = x.copy()
    with_gaps.iloc[[10, 500, 900]] = np.nan
    assert np.isnan(tolerant_t(with_gaps, lag=3))
    mostly_unknown = x.copy()
    mostly_unknown.iloc[:100] = np.nan                     # ۱۰٪ نامعلوم ← آماره اعلام نمی‌شود
    assert np.isnan(tolerant_t(mostly_unknown, lag=3))


def test_research_universe_keeps_halted_symbols(db):
    """سهم متوقف امروز (غیرفعال موقت) از تاریخچه پژوهش حذف نمی‌شود (سوگیری بقا)."""
    active = db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "1", "instrument_type_code": 300})
    halted = db.upsert_symbol({"symbol_fa": "شپنا", "ins_code": "2", "instrument_type_code": 300})
    db.set_symbols_active([halted], False)
    assert [s["id"] for s in db.get_universe_symbols()] == [active]
    assert {s["id"] for s in db.get_universe_symbols(include_inactive=True)} == {active, halted}
