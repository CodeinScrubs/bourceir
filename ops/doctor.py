# -*- coding: utf-8 -*-
"""
عیب‌یاب سامانه (Doctor)

همه چیزهایی که برای کار درست BRS لازم است را بررسی می‌کند و برای هر مشکل، راه‌حل فارسی ساده می‌دهد:
نسخه پایتون، پکیج‌ها، ساعت سیستم، اتصال به TSETMC و کدال (سایت‌های ایرانی — باید در Proxifier
مستقیم/Direct باشند)، اتصال به TypeSafe (سایت خارجی — نیاز به VPN) و سلامت دیتابیس.
"""

import importlib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Optional

import requests

from config import settings

OK, WARN, ERROR = "ok", "warn", "error"
ICONS = {OK: "✅", WARN: "⚠️", ERROR: "❌"}

IRANIAN_SITE_HINT = (
    "این سایت ایرانی است و معمولاً از پشت VPN باز نمی‌شود یا کند است. در برنامه Proxifier یک قانون (Rule) "
    "بسازید که دامنه {domain} را روی Direct بگذارد (بدون پروکسی)، سپس دوباره امتحان کنید."
)
FOREIGN_SITE_HINT = (
    "این سرویس خارجی است و از داخل ایران فقط با VPN در دسترس است. VPN را روشن کنید و مطمئن شوید "
    "دامنه {domain} در Proxifier روی Direct نیست."
)


@dataclass
class Check:
    title: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def icon(self) -> str:
        return ICONS[self.status]


@dataclass
class DoctorReport:
    checks: List[Check] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.checks.append(Check(*args, **kwargs))

    @property
    def worst(self) -> str:
        statuses = {c.status for c in self.checks}
        return ERROR if ERROR in statuses else (WARN if WARN in statuses else OK)


def _timed_get(url: str, timeout: float = 20.0):
    started = time.time()
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (BRS Doctor)"})
    return response, time.time() - started


def check_python(report: DoctorReport) -> None:
    v = sys.version_info
    if (v.major, v.minor) == (3, 11):
        report.add("نسخه پایتون", OK, f"{v.major}.{v.minor}.{v.micro}")
    else:
        report.add("نسخه پایتون", WARN, f"{v.major}.{v.minor}.{v.micro}",
                   "نسخه استاندارد پروژه پایتون 3.11 است. SETUP.bat را دوباره اجرا کنید.")


