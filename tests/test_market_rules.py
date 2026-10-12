# -*- coding: utf-8 -*-
"""
آزمون‌های قوانین ریزساختار بازار ایران (کارمزد، صف قفل، بازده Point-in-Time، تعدیل قیمت)
"""

import pandas as pd
import pytest

from core import market_rules


def _frame(rows):
    cols = ["date", "low_raw", "high_raw", "close_raw", "close_final_raw", "ref_price_raw", "volume", "value"]
    return pd.DataFrame(rows, columns=cols)


def test_breakeven_and_net_return_with_official_fees():
    # (1 + 0.003712) / (1 - 0.0088) - 1 ≈ 1.2622٪
    assert market_rules.breakeven_move() == pytest.approx(0.012622, abs=1e-5)
    assert market_rules.net_trade_return(10_000, 10_000) == pytest.approx(-0.012467, abs=1e-5)
    assert market_rules.net_trade_return(10_000, 10_000 * (1 + market_rules.breakeven_move())) == pytest.approx(0.0, abs=1e-12)


def test_lock_flags_detect_full_day_queues_only():
    df = _frame([
        ("2026-10-05", 3590, 3800, 3800, 3770, 3690, 100, 1),   # بسته شدن در سقف ولی معامله در بازه → قفل نیست
        ("2026-10-06", 3880, 3880, 3880, 3880, 3770, 100, 1),   # کل روز در سقف → صف خرید قفل
        ("2026-10-07", 3770, 3770, 3770, 3770, 3880, 100, 1),   # کل روز در کف → صف فروش قفل
        ("2026-10-08", 3770, 3770, 3770, 3770, 3770, 100, 1),   # یک قیمت ولی بدون تغییر → قفل نیست
    ])
    flags = market_rules.lock_flags(df)
    assert flags["locked_up"].tolist() == [False, True, False, False]
    assert flags["locked_down"].tolist() == [False, False, True, False]


def test_lock_uses_traded_price_not_official_close():
    """
    باگ پیدا‌شده روی داده واقعی: وقتی حجم روز کمتر از حجم مبنا است، قیمت پایانی رسمی فقط بخشی از حرکت را
    نشان می‌دهد. صف قفل کم‌حجم با قیمت پایانی دیده نمی‌شد (بیش از نیمی از روزهای قفل تاریخ بازار).
    """
    df = _frame([
        ("2021-03-01", 3880, 3880, 3880, 3790, 3770, 5_000, 1),   # صف خرید قفل، پایانی فقط +۰.۵٪
        ("2021-03-02", 3714, 3714, 3714, 3770, 3790, 9_000, 1),   # دامنه منفی ۲٪ (۱۳۹۹): صف فروش قفل
        ("2021-03-03", 3725, 3725, 3725, 3725, 3714, 300, 1),     # یک معامله تک‌قیمتی +۰.۳٪ → قفل نیست
    ])
    flags = market_rules.lock_flags(df)
    assert flags["locked_up"].tolist() == [True, False, False]
    assert flags["locked_down"].tolist() == [False, True, False]


def test_pit_returns_remove_corporate_action_gap():
    """در روز افزایش سرمایه، قیمت مرجع تعدیل می‌شود؛ بازده PIT نباید افت ۲۸٪ کاذب نشان دهد."""
    df = _frame([
        ("2025-03-11", 5450, 5560, 5530, 5530, 5480, 10, 1),
        ("2025-03-12", 3950, 4100, 4050, 4000, 3982, 10, 1),
    ])
    returns = market_rules.pit_daily_returns(df)
    assert returns.iloc[1] == pytest.approx(4000 / 3982 - 1)
    naive = df["close_final_raw"].pct_change().iloc[1]
    assert naive < -0.27  # همان چیزی که بدون قیمت مرجع به‌اشتباه محاسبه می‌شد


def test_corporate_action_factor_and_backward_adjustment():
    df = _frame([
        ("d1", 0, 0, 0, 5000, 4900, 1, 1),
        ("d2", 0, 0, 0, 5530, 5000, 1, 1),
        ("d3", 0, 0, 0, 4000, 3982, 1, 1),  # اقدام شرکتی: 3982 / 5530
        ("d4", 0, 0, 0, 4100, 4000, 1, 1),
    ])
    factors = market_rules.corporate_action_factors(df)
    assert factors.tolist() == pytest.approx([1.0, 1.0, 3982 / 5530, 1.0])
    adjusted = market_rules.backward_adjusted_close(df)
    assert adjusted.iloc[3] == 4100 and adjusted.iloc[2] == 4000
    assert adjusted.iloc[1] == pytest.approx(5530 * 3982 / 5530)
    assert adjusted.iloc[0] == pytest.approx(5000 * 3982 / 5530)


def test_invalid_vwap_stays_unknown():
    df = pd.DataFrame({"low_raw": [100.0, 100.0], "high_raw": [110.0, 110.0],
                       "volume": [10, 0], "value": [1500.0, 0.0]})
    vwap = market_rules.daily_vwap(df)
    assert pd.isna(vwap.iloc[0])          # داده خراب (۱۵۰) قیمت اجرای فرضی تولید نمی‌کند
    assert pd.isna(vwap.iloc[1])          # روز بدون حجم میانگین ندارد


def test_execution_bounds():
    assert market_rules.apply_execution_bounds(3883.88, 3880, 3880) == 3880
    assert market_rules.apply_execution_bounds(95.0, 100.0, 110.0) == 100.0
    assert market_rules.apply_execution_bounds(105.0, None, None) == 105.0
