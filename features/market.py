# -*- coding: utf-8 -*-
"""
فیچرهای مقطعی کل بازار (Market Context)

برای هر روز معاملاتی، از فیچرهای روزانه همه نمادهای همان روز ساخته می‌شوند (در پایان روز t در دسترس‌اند):
- mkt_breadth: نسبت نمادهای مثبت
- mkt_ret_median: میانه بازده روز
- mkt_locked_up_ratio / mkt_locked_down_ratio: سهم نمادهای صف خرید/فروش قفل
- mkt_real_flow_ratio: ورود پول حقیقی ÷ ارزش معاملات همان نمادهایی که آمار حقیقی/حقوقی دارند
- mkt_breadth_5d: میانگین پهنای بازار ۵ روز اخیر
و فیچرهای نسبی هر نماد نسبت به بازار:
- rel_ret_5d: بازده ۵ روزه نماد منهای میانه بازده ۵ روزه بازار همان روز
"""

import pandas as pd

from core.client_quality import finite_number

MARKET_FEATURES = [
    "mkt_breadth", "mkt_breadth_5d", "mkt_ret_median", "mkt_locked_up_ratio", "mkt_locked_down_ratio",
    "mkt_real_flow_ratio", "mkt_symbols",
]
RELATIVE_FEATURES = ["rel_ret_5d"]


def compute_market_features(panel: pd.DataFrame) -> pd.DataFrame:
    """
    panel: فیچرهای روزانه همه نمادها با ستون‌های date، symbol، ret_1d، ret_5d، locked_up، locked_down و
    (اختیاری) real_net_value و value. خروجی: یک ردیف برای هر تاریخ.
    """
    grouped = panel.groupby("date")
    market = pd.DataFrame({
        "mkt_breadth": grouped["ret_1d"].apply(lambda r: (r > 0).sum() / r.notna().sum() if r.notna().any() else float("nan")),
        "mkt_ret_median": grouped["ret_1d"].median(),
        "mkt_locked_up_ratio": grouped["locked_up"].mean(),
        "mkt_locked_down_ratio": grouped["locked_down"].mean(),
        "mkt_symbols": grouped["symbol"].count(),
    })
    if {"real_net_value", "value"}.issubset(panel.columns):
        # صورت و مخرج از یک مجموعه نماد: نمادهای بدون آمار حقیقی/حقوقی نباید فقط مخرج را بزرگ کنند
        net, value = finite_number(panel, 'real_net_value'), finite_number(panel, 'value')
        known = net.notna() & (value > 0) & (net.abs() <= value + value.abs()*1e-6 + 1.)
        matched = panel[['date']].assign(real_net_value=net, value=value).loc[known]
        flows = matched.groupby("date")[["real_net_value", "value"]].sum()
        flows["value"] = flows["value"].where(flows["value"] > 0)
        covered = known.groupby(panel['date']).sum()
        # روزهایی که داده حقیقی/حقوقی کمتر از نیمی از نمادها منتشر شده، نامعتبر حساب می‌شوند
        market["mkt_real_flow_ratio"] = (flows["real_net_value"] / flows["value"]).where(covered >= 0.5 * market["mkt_symbols"])
    else:
        market["mkt_real_flow_ratio"] = float("nan")
    market = market.sort_index()
    market["mkt_breadth_5d"] = market["mkt_breadth"].rolling(5, min_periods=3).mean()
    return market.reset_index()[["date"] + MARKET_FEATURES]


def add_relative_features(panel: pd.DataFrame) -> pd.DataFrame:
    """فیچرهای نسبی هر نماد نسبت به همان روز بازار."""
    median_5d = panel.groupby("date")["ret_5d"].transform("median")
    panel = panel.copy()
    panel["rel_ret_5d"] = panel["ret_5d"] - median_5d
    return panel