def check_packages(report: DoctorReport) -> None:
    missing = []
    for module in ("pandas", "numpy", "requests", "streamlit", "yaml", "jdatetime", "dotenv", "typesafe_sdk"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        report.add("پکیج‌های پایتون", ERROR, "نصب نشده: " + "، ".join(missing), "فایل SETUP.bat را اجرا کنید.")
    else:
        report.add("پکیج‌های پایتون", OK, "همه پکیج‌های لازم نصب هستند")


def check_tsetmc(report: DoctorReport) -> Optional[datetime]:
    """اتصال به TSETMC؛ خروجی: زمان سرور (برای بررسی ساعت سیستم)."""
    from data_collector.tsetmc import MARKET_WATCH_STOCKS

    try:
        # سرور TSETMC گاهی یک پاسخ را دیر می‌دهد؛ بهترین زمان از دو تلاش ملاک است
        response, elapsed = _timed_get(MARKET_WATCH_STOCKS, timeout=settings.REQUEST_TIMEOUT_SECONDS)
        if elapsed > 5:
            response, elapsed = min(
                (response, elapsed), _timed_get(MARKET_WATCH_STOCKS, timeout=settings.REQUEST_TIMEOUT_SECONDS),
                key=lambda item: item[1],
            )
        response.raise_for_status()
        count = len(response.json().get("marketwatch", []))
        if elapsed > 5:
            report.add("اتصال به TSETMC", WARN, f"کند: {elapsed:.1f} ثانیه ({count} نماد)",
                       "اگر این کندی همیشگی است: " + IRANIAN_SITE_HINT.format(domain="*.tsetmc.com"))
        else:
            report.add("اتصال به TSETMC", OK, f"{elapsed:.1f} ثانیه — دیده‌بان بازار ({count} ردیف) دریافت شد")
        server_date = response.headers.get("Date")
        return parsedate_to_datetime(server_date) if server_date else None
    except Exception as e:
        report.add("اتصال به TSETMC", ERROR, f"ناموفق: {type(e).__name__}",
                   IRANIAN_SITE_HINT.format(domain="*.tsetmc.com"))
        return None


def check_clock(report: DoctorReport, server_time: Optional[datetime]) -> None:
    from core.timeutils import now_tehran

    if server_time is None:
        report.add("ساعت سیستم", WARN, "به دلیل عدم اتصال به TSETMC بررسی نشد")
        return
    skew = abs((now_tehran() - server_time).total_seconds())
    if skew > 120:
        report.add("ساعت سیستم", WARN, f"اختلاف {int(skew)} ثانیه با ساعت سرور",
                   "ساعت ویندوز را تنظیم کنید (Settings → Time → Sync now). محاسبه تاخیر داده به ساعت درست وابسته است.")
    else:
        report.add("ساعت سیستم", OK, f"اختلاف با سرور: {int(skew)} ثانیه")


def check_codal(report: DoctorReport) -> None:
    from data_collector.codal import SEARCH_URL

    try:
        response, elapsed = _timed_get(f"{SEARCH_URL}?&PageNumber=1", timeout=settings.REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        letters = response.json().get("Letters", [])
        status = OK if elapsed <= 5 else WARN
        report.add("اتصال به کدال", status, f"{elapsed:.1f} ثانیه — {len(letters)} اطلاعیه",
                   "" if status == OK else IRANIAN_SITE_HINT.format(domain="*.codal.ir"))
    except Exception as e:
        report.add("اتصال به کدال", ERROR, f"ناموفق: {type(e).__name__}", IRANIAN_SITE_HINT.format(domain="*.codal.ir"))


def check_jev(report: DoctorReport) -> None:
    from ai.jev import JevClient

    jev = JevClient()
    if not jev.available:
        report.add("هوش مصنوعی Jev", WARN, "کلید API تنظیم نشده",
                   "فایل .env.example را به .env کپی و کلید TYPESAFE_API_KEY را وارد کنید.")
        return
    model = jev.ping()
    jev.close()
    if model:
        report.add("هوش مصنوعی Jev", OK, f"متصل — مدل {model}")
    else:
        report.add("هوش مصنوعی Jev", ERROR, "اتصال ناموفق (کلید نامعتبر یا عدم دسترسی)",
                   FOREIGN_SITE_HINT.format(domain="api.typesafe.ai"))


def check_database(report: DoctorReport) -> None:
    from core.timeutils import now_tehran
    from database.db import DatabaseManager
    from database.migrator import discover_migrations

    try:
        db = DatabaseManager()
    except Exception as e:
        report.add("پایگاه داده", ERROR, f"باز نشد: {e}", "فایل data/brs.db را بررسی کنید یا از پوشه data/backups بازیابی کنید.")
        return
    try:
        integrity = db.integrity_check()
        if integrity == "ok":
            report.add("سلامت فایل دیتابیس", OK, "بدون خرابی")
        else:
            report.add("سلامت فایل دیتابیس", ERROR, f"خرابی: {integrity}",
                       "برنامه را ببندید و آخرین نسخه پشتیبان از پوشه data/backups را جایگزین data/brs.db کنید.")
        latest = discover_migrations()[-1][0]
        version = db.schema_version()
        counts = db.table_counts()
        universe = len(db.get_universe_symbols())
        status = OK if version == latest else ERROR
        report.add("ساختار پایگاه داده", status, f"نسخه {version} از {latest}",
                   "" if status == OK else "برنامه را یک بار دیگر اجرا کنید تا مایگریشن‌ها اعمال شوند.")
        if universe == 0:
            report.add("جامعه سهام", WARN, "هنوز نمادی از کل بازار ثبت نشده",
                       "در داشبورد دکمه «به‌روزرسانی داده‌ها» را بزنید (یا: python brs.py update).")
        else:
            report.add("جامعه سهام", OK, f"{universe} نماد سهام بورس و فرابورس")
        last = db.conn.execute("SELECT MAX(date) FROM daily_prices;").fetchone()[0]
        if not last:
            report.add("تازگی داده روزانه", WARN, "هنوز قیمتی ثبت نشده", "به‌روزرسانی داده‌ها را اجرا کنید.")
        else:
            age = (now_tehran().date() - datetime.fromisoformat(last).date()).days
            status = OK if age <= 4 else WARN
            report.add("تازگی داده روزانه", status, f"آخرین روز: {last} ({age} روز پیش) — {counts.get('daily_prices', 0):,} رکورد",
                       "" if status == OK else "به‌روزرسانی داده‌ها را اجرا کنید.")
    finally:
        db.close()


def run_doctor(include_network: bool = True) -> DoctorReport:
    report = DoctorReport()
    check_python(report)
    check_packages(report)
    check_database(report)
    if include_network:
        server_time = check_tsetmc(report)
        check_clock(report, server_time)
        check_codal(report)
        check_jev(report)
    return report


def format_report(report: DoctorReport) -> str:
    lines = ["", "🩺 گزارش سلامت سامانه BRS", "=" * 60]
    for c in report.checks:
        lines.append(f"{c.icon} {c.title}: {c.detail}")
        if c.hint and c.status != OK:
            lines.append(f"   💡 {c.hint}")
    lines.append("=" * 60)
    summary = {OK: "همه چیز آماده است ✅", WARN: "سامانه کار می‌کند ولی چند هشدار دارد ⚠️",
               ERROR: "مشکل جدی وجود دارد؛ راه‌حل‌های بالا را انجام دهید ❌"}
    lines.append(summary[report.worst])
    return "\n".join(lines)
