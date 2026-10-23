# -*- coding: utf-8 -*-
"""
برچسب‌های پژوهشی (Labels) — «اگر در پایان روز t تصمیم به خرید می‌گرفتیم، واقعاً چقدر سود خالص می‌بردیم؟»

قرارداد اجرا (همان موتور بکتست؛ قیمت در بازه روز محدود می‌شود؛ پرشدن واقعی سفارش از OHLC اثبات نمی‌شود):
- تصمیم: پایان روز t (فقط با داده تا پایان t). معامله با قیمت پایانی روز t ممکن نیست، چون بعد از بسته شدن
  بازار محاسبه می‌شود.
- ورود: «روز معاملاتی بعدی بازار» به میانگین موزون قیمت آن روز (ارزش ÷ حجم) + لغزش. اگر نماد آن روز ردیف
  معامله نداشت یا صف خرید قفل بود، ورود در مدل انجام نمی‌شود؛ نبود ردیف به‌تنهایی توقف نماد را اثبات نمی‌کند.
- خروج: h جلسه معاملاتی بازار بعد از روز ورود، به میانگین موزون آن روز − لغزش. اگر آن روز صف فروش قفل یا نماد
  متوقف بود، خروج به اولین روز قابل فروش بعدی منتقل می‌شود (گیر افتادن در صف/توقف، ریسک واقعی است).
- اقدام شرکتی با ضریب مصنوعی تعداد سهم مدل می‌شود؛ نوع رویداد، زمان وصول سود و قابلیت فروش سهام جدید تأیید نشده‌اند.
  شمار عبور از این رویداد جدا گزارش می‌شود؛ نتیجه، برآورد پژوهشی است.
- کارمزد خرید و فروش از config (منبع یکتا).
- فقط تاریخ داخل تقویم می‌تواند روز تصمیم/اجرای معتبر باشد. مشاهده خارج تقویم در فاصله تصمیم تا ورود
  یا در مسیر نگهداری تا خروج، نتیجه را invalid_data می‌کند؛ از مسیر تعدیل/بیشینه/کمینه حذف نمی‌شود.
  تعارض پس از خروج کامل، نتیجه قبلی را تغییر نمی‌دهد. تقویم استنباطی، تقویم رسمی تأییدشده نیست.

خروجی هر ردیف (نماد، روز تصمیم): بازده خالص هر افق (و نسخه «بدترین اجرا»: خرید در سقف و فروش در کف
همان روزها)، بیشترین سود و زیان شناور در طول نگهداری (MFE/MAE) بر
اساس قیمت‌های بیشترین/کمترین، تأخیر خروج و ارزش معاملات روز تصمیم (برای فیلتر نقدشوندگی).
"""

from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from config import settings
from core import market_rules

LABEL_SET_VERSION = "labels-v5"   # calendar-conflicted outcome paths remain unknown
DEFAULT_HORIZONS = (1, 2, 3, 5)
CALENDAR_MIN_SYMBOLS = 20          # روزی «روز معاملاتی بازار» است که دست‌کم این تعداد نماد معامله داشته باشند


def market_calendar(prices: pd.DataFrame, min_symbols: Optional[int] = None) -> np.ndarray:
    """تقویم معاملاتی بازار از خود داده: روزهایی که دست‌کم min_symbols نماد معامله داشته‌اند (مرتب)."""
    min_symbols = CALENDAR_MIN_SYMBOLS if min_symbols is None else min_symbols
    counts = prices.loc[prices["volume"] > 0].groupby("date")["symbol"].nunique()
    return np.array(sorted(counts.index[counts >= min_symbols]))


