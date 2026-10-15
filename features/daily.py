# -*- coding: utf-8 -*-
"""
فیچرهای روزانه هر نماد (Point-in-Time)

ورودی: خروجی DatabaseManager.get_daily_prices_df (ستون‌های خام) و get_client_types_df.
خروجی: یک ردیف برای هر روز معاملاتی؛ فیچر ردیف t فقط از داده‌های تا پایان روز t ساخته می‌شود و
برای تصمیم معاملاتی روز t+1 قابل استفاده است.

نکته کلیدی تعدیل: به جای قیمت خام (که در روز افزایش سرمایه/سود نقدی جهش کاذب دارد) از «شاخص بازده»
استفاده می‌شود: I_t = Π (1 + بازده PIT). نسبت‌هایی مثل فاصله از میانگین یا سقف ۲۰ روزه روی این شاخص
دقیقاً همان مقدار قیمت تعدیل‌شده تا روز t را دارند، ولی هیچ اطلاعاتی از آینده (اقدام شرکتی بعدی) ندارند.
"""

from typing import List, Optional

import numpy as np
import pandas as pd

from core import market_rules

FEATURE_SET_VERSION = "daily-v4"  # paired observations; see docs/features.md

PRICE_FEATURES = [
    "ret_1d", "ret_3d", "ret_5d", "ret_20d",
    "vol_20d", "atr_14d_pct", "rsi_14d",
    "dist_sma_20d", "dist_sma_50d", "dist_high_20d", "dist_low_20d",
    "range_pct", "close_position", "final_vs_last_pct", "days_since_listing",
    "value_ratio_20d", "locked_up", "locked_down", "lock_up_streak", "lock_down_streak",
]
FLOW_FEATURES = [
    "real_flow_ratio", "real_flow_ratio_5d", "buyer_power", "buyer_power_5d",
    "per_capita_buy_ratio_20d", "legal_to_real_ratio",
]
DAILY_FEATURES: List[str] = PRICE_FEATURES + FLOW_FEATURES


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    """تقسیم بدون بی‌نهایت: تقسیم بر صفر یا مقدار گمشده → NaN (هرگز عدد ساختگی)."""
    out = a / b
    return out.replace([np.inf, -np.inf], np.nan)


def _streak(flag: pd.Series) -> pd.Series:
    """تعداد روزهای متوالی که flag تا امروز برقرار بوده است (۰ اگر امروز برقرار نیست)."""
    flag = flag.fillna(False).astype(bool)
    groups = (~flag).cumsum()
    return flag.astype(int).groupby(groups).cumsum()


