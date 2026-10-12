# -*- coding: utf-8 -*-
"""
قوانین ریزساختار بازار سهام ایران (منبع یکتای حقیقت برای کل سامانه)

معنای دقیق ستون‌های قیمت روزانه (همگی خام و تعدیل‌نشده — Point-in-Time):
- open_raw / high_raw / low_raw : اولین، بیشترین و کمترین قیمت معامله‌شده روز
- close_raw                     : قیمت آخرین معامله (pDrCotVal)
- close_final_raw               : قیمت پایانی رسمی (pClosing) — مبنای دامنه نوسان روز بعد
- ref_price_raw                 : قیمت مرجع روز (priceYesterday) = قیمت پایانی دیروز؛ در روز اقدام
                                  شرکتی (افزایش سرمایه/سود نقدی) توسط بورس تعدیل شده است

نکته کلیدی: بازده روزانه = close_final_raw / ref_price_raw - 1
این فرمول فقط از داده‌های همان روز استفاده می‌کند (بدون نگاه به آینده) و اثر افزایش سرمایه و
سود نقدی را خودبه‌خود حذف می‌کند، چون قیمت مرجع در روز مجمع/افزایش سرمایه تعدیل می‌شود.
"""

from typing import Optional

import numpy as np
import pandas as pd

from config import settings

BUY_FEE = settings.FEES["buy"]
SELL_FEE = settings.FEES["sell"]


# ═══════════════════════════════════════════════
# کارمزد و هزینه معامله
# ═══════════════════════════════════════════════

def net_trade_return(entry_price: float, exit_price: float,
                     buy_fee: float = BUY_FEE, sell_fee: float = SELL_FEE) -> float:
    """بازده خالص یک معامله رفت و برگشت پس از کسر کارمزد خرید و فروش."""
    if entry_price <= 0:
        raise ValueError("قیمت ورود باید مثبت باشد")
    return (exit_price * (1.0 - sell_fee)) / (entry_price * (1.0 + buy_fee)) - 1.0


def breakeven_move(buy_fee: float = BUY_FEE, sell_fee: float = SELL_FEE) -> float:
    """حداقل رشد قیمت لازم برای سر به سر شدن یک معامله (≈ ۱.۲۶٪ با کارمزدهای فعلی)."""
    return (1.0 + buy_fee) / (1.0 - sell_fee) - 1.0


# ═══════════════════════════════════════════════
# قیمت پایانی، مرجع و میانگین موزون
# ═══════════════════════════════════════════════

def official_close(df: pd.DataFrame) -> pd.Series:
    """قیمت پایانی رسمی؛ در نبود آن، قیمت آخرین معامله."""
    if "close_final_raw" in df.columns:
        return df["close_final_raw"].fillna(df.get("close_raw"))
    return df["close_raw"]


def reference_price(df: pd.DataFrame) -> pd.Series:
    """مرجعِ ارائه‌شده اگر خراب/نامعلوم باشد همان NaN می‌ماند؛ از نماد دیگر حدس زده نمی‌شود."""
    if "ref_price_raw" in df.columns:
        ref = df["ref_price_raw"]
        return ref.where(np.isfinite(ref) & (ref > 0))
    close = official_close(df)
    return close.groupby(df["symbol"]).shift(1) if "symbol" in df else close.shift(1)


def daily_vwap(df: pd.DataFrame) -> pd.Series:
    """
    میانگین موزون قیمت روز = ارزش معاملات ÷ حجم (قیمت واقع‌بینانه برای کسی که در طول روز معامله می‌کند).
    داده خراب به قیمت دیگری تبدیل نمی‌شود؛ VWAP نامعتبر یا خارج بازه = NaN.
    """
    volume = df["volume"].astype(float)
    vwap = (df["value"].astype(float) / volume).where(volume > 0)
    low, high = df["low_raw"], df["high_raw"]
    valid = np.isfinite(vwap) & np.isfinite(volume) & (volume > 0) & (volume % 1 == 0)
    valid &= np.isfinite(low) & np.isfinite(high) & (low > 0) & (high >= low)
    tolerance = 1e-6 * high.abs()
    valid &= (vwap >= low - tolerance) & (vwap <= high + tolerance)
    return vwap.where(valid).clip(lower=low, upper=high)


