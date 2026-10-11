# -*- coding: utf-8 -*-
"""
آزمون‌های موتور فیچر — مهم‌ترین تضمین پروژه: هیچ فیچری از آینده استفاده نمی‌کند.

داده این آزمون‌ها یک سری قیمت مصنوعیِ تکرارپذیر (seed ثابت) است که فقط در حافظه آزمون ساخته می‌شود
و هرگز وارد دیتابیس اصلی نمی‌شود.
"""

import numpy as np
import pandas as pd
import pytest

from features.daily import DAILY_FEATURES, PRICE_FEATURES, compute_daily_features
from features.market import add_relative_features, compute_market_features
from features.store import ALL_FEATURES, build_feature_store, load_feature_store, load_manifest

N_DAYS = 160
EX_DATE = 90           # روز افزایش سرمایه در سری آزمونی
CA_FACTOR = 0.72       # ضریب تعدیل قیمت در آن روز


def synthetic_history(seed: int = 7, corporate_action: bool = True):
    """سری قیمت و حقیقی/حقوقی آزمونی با صف قفل، روز اقدام شرکتی و فیلد خالی."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=N_DAYS).strftime("%Y-%m-%d")
    rets = rng.normal(0.001, 0.02, N_DAYS).clip(-0.05, 0.05)
    rets[40:43] = 0.05                       # سه روز صف خرید قفل
    rows, prev_close = [], 1000.0
    for i in range(N_DAYS):
        ref = prev_close * (CA_FACTOR if corporate_action and i == EX_DATE else 1.0)
        close = ref * (1 + rets[i])
        if 40 <= i < 43:
            low = high = last = close
        else:
            low, high = min(ref, close) * 0.985, max(ref, close) * 1.015
            last = close * (1 + rng.normal(0, 0.003))
            last = min(max(last, low), high)
        volume = float(rng.integers(1_000_000, 5_000_000))
        rows.append({
            "date": dates[i], "open_raw": ref, "high_raw": high, "low_raw": low, "close_raw": last,
            "close_final_raw": close, "ref_price_raw": ref, "volume": volume, "value": volume * close,
            "trade_count": 100.0,
        })
        prev_close = close
    prices = pd.DataFrame(rows)
    prices.loc[10, "close_raw"] = np.nan    # فیلد خالی‌شده توسط اعتبارسنج
    ct = pd.DataFrame({
        "date": prices["date"],
        "buy_i_volume": prices["volume"] * 0.6, "sell_i_volume": prices["volume"] * 0.55,
        "buy_n_volume": prices["volume"] * 0.4, "sell_n_volume": prices["volume"] * 0.45,
        "buy_i_value": prices["value"] * rng.uniform(0.4, 0.8, N_DAYS),
        "sell_i_value": prices["value"] * rng.uniform(0.4, 0.8, N_DAYS),
        "buy_n_value": prices["value"] * 0.4, "sell_n_value": prices["value"] * 0.45,
        "buy_i_count": rng.integers(50, 500, N_DAYS).astype(float),
        "sell_i_count": rng.integers(50, 500, N_DAYS).astype(float),
        "buy_n_count": 3.0, "sell_n_count": 3.0,
    }).drop(index=[120])  # روزی که حقیقی/حقوقی منتشر نشده
    return prices, ct


def test_no_lookahead_truncation():
    """فیچر روز t با داده کامل = فیچر روز t با داده بریده‌شده در t (برای ۲۵ نقطه برش)."""
    prices, ct = synthetic_history()
    full = compute_daily_features(prices, ct)
    for cut in list(range(5, N_DAYS, 7)) + [N_DAYS - 1]:
        partial = compute_daily_features(prices.iloc[:cut + 1], ct[ct["date"] <= prices["date"].iloc[cut]])
        expected = full.iloc[cut][DAILY_FEATURES].astype(float)
        actual = partial.iloc[cut][DAILY_FEATURES].astype(float)
        mismatched = [f for f in DAILY_FEATURES if not np.isclose(expected[f], actual[f], equal_nan=True, rtol=1e-9)]
        assert not mismatched, f"نشت داده آینده در روز {cut}: {mismatched}"


def test_corporate_action_does_not_distort_price_features():
    """فیچرهای قیمتی سری دارای افزایش سرمایه با سری بدون افزایش سرمایه (همان بازده‌ها) برابرند."""
    with_ca, ct = synthetic_history(corporate_action=True)
    without_ca, _ = synthetic_history(corporate_action=False)
    a = compute_daily_features(with_ca, ct)
    b = compute_daily_features(without_ca, ct)
    scale_free = [f for f in PRICE_FEATURES if f != "value_ratio_20d"]
    for f in scale_free:
        assert np.allclose(a[f].astype(float), b[f].astype(float), equal_nan=True, rtol=1e-6, atol=1e-9), f
    # بازده روز اقدام شرکتی کوچک است، نه افت کاذب ۲۸٪
    assert abs(a.loc[EX_DATE, "ret_1d"]) <= 0.05
    assert with_ca.loc[EX_DATE, "close_final_raw"] / with_ca.loc[EX_DATE - 1, "close_final_raw"] - 1 < -0.2


def test_feature_values_are_sane():
    prices, ct = synthetic_history()
    f = compute_daily_features(prices, ct)
    assert list(f.columns) == ["date"] + DAILY_FEATURES
    assert f["rsi_14d"].dropna().between(0, 100).all()
    assert f["close_position"].dropna().between(0, 1).all()
    assert f.loc[40:42, "locked_up"].tolist() == [1, 1, 1]
    assert f.loc[40:42, "lock_up_streak"].tolist() == [1, 2, 3] and f.loc[43, "lock_up_streak"] == 0
    assert pd.isna(f.loc[40, "close_position"])           # روز تک‌قیمتی: بازه صفر → بدون مقدار، نه عدد ساختگی
    assert pd.isna(f.loc[120, "buyer_power"])               # روز بدون حقیقی/حقوقی
    assert pd.isna(f.loc[10, "final_vs_last_pct"])          # فیلد خالی ورودی
    assert not np.isinf(f[DAILY_FEATURES].astype(float).to_numpy()).any()
    # پنجره‌های غلتان قبل از داشتن داده کافی مقدار ندارند
    assert f.loc[:13, "vol_20d"].isna().all() and f.loc[:38, "dist_sma_50d"].isna().all()


def test_features_without_client_types():
    prices, _ = synthetic_history()
    f = compute_daily_features(prices, None)
    assert f["buyer_power"].isna().all() and f["ret_5d"].notna().sum() > 100


def test_market_and_relative_features():
    panel = pd.DataFrame({
        "date": ["d1", "d1", "d1", "d2", "d2", "d2"],
        "symbol": ["a", "b", "c", "a", "b", "c"],
        "ret_1d": [0.02, -0.01, 0.03, -0.02, -0.01, 0.0],
        "ret_5d": [0.10, 0.00, 0.05, 0.02, 0.04, 0.06],
        "locked_up": [1, 0, 0, 0, 0, 0], "locked_down": [0, 0, 0, 1, 0, 0],
        "real_net_value": [10.0, -5.0, np.nan, 1.0, 1.0, 1.0], "value": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
    })
    market = compute_market_features(panel).set_index("date")
    assert market.loc["d1", "mkt_breadth"] == pytest.approx(2 / 3)
    assert market.loc["d2", "mkt_breadth"] == 0.0
    assert market.loc["d1", "mkt_locked_up_ratio"] == pytest.approx(1 / 3)
    assert market.loc["d1", "mkt_real_flow_ratio"] == pytest.approx(5 / 200)
    rel = add_relative_features(panel).set_index(["date", "symbol"])
    assert rel.loc[("d1", "a"), "rel_ret_5d"] == pytest.approx(0.05)


def test_feature_store_build_and_load(db, tmp_path):
    for i, symbol in enumerate(["فولاد", "خودرو"]):
        prices, ct = synthetic_history(seed=i)
        sym_id = db.upsert_symbol({"symbol_fa": symbol, "ins_code": str(i), "instrument_type_code": 300})
        db.upsert_daily_prices_df(sym_id, prices, raw_provenance="tsetmc:daily")
        db.upsert_client_types_df(sym_id, ct)
    manifest = build_feature_store(db, root=tmp_path)
    assert manifest["rows"] == 2 * N_DAYS and manifest["symbols"] == 2
    assert manifest["features"] == ALL_FEATURES and manifest["coverage"]["ret_5d"] > 0.9
    panel = load_feature_store(root=tmp_path)
    assert list(panel.columns) == ["date", "symbol"] + ALL_FEATURES
    assert panel.groupby("date")["mkt_symbols"].first().max() == 2
    assert load_manifest(root=tmp_path)["version"] == manifest["version"]


def test_ipo_day_return_is_not_computed():
    """روز عرضه اولیه: قیمت مرجع = ارزش اسمی ۱۰۰۰ ریال؛ «بازده» ۱۸۳۳۸٪ نباید وارد فیچرها شود."""
    prices, ct = synthetic_history()
    prices.loc[0, ["ref_price_raw", "open_raw"]] = 1000.0
    f = compute_daily_features(prices, ct)
    assert pd.isna(f.loc[0, "ret_1d"]) and f.loc[1:, "ret_1d"].notna().all()
    assert f["days_since_listing"].tolist() == list(range(N_DAYS))


def test_persian_labels_cover_every_feature():
    from features.labels_fa import FEATURE_LABELS_FA, format_feature

    assert set(FEATURE_LABELS_FA) == set(ALL_FEATURES)
    assert format_feature("ret_1d", np.float32(0.0275)) == "+2.75%"
    assert format_feature("ret_1d", np.float32("nan")) == "—"
    assert format_feature("locked_up", 1.0) == "بله"
    assert format_feature("lock_up_streak", np.float32(3)) == "3"