def compute_daily_features(prices: pd.DataFrame, client_types: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    محاسبه فیچرهای روزانه یک نماد. همه عملیات علّی (causal) هستند: rolling و ewm روی گذشته، shift مثبت.
    """
    if prices["date"].duplicated().any():
        raise ValueError("برای هر نماد و تاریخ فقط یک ردیف قیمت مجاز است")
    df = prices.sort_values("date").reset_index(drop=True)
    out = pd.DataFrame({"date": df["date"]})
    if df.empty:
        return out.reindex(columns=["date"] + DAILY_FEATURES)

    # ─── بازده و شاخص بازده بدون جهش اقدام شرکتی ───
    ret = market_rules.pit_daily_returns(df)
    # روز اول معاملات (عرضه اولیه): قیمت مرجع همان ارزش اسمی ۱۰۰۰ ریالی است و «بازده» بی‌معناست
    ret.iloc[0] = np.nan
    index = (1.0 + ret.fillna(0.0)).cumprod()
    out["ret_1d"] = ret
    for n in (3, 5, 20):
        out[f"ret_{n}d"] = (index / index.shift(n) - 1.0).where(ret.rolling(n).count() == n)

    # ─── نوسان و محدوده ───
    out["vol_20d"] = ret.rolling(20, min_periods=15).std()
    ref = market_rules.reference_price(df)
    true_range = pd.concat([
        df["high_raw"] - df["low_raw"], (df["high_raw"] - ref).abs(), (df["low_raw"] - ref).abs(),
    ], axis=1).max(axis=1)
    out["atr_14d_pct"] = _safe_div(true_range, ref).rolling(14, min_periods=10).mean()
    gain, loss = ret.clip(lower=0), (-ret).clip(lower=0)
    segments = ret.isna().cumsum()
    avg_gain = gain.groupby(segments).transform(lambda x: x.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean())
    avg_loss = loss.groupby(segments).transform(lambda x: x.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean())
    out["rsi_14d"] = 100.0 - 100.0 / (1.0 + _safe_div(avg_gain, avg_loss))
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi_14d"] = 100.0
    out.loc[(avg_loss == 0) & (avg_gain == 0), "rsi_14d"] = 50.0   # قیمت ثابت: نه اشباع خرید، نه فروش

    # ─── موقعیت نسبت به میانگین و سقف/کف (روی شاخص بازده) ───
    out["dist_sma_20d"] = index / index.rolling(20, min_periods=15).mean() - 1.0
    out["dist_sma_50d"] = index / index.rolling(50, min_periods=40).mean() - 1.0
    out["dist_high_20d"] = index / index.rolling(20, min_periods=15).max() - 1.0
    out["dist_low_20d"] = index / index.rolling(20, min_periods=15).min() - 1.0
    valid_path = ret.notna().astype(int)
    valid_path.iloc[0] = int(pd.notna(market_rules.official_close(df).iloc[0]))
    for length, columns in ((20, ["dist_sma_20d", "dist_high_20d", "dist_low_20d"]), (50, ["dist_sma_50d"])):
        complete = valid_path.rolling(length, min_periods=1).min() == 1
        out[columns] = out[columns].where(complete, axis=0)

    # ─── رفتار روز ───
    official = market_rules.official_close(df)
    day_range = df["high_raw"] - df["low_raw"]
    out["range_pct"] = _safe_div(day_range, ref)
    # موقعیت آخرین معامله در بازه روز (۰ = کف، ۱ = سقف). از قیمت پایانی استفاده نمی‌شود چون وقتی حجم
    # کمتر از حجم مبنا باشد، قیمت پایانی فقط بخشی جابه‌جا می‌شود و می‌تواند خارج از بازه معاملات باشد
    out["close_position"] = _safe_div(df["close_raw"] - df["low_raw"], day_range)
    out["final_vs_last_pct"] = _safe_div(official, df["close_raw"]) - 1.0   # فشار خرید/فروش انتهای بازار
    out["value_ratio_20d"] = _safe_div(df["value"], df["value"].shift(1).rolling(20, min_periods=15).mean())
    # تعداد روزهای معاملاتی از اولین ردیف آرشیو (آرشیو TSETMC از ۱۳۸۰ شروع می‌شود؛ برای سهم‌های قدیمی‌تر تاریخ عرضه نیست)
    out["days_since_listing"] = np.arange(len(df))

    # ─── صف قفل ───
    locks = market_rules.lock_flags(df)
    out["locked_up"] = locks["locked_up"].astype(int)
    out["locked_down"] = locks["locked_down"].astype(int)
    out["lock_up_streak"] = _streak(locks["locked_up"])
    out["lock_down_streak"] = _streak(locks["locked_down"])

    # ─── پول هوشمند (حقیقی/حقوقی) ───
    if client_types is not None and not client_types.empty:
        ct = df[["date", "value", "volume"]].merge(client_types, on="date", how="left")
        net_real = ct["buy_i_value"] - ct["sell_i_value"]
        out["real_flow_ratio"] = _safe_div(net_real, ct["value"])
        flow_known = np.isfinite(net_real) & np.isfinite(ct["value"]) & (ct["value"] > 0)
        out["real_flow_ratio_5d"] = _safe_div(net_real.where(flow_known).rolling(5, min_periods=3).sum(),
                                              ct["value"].where(flow_known).rolling(5, min_periods=3).sum())
        per_cap_buy = _safe_div(ct["buy_i_value"], ct["buy_i_count"])
        per_cap_sell = _safe_div(ct["sell_i_value"], ct["sell_i_count"])
        out["buyer_power"] = _safe_div(per_cap_buy, per_cap_sell)
        power_columns = ["buy_i_value", "buy_i_count", "sell_i_value", "sell_i_count"]
        power_known = np.isfinite(ct[power_columns]).all(axis=1) & (ct[power_columns] >= 0).all(axis=1)
        # An observed zero-count/zero-value day is known and belongs to aggregate totals.
        power_known &= (ct["buy_i_count"] > 0) == (ct["buy_i_value"] > 0)
        power_known &= (ct["sell_i_count"] > 0) == (ct["sell_i_value"] > 0)
        totals = ct[power_columns].where(power_known, axis=0).rolling(5, min_periods=3).sum()
        out["buyer_power_5d"] = _safe_div(_safe_div(totals["buy_i_value"], totals["buy_i_count"]),
                                            _safe_div(totals["sell_i_value"], totals["sell_i_count"]))
        out["per_capita_buy_ratio_20d"] = _safe_div(per_cap_buy, per_cap_buy.shift(1).rolling(20, min_periods=15).mean())
        # مثبت یعنی حقوقی‌ها به حقیقی‌ها فروخته‌اند (تغییر مالکیت حقوقی به حقیقی)
        out["legal_to_real_ratio"] = _safe_div(ct["sell_n_volume"] - ct["buy_n_volume"], ct["volume"])
    else:
        for col in FLOW_FEATURES:
            out[col] = np.nan

    return out[["date"] + DAILY_FEATURES]