def _symbol_labels(df: pd.DataFrame, calendar: np.ndarray, horizons: Iterable[int],
                   buy_cost: float, sell_keep: float) -> pd.DataFrame:
    """برچسب‌های یک نماد. df: ردیف‌های همان نماد، مرتب بر اساس تاریخ، فقط روزهای دارای معامله."""
    n = len(df)
    dates = df["date"].to_numpy().astype(str)
    on_calendar = np.isin(dates, calendar)
    # Keep anomalous observations in the path; silently dropping them could change
    # the corporate-action chain and extrema while appearing to repair execution.
    off_calendar_cum = np.r_[0, np.cumsum(~on_calendar)]
    vwap = market_rules.daily_vwap(df).to_numpy(dtype=float)
    locks = market_rules.lock_flags(df)
    locked_up = locks["locked_up"].to_numpy()
    locked_down = locks["locked_down"].to_numpy()
    high = df["high_raw"].to_numpy(dtype=float)
    low = df["low_raw"].to_numpy(dtype=float)
    # لگاریتم تجمعی ضریب اقدام شرکتی: ضرب ضرایب روزهای (j, k] = exp(cum[k] - cum[j])
    factors = market_rules.corporate_action_factors(df).to_numpy(dtype=float)
    cum = np.cumsum(np.log(np.where(factors > 0, factors, 1.0)))

    out: Dict[str, np.ndarray] = {"date": dates, "value": df["value"].to_numpy(dtype=float)}

    # روز ورود = روز معاملاتی بعدی بازار؛ نماد باید همان روز معامله داشته باشد
    cal_pos = np.searchsorted(calendar, dates, side="right")          # اندیس روز بعدی بازار در تقویم
    has_next = cal_pos < len(calendar)
    entry_date = np.where(has_next, calendar[np.minimum(cal_pos, len(calendar) - 1)], "")
    j = np.searchsorted(dates, entry_date)                             # ردیف نماد در روز ورود
    j_ok = has_next & (j < n)
    j_safe = np.minimum(j, n - 1)
    j_ok &= dates[j_safe] == entry_date
    refs = df["ref_price_raw"].to_numpy(dtype=float)
    execution_valid = np.isfinite(vwap) & np.isfinite(refs) & (refs > 0)
    entry_observed_end = np.searchsorted(dates, entry_date, side="right")
    entry_calendar_conflict = ~on_calendar | (has_next & (
        off_calendar_cum[entry_observed_end] - off_calendar_cum[np.arange(n)] > 0))
    entry_executable = j_ok & ~locked_up[j_safe] & execution_valid[j_safe] & ~entry_calendar_conflict
    entry_price = np.where(entry_executable, np.minimum(vwap[j_safe] * (1 + buy_cost), high[j_safe]), np.nan)
    out["entry_executable"] = entry_executable
    out["entry_date"] = entry_date
    invalid_cum = np.cumsum(~execution_valid | ~np.isfinite(factors))
    action_cum = np.cumsum((np.abs(factors - 1.0) > 1e-6).astype(int))

    # اولین ردیف «قابل فروش» از هر ردیف به بعد (نه صف فروش قفل، با میانگین موزون معتبر)
    sellable = on_calendar & ~locked_down & execution_valid
    next_sellable = np.full(n + 1, n)
    for idx in range(n - 1, -1, -1):
        next_sellable[idx] = idx if sellable[idx] else next_sellable[idx + 1]

    for h in horizons:
        planned_pos = cal_pos + h
        planned_ok = planned_pos < len(calendar)
        planned_date = np.where(planned_ok, calendar[np.minimum(planned_pos, len(calendar) - 1)], "")
        p = np.searchsorted(dates, planned_date)                       # اولین ردیف نماد در/بعد از روز برنامه‌ریزی‌شده
        k = next_sellable[np.minimum(p, n)]
        valid = entry_executable & planned_ok & (p < n) & (k < n)
        k_safe = np.minimum(k, n - 1)
        status = np.full(n, "censored", dtype=object)
        status[has_next & (~j_ok | locked_up[j_safe])] = "entry_blocked"
        bad_entry = j_ok & ~execution_valid[j_safe]
        bad_window = valid & ((invalid_cum[k_safe] - invalid_cum[j_safe]) > 0)
        # When exit is not yet observable, a known calendar conflict in the held
        # prefix is still invalid data, not merely an unresolved future outcome.
        observed_end = np.where(planned_ok & (k < n), k, n - 1)
        calendar_window_conflict = entry_executable & (
            off_calendar_cum[observed_end + 1] - off_calendar_cum[j_safe] > 0)
        status[valid & ~bad_window] = "complete"
        status[bad_entry | bad_window | entry_calendar_conflict | calendar_window_conflict] = "invalid_data"
        valid &= status == "complete"
        out[f"label_status_{h}d"] = status
        out[f"planned_exit_date_{h}d"] = planned_date
        out[f"exit_date_{h}d"] = np.where(valid, dates[k_safe], "")
        out[f"corporate_actions_{h}d"] = np.where(valid, action_cum[k_safe] - action_cum[j_safe], np.nan)
        share_mult = np.exp(-(cum[k_safe] - cum[j_safe]))              # تعدیل تعداد سهم پس از اقدام شرکتی
        gross = np.where(valid, np.maximum(vwap[k_safe] * (1 - buy_cost), low[k_safe]) * sell_keep * share_mult / entry_price, np.nan)
        out[f"net_ret_{h}d"] = gross - 1.0
        # آزمون فشار اجرا: خرید در بیشترین و فروش در کمترین قیمت همان روزها (بدترین حالت ممکن روزانه)
        worst = np.where(valid, low[k_safe] * sell_keep * share_mult / high[j_safe], np.nan)
        out[f"net_ret_{h}d_worst"] = worst - 1.0
        # Entire actual holding interval, including sessions spent waiting for a sellable exit.
        # Sparse range queries avoid quadratic loops for long suspensions/locked queues.
        scale = np.exp(-cum)
        max_table, min_table = [high * scale], [low * scale]
        width = 2
        while width <= n:
            half = width // 2
            size = n - width + 1
            max_table.append(np.maximum(max_table[-1][:size], max_table[-1][half:half + size]))
            min_table.append(np.minimum(min_table[-1][:size], min_table[-1][half:half + size]))
            width *= 2
        levels = np.floor(np.log2(np.maximum(k_safe - j_safe + 1, 1))).astype(int)
        mfe, mae = np.full(n, np.nan), np.full(n, np.nan)
        for level in range(len(max_table)):
            mask = valid & (levels == level)
            left, right = j_safe[mask], k_safe[mask] - (1 << level) + 1
            multiplier = np.exp(cum[j_safe[mask]])
            mfe[mask] = np.maximum(max_table[level][left], max_table[level][right]) * multiplier
            mae[mask] = np.minimum(min_table[level][left], min_table[level][right]) * multiplier
        out[f"mfe_{h}d"] = np.where(valid, mfe / entry_price - 1.0, np.nan)
        out[f"mae_{h}d"] = np.where(valid, mae / entry_price - 1.0, np.nan)
        # تأخیر خروج (به تعداد جلسه بازار) به خاطر صف فروش یا توقف نماد
        exit_date = np.where(valid, dates[k_safe], "")
        delay = np.searchsorted(calendar, exit_date) - planned_pos
        out[f"exit_delay_{h}d"] = np.where(valid, delay, np.nan)
    return pd.DataFrame(out)


