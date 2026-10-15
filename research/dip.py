# -*- coding: utf-8 -*-
"""
استراتژی «خرید در اصلاح» (روش معاملاتی کاربر) — فاز ۱-ج، بخش ۲

ایده: پایان روز t بهترین نامزدها انتخاب می‌شوند. روز t+1 سفارش خرید محدود (Limit) در «قیمت مرجع × (۱ − d)»
گذاشته می‌شود؛ اگر سهم تا آنجا پایین آمد خریده می‌شود و با سفارش فروش در «قیمت خرید × (۱ + tp)» همان روز یا
روزهای بعد فروخته می‌شود. اگر هدف تا پایان روز t+1+H نرسید، فروش به میانگین قیمت همان روز (خروج زمانی).

قواعد اجرا با داده روزانه (OHLC):
- خرید: اگر قیمت اولین معامله (open) ≤ قیمت سفارش ← خرید به همان open (حراج گشایش)؛ وگرنه اگر کمترین قیمت روز
  به قیمت سفارش رسید ← خرید به قیمت سفارش؛ وگرنه خرید انجام نمی‌شود (سرمایه بیکار).
  حالت «محافظه‌کار»: کمترین قیمت باید دست‌کم ۰.۱٪ از قیمت سفارش پایین‌تر رفته باشد (لمس صرف یعنی شاید نوبت به ما نرسد).
- هدف سود در همان روز خرید: ترتیب سقف و کف در داده روزانه معلوم نیست. اگر خرید به open بوده، هر سقف بعدی قطعاً
  بعد از خرید است (معتبر). اگر خرید وسط روز بوده: «خوش‌بینانه» یعنی سقف بعد از کف آمده، «محافظه‌کار» یعنی نه.
- روزهای بعد: گشایش بالاتر از هدف ← فروش به open؛ وگرنه اگر سقف روز به هدف رسید ← فروش به هدف.
  حد ضرر (اختیاری) قرینه همین است؛ اگر در یک روز هم هدف و هم حد ضرر لمس شد، حالت محافظه‌کار اول حد ضرر را فرض می‌کند.
- روز صف فروش قفل نمی‌توان فروخت (گیر افتادن)؛ روز اقدام شرکتی، سطح‌ها با ضریب قیمت مرجع تعدیل می‌شوند.
- کارمزد خرید و فروش از config؛ سفارش محدود لغزش ندارد، خروج زمانی به میانگین قیمت روز − لغزش.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config import settings
from core import market_rules

DIP_VERSION = "dip-v3"
THROUGH_MARGIN = 0.001   # حالت محافظه‌کار: کف روز باید این مقدار از قیمت سفارش پایین‌تر رفته باشد


@dataclass(frozen=True)
class DipRule:
    dip: float = 0.02            # فاصله قیمت سفارش خرید از قیمت مرجع روز ورود
    take_profit: float = 0.03    # هدف سود نسبت به قیمت خرید
    hold: int = 1                # حداکثر چند جلسه بعد از روز خرید (۱ = «امروز یا فردا»)
    stop_loss: Optional[float] = None
    conservative: bool = True

    def __post_init__(self):
        if not np.isfinite(self.dip) or not 0 < self.dip < 1 or not np.isfinite(self.take_profit) or self.take_profit <= 0:
            raise ValueError("افت خرید و هدف باید محدود و مثبت باشند؛ افت خرید کمتر از یک است")
        if isinstance(self.hold, bool) or not isinstance(self.hold, (int, np.integer)) or self.hold < 1:
            raise ValueError("افق نگهداری باید تعداد صحیح و مثبت جلسه باشد؛ افق صفر با OHLC روزانه پشتیبانی نمی‌شود")
        if self.stop_loss is not None and (not np.isfinite(self.stop_loss) or not 0 < self.stop_loss < 1):
            raise ValueError("فاصله حد زیان باید بین صفر و یک باشد")
        if not isinstance(self.conservative, bool):
            raise ValueError("حالت محافظه‌کار باید بولی باشد")

    @property
    def name(self) -> str:
        sl = f"-sl{self.stop_loss * 100:.8g}%" if self.stop_loss else ""
        mode = "cons" if self.conservative else "opt"
        return f"dip{self.dip * 100:.8g}%-tp{self.take_profit * 100:.8g}%-h{self.hold}{sl}-{mode}"


def simulate_symbol(df: pd.DataFrame, calendar: np.ndarray, rule: DipRule,
                    want: Optional[np.ndarray] = None) -> pd.DataFrame:
    """
    نتیجه قاعده برای هر روز تصمیم یک نماد. df: ردیف‌های همان نماد (مرتب، فقط روزهای دارای معامله) با ستون‌های
    date، open_raw، high_raw، low_raw، close_final_raw، ref_price_raw، volume، value.
    خروجی: date، dip_status (no_session/not_filled/complete/censored/invalid)، entry_kind، exit_kind، net_ret و
    days_held (تعداد جلسه بعد از روز خرید تا فروش؛ فروش همان روز = ۰).
    """
    n = len(df)
    calendar = np.asarray(calendar).astype(str)
    if not len(calendar) or calendar.ndim != 1 or list(calendar) != sorted(set(calendar)):
        raise ValueError("تقویم باید یکتا، مرتب و غیرخالی باشد")
    if df["date"].duplicated().any() or not df["date"].is_monotonic_increasing:
        raise ValueError("تاریخ قیمت یک نماد باید یکتا و مرتب باشد")
    if want is not None and len(want) != n:
        raise ValueError("ماسک تصمیم با قیمت‌ها هم‌اندازه نیست")
    # لیست پایتون برای دسترسی تک‌عنصری در حلقه چند برابر سریع‌تر از آرایه numpy است
    dates = df["date"].astype(str).tolist()
    opens = df["open_raw"].astype(float).tolist()
    highs = df["high_raw"].astype(float).tolist()
    lows = df["low_raw"].astype(float).tolist()
    refs = df["ref_price_raw"].astype(float).tolist()
    vwap = market_rules.daily_vwap(df).astype(float).tolist()
    locked_down = market_rules.lock_flags(df)["locked_down"].tolist()
    factors = market_rules.corporate_action_factors(df).to_numpy(dtype=float)
    cum = np.cumsum(np.log(np.where(factors > 0, factors, 1.0))).tolist()
    want = [True] * n if want is None else list(want)
    isfinite = np.isfinite
    slip = settings.BACKTEST_CONFIG["default_slippage_bps"] / 10_000.0
    keep = (1.0 - settings.FEES["sell"]) / (1.0 + settings.FEES["buy"])

    status = np.full(n, "no_session", dtype=object)
    entry_kind = np.full(n, "", dtype=object)
    exit_kind = np.full(n, "", dtype=object)
    net = np.full(n, np.nan)
    held = np.full(n, np.nan)
    entry_dates, exit_dates = np.full(n, "", dtype=object), np.full(n, "", dtype=object)
    entry_prices, exit_prices = np.full(n, np.nan), np.full(n, np.nan)
    entry_filled, actions = np.full(n, np.nan), np.full(n, np.nan)

    cal_pos = np.searchsorted(calendar, np.asarray(dates), side="right").tolist()   # روز معاملاتی بعدی بازار
    calendar_list = calendar.tolist()
    for i in range(n):
        if not want[i]:
            status[i] = "skipped"
            continue
        if cal_pos[i] >= len(calendar):
            status[i] = "censored"
            continue
        entry_date = calendar_list[cal_pos[i]]
        j = i + 1
        if j >= n or dates[j] != entry_date:
            entry_filled[i] = 0
            continue                                                  # نماد فردا معامله نداشت (توقف)
        ref, op, lo, hi = refs[j], opens[j], lows[j], highs[j]
        if not (isfinite(ref) and ref > 0 and isfinite(op) and isfinite(lo) and isfinite(hi) and 0 < lo <= op <= hi):
            status[i] = "invalid"
            continue
        limit = ref * (1.0 - rule.dip)
        if op <= limit:
            entry, entry_kind[i] = op, "open"
        elif lo <= limit * (1.0 - (THROUGH_MARGIN if rule.conservative else 0.0)):
            entry, entry_kind[i] = limit, "limit"
        else:
            status[i] = "not_filled"
            entry_filled[i] = 0
            continue
        entry_filled[i], entry_dates[i], entry_prices[i] = 1, entry_date, entry
        target = entry * (1.0 + rule.take_profit)
        stop = entry * (1.0 - rule.stop_loss) if rule.stop_loss else None
        # همان روز خرید: سقف بعد از خرید قطعی است فقط اگر خرید در گشایش بوده. کفِ پایین‌تر از حد ضرر همیشه بعد از
        # خرید است (قیمت برای رسیدن به آن از قیمت سفارش عبور کرده است).
        same_day_target = not locked_down[j] and (entry_kind[i] == "open" or not rule.conservative) and hi >= target
        same_day_stop = bool(stop) and lo <= stop and not locked_down[j]
        exit_price, exit_row = None, None
        if same_day_stop and (rule.conservative or not same_day_target):
            exit_price, exit_row, exit_kind[i] = stop, j, "stop"
        elif same_day_target:
            exit_price, exit_row, exit_kind[i] = target, j, "target"
        planned_pos = cal_pos[i] + rule.hold
        if exit_price is None and planned_pos >= len(calendar):
            status[i] = "censored"
            continue
        planned_date = calendar_list[min(planned_pos, len(calendar) - 1)]
        m = j + 1
        while exit_price is None and m < n:
            if not (isfinite(refs[m]) and refs[m] > 0 and isfinite(factors[m])
                    and isfinite(opens[m]) and isfinite(lows[m]) and isfinite(highs[m])
                    and 0 < lows[m] <= opens[m] <= highs[m]):
                status[i] = "invalid"
                break
            scale = float(np.exp(cum[m] - cum[j])) if cum[m] != cum[j] else 1.0   # تعدیل سطح‌ها در روز اقدام شرکتی
            tgt, stp = target * scale, (stop * scale if stop else None)
            if dates[m] <= planned_date:
                can_sell = not locked_down[m]
                if can_sell and opens[m] >= tgt:
                    exit_price, exit_kind[i] = opens[m], "target_gap"
                elif stp and can_sell and opens[m] <= stp:
                    exit_price, exit_kind[i] = opens[m], "stop_gap"
                elif stp and can_sell and lows[m] <= stp and (rule.conservative or highs[m] < tgt):
                    exit_price, exit_kind[i] = stp, "stop"
                elif can_sell and highs[m] >= tgt:
                    exit_price, exit_kind[i] = tgt, "target"
                elif dates[m] == planned_date and can_sell and isfinite(vwap[m]):
                    exit_price, exit_kind[i] = max(vwap[m] * (1.0 - slip), lows[m]), "time"
            elif not locked_down[m] and isfinite(vwap[m]):
                # روز برنامه‌ریزی‌شده گذشت (صف فروش یا توقف): اولین روز قابل فروش
                exit_price, exit_kind[i] = max(vwap[m] * (1.0 - slip), lows[m]), "time_late"
            if exit_price is not None:
                exit_row = m
                break
            m += 1
        if exit_price is None:
            if status[i] != "invalid":
                status[i] = "censored"
            exit_kind[i] = ""
            continue
        share_mult = float(np.exp(-(cum[exit_row] - cum[j])))
        net[i] = exit_price * share_mult * keep / entry - 1.0
        held[i] = int(np.searchsorted(calendar, dates[exit_row])) - cal_pos[i]
        status[i] = "complete"
        exit_dates[i], exit_prices[i] = dates[exit_row], exit_price
        actions[i] = np.count_nonzero(np.abs(factors[j + 1:exit_row + 1] - 1) > 1e-6)
    return pd.DataFrame({"date": dates, "dip_status": status, "entry_kind": entry_kind,
                         "exit_kind": exit_kind, "net_ret": net, "days_held": held,
                         "entry_filled": entry_filled, "entry_date": entry_dates, "exit_date": exit_dates,
                         "entry_price_rial": entry_prices, "exit_price_rial": exit_prices,
                         "corporate_actions": actions})


def simulate(prices: pd.DataFrame, calendar: np.ndarray, rule: DipRule,
             decisions: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    قاعده برای همه نمادها. prices: ستون symbol + ستون‌های simulate_symbol. decisions (اختیاری): فقط این
    (date، symbol)ها شبیه‌سازی می‌شوند (سرعت)؛ بقیه وضعیت skipped می‌گیرند.
    """
    calendar = np.asarray(calendar).astype(str)
    wanted = None if decisions is None else set(zip(decisions["date"], decisions["symbol"]))
    frames = []
    for symbol, df in prices.loc[prices["volume"] > 0].sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        df = df.reset_index(drop=True)
        want = None if wanted is None else np.array([(d, symbol) in wanted for d in df["date"]])
        if want is not None and not want.any():
            continue
        out = simulate_symbol(df, calendar, rule, want)
        out.insert(1, "symbol", symbol)
        frames.append(out)
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", "dip_status", "entry_kind", "exit_kind", "net_ret", "days_held"])
    return pd.concat(frames, ignore_index=True)
