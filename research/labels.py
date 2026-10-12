# -*- coding: utf-8 -*-
"""
برچسب‌های پژوهشی (Labels) — «اگر در پایان روز t تصمیم به خرید می‌گرفتیم، واقعاً چقدر سود خالص می‌بردیم؟»

قرارداد اجرا (همان موتور بکتست؛ هیچ قیمتی که در عمل قابل معامله نبوده استفاده نمی‌شود):
- تصمیم: پایان روز t (فقط با داده تا پایان t). معامله با قیمت پایانی روز t ممکن نیست، چون بعد از بسته شدن
  بازار محاسبه می‌شود.
- ورود: «روز معاملاتی بعدی بازار» به میانگین موزون قیمت آن روز (ارزش ÷ حجم) + لغزش. اگر نماد آن روز معامله
  نداشت (توقف) یا صف خرید قفل بود، ورود ممکن نیست (برچسب خالی، ولی ثبت می‌شود: خودش اطلاعات مهمی است).
- خروج: h جلسه معاملاتی بازار بعد از روز ورود، به میانگین موزون آن روز − لغزش. اگر آن روز صف فروش قفل یا نماد
  متوقف بود، خروج به اولین روز قابل فروش بعدی منتقل می‌شود (گیر افتادن در صف/توقف، ریسک واقعی است).
- اقدام شرکتی بین ورود و خروج با حفظ ارزش (تعدیل تعداد سهم) — همان قرارداد بکتست و بازده PIT.
- کارمزد خرید و فروش از config (منبع یکتا).

خروجی هر ردیف (نماد، روز تصمیم): بازده خالص هر افق (و نسخه «بدترین اجرا»: خرید در سقف و فروش در کف
همان روزها)، بیشترین سود و زیان شناور در طول نگهداری (MFE/MAE) بر
اساس قیمت‌های بیشترین/کمترین، تأخیر خروج و ارزش معاملات روز تصمیم (برای فیلتر نقدشوندگی).
"""

from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from config import settings
from core import market_rules

LABEL_SET_VERSION = "labels-v1"   # هر تغییر در تعریف برچسب → نسخه جدید
DEFAULT_HORIZONS = (1, 2, 3, 5)
CALENDAR_MIN_SYMBOLS = 20          # روزی «روز معاملاتی بازار» است که دست‌کم این تعداد نماد معامله داشته باشند


def market_calendar(prices: pd.DataFrame, min_symbols: Optional[int] = None) -> np.ndarray:
    """تقویم معاملاتی بازار از خود داده: روزهایی که دست‌کم min_symbols نماد معامله داشته‌اند (مرتب)."""
    min_symbols = CALENDAR_MIN_SYMBOLS if min_symbols is None else min_symbols
    counts = prices.loc[prices["volume"] > 0].groupby("date").size()
    return np.array(sorted(counts.index[counts >= min_symbols]))


def _symbol_labels(df: pd.DataFrame, calendar: np.ndarray, horizons: Iterable[int],
                   buy_cost: float, sell_keep: float) -> pd.DataFrame:
    """برچسب‌های یک نماد. df: ردیف‌های همان نماد، مرتب بر اساس تاریخ، فقط روزهای دارای معامله."""
    n = len(df)
    dates = df["date"].to_numpy().astype(str)
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
    entry_executable = j_ok & ~locked_up[j_safe] & np.isfinite(vwap[j_safe])
    entry_price = np.where(entry_executable, vwap[j_safe] * (1 + buy_cost), np.nan)
    out["entry_executable"] = entry_executable

    # اولین ردیف «قابل فروش» از هر ردیف به بعد (نه صف فروش قفل، با میانگین موزون معتبر)
    sellable = ~locked_down & np.isfinite(vwap)
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
        share_mult = np.exp(-(cum[k_safe] - cum[j_safe]))              # تعدیل تعداد سهم پس از اقدام شرکتی
        gross = np.where(valid, vwap[k_safe] * sell_keep * share_mult / entry_price, np.nan)
        out[f"net_ret_{h}d"] = gross - 1.0
        # آزمون فشار اجرا: خرید در بیشترین و فروش در کمترین قیمت همان روزها (بدترین حالت ممکن روزانه)
        worst = np.where(valid, low[k_safe] * sell_keep * share_mult / (high[j_safe] * (1 + buy_cost)), np.nan)
        out[f"net_ret_{h}d_worst"] = worst - 1.0
        # بدترین و بهترین وضعیت شناور در پنجره نگهداری برنامه‌ریزی‌شده [ورود، خروج برنامه‌ریزی‌شده]
        end = np.minimum(p, n - 1)
        mfe = np.full(n, -np.inf)
        mae = np.full(n, np.inf)
        for offset in range(h + 1):
            m = np.minimum(j_safe + offset, n - 1)
            inside = valid & (j_safe + offset <= end)
            mult = np.exp(-(cum[m] - cum[j_safe]))
            mfe = np.where(inside, np.maximum(mfe, high[m] * mult), mfe)
            mae = np.where(inside, np.minimum(mae, low[m] * mult), mae)
        out[f"mfe_{h}d"] = np.where(valid, mfe / entry_price - 1.0, np.nan)
        out[f"mae_{h}d"] = np.where(valid, mae / entry_price - 1.0, np.nan)
        # تأخیر خروج (به تعداد جلسه بازار) به خاطر صف فروش یا توقف نماد
        exit_date = np.where(valid, dates[k_safe], "")
        delay = np.searchsorted(calendar, exit_date) - planned_pos
        out[f"exit_delay_{h}d"] = np.where(valid, delay, -1)
    return pd.DataFrame(out)


def compute_labels(prices: pd.DataFrame, horizons: Iterable[int] = DEFAULT_HORIZONS,
                   calendar: Optional[np.ndarray] = None) -> pd.DataFrame:
    """
    برچسب همه نمادها. prices: ستون‌های symbol، date، low_raw، high_raw، close_final_raw، ref_price_raw، volume،
    value (خروجی خام دیتابیس). خروجی: یک ردیف برای هر (نماد، روز تصمیم).
    """
    horizons = tuple(sorted(set(int(h) for h in horizons)))
    if any(h < 1 for h in horizons):
        raise ValueError("افق برچسب باید حداقل یک جلسه باشد")
    prices = prices.loc[prices["volume"] > 0].sort_values(["symbol", "date"])
    if prices.duplicated(["symbol", "date"]).any():
        raise ValueError("برای هر نماد و تاریخ فقط یک ردیف قیمت مجاز است")
    calendar = (market_calendar(prices) if calendar is None else np.asarray(calendar)).astype(str)
    slip = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10_000.0
    buy_cost = slip
    sell_keep = (1.0 - slip) * (1.0 - settings.FEES["sell"]) / (1.0 + settings.FEES["buy"])
    frames: List[pd.DataFrame] = []
    for symbol, df in prices.groupby("symbol", sort=False):
        labels = _symbol_labels(df.reset_index(drop=True), calendar, horizons, buy_cost, sell_keep)
        labels.insert(1, "symbol", symbol)
        frames.append(labels)
    if not frames:
        return pd.DataFrame(columns=["date", "symbol"])
    return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
