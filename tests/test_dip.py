# -*- coding: utf-8 -*-
"""
آزمون‌های استراتژی «خرید در اصلاح» (روش کاربر) با داده روزانه آزمونی (بدون اینترنت)
"""

import numpy as np
import pandas as pd
import pytest

from config import settings
from research.dip import DipRule, simulate_symbol

BUY, SELL = settings.FEES["buy"], settings.FEES["sell"]
SLIP = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10_000
KEEP = (1 - SELL) / (1 + BUY)
CAL = np.array([f"2026-06-{d:02d}" for d in range(1, 11)])


def _frame(rows):
    """rows: (تاریخ، باز، بیشترین، کمترین، پایانی، مرجع)؛ ارزش = حجم × پایانی."""
    return pd.DataFrame([{"date": d, "open_raw": o, "high_raw": h, "low_raw": lo, "close_raw": c,
                          "close_final_raw": c, "ref_price_raw": r, "volume": 1000, "value": 1000 * c}
                         for d, o, h, lo, c, r in rows])


def _flat(n=10):
    return [(CAL[i], 100, 101, 99, 100, 100) for i in range(n)]


def _result(rows, **kw):
    out = simulate_symbol(_frame(rows), CAL, DipRule(**kw)).set_index("date")
    return out.loc[CAL[0]]


def test_limit_buy_at_minus_two_then_target_next_day():
    rows = _flat()
    rows[1] = (CAL[1], 100, 100.5, 97.5, 98, 100)       # روز ورود: کف ۹۷.۵ ← خرید محدود در ۹۸
    rows[2] = (CAL[2], 98.5, 101.5, 98, 101, 98)        # فردا سقف ۱۰۱.۵ ≥ ۹۸×۱.۰۳ = ۱۰۰.۹۴
    r = _result(rows, dip=0.02, take_profit=0.03, hold=1)
    assert r["dip_status"] == "complete" and r["entry_kind"] == "limit" and r["exit_kind"] == "target"
    assert r["net_ret"] == pytest.approx(98 * 1.03 * KEEP / 98 - 1)
    assert r["days_held"] == 1                          # جلسه‌های بعد از روز خرید (همان روز = ۰)


def test_gap_down_fills_at_open_and_no_fill_when_dip_not_reached():
    rows = _flat()
    rows[1] = (CAL[1], 97, 99, 96.5, 98, 100)           # گشایش ۹۷ پایین‌تر از ۹۸ ← خرید در گشایش
    r = _result(rows, dip=0.02, take_profit=0.03, hold=1)
    assert r["entry_kind"] == "open"
    rows[1] = (CAL[1], 100, 101, 98.5, 100, 100)        # کف ۹۸.۵ هرگز به ۹۸ نرسید
    assert _result(rows, dip=0.02)["dip_status"] == "not_filled"


def test_same_day_target_depends_on_mode_after_intraday_dip():
    rows = _flat()
    rows[1] = (CAL[1], 100, 101.5, 97.5, 100, 100)      # هم کف (خرید) و هم سقف ≥ هدف؛ ترتیب نامعلوم
    cons = _result(rows, dip=0.02, take_profit=0.03, hold=1, conservative=True)
    opt = _result(rows, dip=0.02, take_profit=0.03, hold=1, conservative=False)
    assert opt["exit_kind"] == "target" and opt["days_held"] == 0
    assert cons["exit_kind"] == "target" and cons["days_held"] == 1   # محافظه‌کار: هدف همان روز پذیرفته نمی‌شود


def test_conservative_touch_is_not_a_fill():
    rows = _flat()
    rows[1] = (CAL[1], 100, 100.5, 98.0, 99, 100)       # فقط لمس دقیق ۹۸
    assert _result(rows, dip=0.02, conservative=True)["dip_status"] == "not_filled"
    assert _result(rows, dip=0.02, conservative=False)["dip_status"] == "complete"


def test_time_exit_and_stuck_in_sell_queue():
    rows = _flat()
    rows[1] = (CAL[1], 99, 100, 97.5, 98, 100)          # خرید ۹۸
    rows[2] = (CAL[2], 95.1, 95.1, 95.1, 95.1, 98)      # روز خروج: صف فروش قفل (−۳٪)
    rows[3] = (CAL[3], 94, 96, 93, 95, 95.1)            # اولین روز قابل فروش: خروج زمانی دیرهنگام
    r = _result(rows, dip=0.02, take_profit=0.03, hold=1)
    assert r["exit_kind"] == "time_late" and r["days_held"] == 2
    assert r["net_ret"] == pytest.approx(max(95 * (1 - SLIP), 93) * KEEP / 98 - 1)


def test_stop_loss_and_halted_entry_day():
    rows = _flat()
    rows[1] = (CAL[1], 99, 100, 97.5, 98, 100)
    rows[2] = (CAL[2], 97, 98, 94, 95, 98)              # کف ۹۴ ≤ حد ضرر ۹۸×۰.۹۷=۹۵.۰۶
    r = _result(rows, dip=0.02, take_profit=0.03, hold=1, stop_loss=0.03)
    assert r["exit_kind"] == "stop" and r["net_ret"] == pytest.approx(0.97 * KEEP - 1)
    halted = [row for k, row in enumerate(_flat()) if k != 1]   # فردای تصمیم نماد معامله ندارد
    assert _result(halted, dip=0.02)["dip_status"] == "no_session"