def compute_labels(prices: pd.DataFrame, horizons: Iterable[int] = DEFAULT_HORIZONS,
                   calendar: Optional[np.ndarray] = None) -> pd.DataFrame:
    """
    برچسب همه نمادها. prices: ستون‌های symbol، date، low_raw، high_raw، close_final_raw، ref_price_raw، volume،
    value (خروجی خام دیتابیس). خروجی: یک ردیف برای هر (نماد، روز تصمیم).
    """
    requested = tuple(horizons)
    if not requested or any(isinstance(h, bool) or not isinstance(h, (int, np.integer)) or h < 1 for h in requested):
        raise ValueError("افق برچسب باید عدد صحیح مثبت باشد")
    horizons = tuple(sorted(set(requested)))
    if prices.empty:
        return pd.DataFrame(columns=["date", "symbol"])
    if prices.duplicated(["symbol", "date"]).any():
        raise ValueError("برای هر نماد و تاریخ فقط یک ردیف قیمت مجاز است")
    prices = prices.loc[prices["volume"] > 0].sort_values(["symbol", "date"])
    calendar = (market_calendar(prices) if calendar is None else np.asarray(calendar)).astype(str)
    if calendar.ndim != 1 or not len(calendar):
        raise ValueError("تقویم معاملاتی کافی نیست؛ پوشش بازار را بررسی کنید یا تقویم صریح بدهید")
    parsed = pd.to_datetime(calendar, format="%Y-%m-%d", errors="coerce")
    if parsed.isna().any() or len(np.unique(calendar)) != len(calendar) or not np.array_equal(calendar, np.sort(calendar)):
        raise ValueError("تقویم باید تاریخ‌های معتبر، یکتا و مرتب داشته باشد")
    slip = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10_000.0
    buy_cost = slip
    sell_keep = (1.0 - settings.FEES["sell"]) / (1.0 + settings.FEES["buy"])
    frames: List[pd.DataFrame] = []
    for symbol, df in prices.groupby("symbol", sort=False):
        labels = _symbol_labels(df.reset_index(drop=True), calendar, horizons, buy_cost, sell_keep)
        labels.insert(1, "symbol", symbol)
        frames.append(labels)
    if not frames:
        return pd.DataFrame(columns=["date", "symbol"])
    return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
