# -*- coding: utf-8 -*-
"""
یکسان‌سازی متن فارسی

TSETMC نام نمادها را با «ي» و «ك» عربی برمی‌گرداند (مثلاً «خزاميا») ولی کاربر با کیبورد فارسی
«ی» و «ک» تایپ می‌کند. بدون یکسان‌سازی، جستجوی نماد شکست می‌خورد یا نماد تکراری ثبت می‌شود.
"""

import re

_ARABIC_TO_PERSIAN = str.maketrans({
    "ي": "ی",  # ي → ی
    "ى": "ی",  # ى → ی
    "ك": "ک",  # ك → ک
    "ة": "ه",  # ة → ه
})

_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_TO_EN_DIGITS = str.maketrans(_FA_DIGITS + _AR_DIGITS, "0123456789" * 2)
_TO_FA_DIGITS = str.maketrans("0123456789", _FA_DIGITS)

_ZWNJ = "‌"
_MULTI_SPACE = re.compile(r"\s+")


def normalize_fa(text: str) -> str:
    """تبدیل حروف عربی به فارسی و یکسان‌سازی فاصله‌ها (نیم‌فاصله حفظ می‌شود)."""
    if text is None:
        return ""
    return _MULTI_SPACE.sub(" ", str(text).translate(_ARABIC_TO_PERSIAN)).strip()


def normalize_symbol(symbol: str) -> str:
    """کلید یکتای نماد: حروف فارسی، بدون نیم‌فاصله و فاصله اضافه."""
    return normalize_fa(symbol).replace(_ZWNJ, "")


def to_en_digits(text: str) -> str:
    """تبدیل ارقام فارسی/عربی به انگلیسی (برای پردازش تاریخ و عدد)."""
    return str(text).translate(_TO_EN_DIGITS) if text is not None else ""


def to_fa_digits(text) -> str:
    """تبدیل ارقام انگلیسی به فارسی (فقط برای نمایش به کاربر)."""
    return str(text).translate(_TO_FA_DIGITS) if text is not None else ""
