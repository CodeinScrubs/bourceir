# -*- coding: utf-8 -*-
"""
آزمون‌های یکسان‌سازی متن فارسی و زمان/تاریخ شمسی
"""

from datetime import datetime

from core.text import normalize_fa, normalize_symbol, to_en_digits, to_fa_digits
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time, is_market_open, jalali_to_iso, to_jalali


def test_arabic_letters_are_normalized():
    assert normalize_symbol("خزاميا") == "خزامیا"
    assert normalize_fa("فولاد مباركه  اصفهان") == "فولاد مبارکه اصفهان"
    assert normalize_symbol("دارا‌یکم") == "دارایکم"


def test_digits_conversion():
    assert to_en_digits("۱۴۰۵/۰۷/۰۷") == "1405/07/07"
    assert to_en_digits("ن-۳۰") == "ن-30"
    assert to_fa_digits("1405") == "۱۴۰۵"


def test_tsetmc_number_formats():
    assert deven_to_iso(20261010) == "2026-10-10"
    assert heven_to_time(91530) == "09:15:30"
    assert heven_to_time(164657) == "16:46:57"


def test_jalali_round_trip():
    assert to_jalali("2026-10-11", fa_digits=False) == "1405/07/19"
    assert jalali_to_iso("۱۴۰۵/۰۷/۰۷ ۱۸:۵۵:۳۴") == "2026-09-29T18:55:34+03:30"
    assert jalali_to_iso("1405/07/07 18:55") == "2026-09-29T18:55:00+03:30"


def test_market_hours_use_tehran_time():
    sunday_10am = datetime(2026, 10, 11, 10, 0, tzinfo=TEHRAN_TZ)
    assert is_market_open(sunday_10am)
    assert not is_market_open(sunday_10am.replace(hour=13))
    assert not is_market_open(datetime(2026, 10, 9, 10, 0, tzinfo=TEHRAN_TZ))  # جمعه
    # ساعت ۷:۰۰ به وقت UTC همان ۱۰:۳۰ تهران است
    from datetime import timezone

    assert is_market_open(datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc))
