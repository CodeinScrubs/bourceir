# -*- coding: utf-8 -*-
"""
ابزارهای زمان: منطقه زمانی رسمی تهران، ساعات معاملات و تبدیل تاریخ شمسی/میلادی

قانون پروژه: همه زمان‌ها با منطقه زمانی Asia/Tehran ذخیره می‌شوند؛ تاریخ‌ها در دیتابیس میلادی
(YYYY-MM-DD) هستند و فقط هنگام نمایش به شمسی تبدیل می‌شوند.
"""

from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Optional, Union
from zoneinfo import ZoneInfo

import jdatetime

from config import settings
from core.text import to_en_digits, to_fa_digits

try:
    TEHRAN_TZ = ZoneInfo(settings.TIMEZONE)
except Exception:  # در ویندوز بدون پکیج tzdata
    # ایران از سال ۱۴۰۱ ساعت تابستانی ندارد؛ افست ثابت +۰۳:۳۰ صحیح است
    TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")


def now_tehran() -> datetime:
    """زمان کنونی با منطقه زمانی تهران."""
    return datetime.now(TEHRAN_TZ)


def now_tehran_iso() -> str:
    """زمان کنونی تهران به صورت رشته ISO (برای ثبت در دیتابیس)."""
    return now_tehran().isoformat()


def _parse_hhmm(value: str) -> dtime:
    hour, minute = (int(p) for p in value.split(":")[:2])
    return dtime(hour=hour, minute=minute)


def is_trading_weekday(day: Union[date, datetime]) -> bool:
    """آیا این روز هفته، روز کاری بورس است؟ (تعطیلات رسمی در جدول market_calendar بررسی می‌شود)"""
    return day.weekday() in settings.TRADING_DAYS


def is_market_open(at: Optional[datetime] = None) -> bool:
    """آیا بازار در این لحظه (به وقت تهران) در ساعات معاملات است؟"""
    at = at or now_tehran()
    at = at.replace(tzinfo=TEHRAN_TZ) if at.tzinfo is None else at.astimezone(TEHRAN_TZ)
    if not is_trading_weekday(at):
        return False
    start = _parse_hhmm(settings.TRADING_HOURS.get("start", "09:00"))
    end = _parse_hhmm(settings.TRADING_HOURS.get("end", "12:30"))
    return start <= at.time() < end


def deven_to_iso(deven: Union[int, str]) -> str:
    """تبدیل تاریخ عددی TSETMC (مثل 20261010) به YYYY-MM-DD."""
    s = str(int(deven))
    if len(s) != 8:
        raise ValueError("تاریخ TSETMC باید هشت رقم باشد")
    return date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()


def heven_to_time(heven: Union[int, str]) -> str:
    """تبدیل ساعت عددی TSETMC (مثل 91530 یا 164657) به HH:MM:SS."""
    s = str(int(heven)).zfill(6)
    if len(s) != 6:
        raise ValueError("ساعت TSETMC نامعتبر است")
    return dtime(int(s[:2]), int(s[2:4]), int(s[4:])).isoformat()


def to_jalali(gregorian: Union[str, date, datetime, None], with_time: bool = False, fa_digits: bool = True) -> str:
    """تبدیل تاریخ میلادی به رشته شمسی برای نمایش (مثلاً ۱۴۰۵/۰۷/۱۹)."""
    if gregorian is None or gregorian == "":
        return ""
    if isinstance(gregorian, str):
        dt = datetime.fromisoformat(gregorian)
    elif isinstance(gregorian, datetime):
        dt = gregorian
    else:
        dt = datetime(gregorian.year, gregorian.month, gregorian.day)
    if dt.tzinfo is not None:
        dt = dt.astimezone(TEHRAN_TZ)
    jdt = jdatetime.datetime.fromgregorian(datetime=dt.replace(tzinfo=None))
    text = jdt.strftime("%Y/%m/%d %H:%M" if with_time else "%Y/%m/%d")
    return to_fa_digits(text) if fa_digits else text


def jalali_to_iso(jalali_text: str) -> str:
    """
    تبدیل تاریخ/زمان شمسی (مثل «۱۴۰۵/۰۷/۰۷ ۱۸:۵۵:۳۴» از کدال) به ISO میلادی با منطقه زمانی تهران.
    """
    text = to_en_digits(jalali_text).strip()
    date_part, _, time_part = text.partition(" ")
    y, m, d = (int(p) for p in date_part.split("/"))
    clock = [int(p) for p in time_part.split(":") if p] if time_part else []
    hh, mm, ss = (clock + [0, 0, 0])[:3]
    g = jdatetime.datetime(y, m, d, hh, mm, ss).togregorian()
    return g.replace(tzinfo=TEHRAN_TZ).isoformat()