# ═══════════════════════════════════════════════
# صف قفل (Limit Lock)
# ═══════════════════════════════════════════════

def lock_flags(df: pd.DataFrame, min_move: Optional[float] = None) -> pd.DataFrame:
    """
    تشخیص روزهای صف قفل خرید/فروش بدون نیاز به دانستن دامنه نوسان.

    روز «قفل صف خرید» یعنی: کل معاملات روز در یک قیمت انجام شده (بیشترین = کمترین)
    و آن قیمت حداقل min_move بالاتر از قیمت مرجع است. در چنین روزی خریدار جدید در انتهای صف
    قرار می‌گیرد و عملاً سفارشش پر نمی‌شود. «قفل صف فروش» قرینه همین است.

    مبنای مقایسه «قیمت معامله‌شده» است، نه قیمت پایانی رسمی: وقتی حجم روز کمتر از حجم مبنا باشد،
    قیمت پایانی فقط بخشی از حرکت را نشان می‌دهد و صف قفلِ کم‌حجم دیده نمی‌شد (روی داده واقعی،
    بیش از نیمی از روزهای قفل — مثلاً ۱۸ هزار روز صف فروش سال ۱۴۰۰ — از قلم می‌افتاد).
    دامنه نوسان در تاریخ بازار از ±۲٪ تا ±۷٪ و گاهی نامتقارن (+۶٪/−۲٪) بوده است؛ آستانه پیش‌فرض ۰.۵٪
    از کمترین دامنه مشاهده‌شده بسیار کمتر است و فقط معامله تک‌قیمتی نزدیک قیمت مرجع را کنار می‌گذارد.
    """
    min_move = settings.BACKTEST_CONFIG["lock_min_move_pct"] if min_move is None else min_move
    ref = reference_price(df)
    single_price = (df["high_raw"] == df["low_raw"]) & (df["volume"].fillna(0) > 0)
    traded_move = df["high_raw"] / ref - 1.0
    return pd.DataFrame({
        "locked_up": (single_price & (traded_move >= min_move)).fillna(False),
        "locked_down": (single_price & (traded_move <= -min_move)).fillna(False),
    }, index=df.index)


# ═══════════════════════════════════════════════
# بازده Point-in-Time و تعدیل قیمت
# ═══════════════════════════════════════════════

def pit_daily_returns(df: pd.DataFrame) -> pd.Series:
    """بازده روزانه بدون نگاه به آینده (اثر افزایش سرمایه و سود نقدی خودبه‌خود حذف می‌شود)."""
    return official_close(df) / reference_price(df) - 1.0


def corporate_action_factors(df: pd.DataFrame, tolerance: float = 1e-6) -> pd.Series:
    """
    ضریب اقدام شرکتی هر روز = قیمت مرجع امروز ÷ قیمت پایانی روز معاملاتی قبل.
    در روزهای عادی دقیقاً ۱ است؛ در روز افزایش سرمایه یا تقسیم سود کمتر از ۱ می‌شود.
    """
    prev_close = official_close(df).shift(1)
    ref = df["ref_price_raw"] if "ref_price_raw" in df.columns else prev_close
    factor = (ref / prev_close).where((ref > 0) & (prev_close > 0), 1.0).fillna(1.0)
    return factor.where((factor - 1.0).abs() > tolerance, 1.0)


def backward_adjusted_close(df: pd.DataFrame) -> pd.Series:
    """
    قیمت پایانی تعدیل‌شده رو به عقب (برای نمودار بلندمدت).
    ⚠️ این سری با هر اقدام شرکتی جدید بازنویسی می‌شود؛ مصرف آن در فیچرهای مدل ممنوع است (قانون ۱۰).
    """
    factors = corporate_action_factors(df)
    # حاصل‌ضرب ضرایب همه روزهای «بعد» از هر روز
    future_product = factors[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    return official_close(df) * future_product


def apply_execution_bounds(price: float, low: Optional[float], high: Optional[float]) -> float:
    """قیمت اجرای سفارش هرگز نمی‌تواند خارج از بازه معاملات واقعی آن روز باشد."""
    if low is not None and not np.isnan(low) and low > 0:
        price = max(price, low)
    if high is not None and not np.isnan(high) and high > 0:
        price = min(price, high)
    return price
