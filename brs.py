# -*- coding: utf-8 -*-
"""
نقطه ورود خط فرمان سامانه BRS

کاربر عادی نیازی به این فایل ندارد (همه کارها از داشبورد انجام می‌شود). دستورات:

    python brs.py doctor                      بررسی سلامت سامانه، اینترنت/پروکسی و دیتابیس
    python brs.py update                      به‌روزرسانی همه داده‌ها (جامعه سهام، قیمت، حقیقی/حقوقی، کدال)
    python brs.py update --symbols فولاد,خودرو  فقط نمادهای مشخص
    python brs.py update --full               دریافت دوباره کل تاریخچه
    python brs.py universe                    فقط همگام‌سازی فهرست سهام بورس و فرابورس
    python brs.py live                        پایش زنده کل بازار در ساعات معاملات (هر ۳۰ ثانیه)
    python brs.py live --force --cycles 1     یک اسنپ‌شات فوری حتی خارج از ساعات بازار
    python brs.py codal --judge               دریافت اطلاعیه‌های کدال و ارزیابی با هوش مصنوعی Jev
    python brs.py backup                      نسخه پشتیبان از دیتابیس (۵ نسخه آخر نگه داشته می‌شود)
    python brs.py features                    ساخت انبار فیچر (بعد از هر update خودکار انجام می‌شود)
"""

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import settings  # noqa: E402

logger = logging.getLogger("brs")


class _TehranFormatter(logging.Formatter):
    """زمان لاگ همیشه به وقت تهران (مستقل از منطقه زمانی ویندوز یا سرور CI)."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 (نام متد کتابخانه logging)
        from datetime import datetime

        from core.timeutils import TEHRAN_TZ

        return datetime.fromtimestamp(record.created, TEHRAN_TZ).strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging(verbose: bool = False, command: str = "brs") -> None:
    fmt = _TehranFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    # حداکثر ۵ فایل ۵ مگابایتی؛ هر دستور فایل جدا دارد تا اجرای هم‌زمان (پایش زنده + به‌روزرسانی)
    # در ویندوز هنگام چرخش فایل لاگ تداخل نداشته باشد
    file_handler = RotatingFileHandler(settings.LOG_DIR / f"brs-{command}.log", maxBytes=5 * 1024 * 1024,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    root = logging.getLogger()
    root.handlers = [file_handler, console]
    root.setLevel(logging.INFO)


def _symbols(arg: str):
    return [s.strip() for s in arg.split(",") if s.strip()] if arg else None


def _progress(i: int, n: int, symbol: str) -> None:
    print(f"   [{i}/{n}] {symbol}", flush=True)


def _print_report(title: str, report: dict) -> None:
    print(f"\n📊 {title}: وضعیت {report.get('status')} | موفق {report.get('symbols_succeeded', 0)} "
          f"| ناموفق {report.get('symbols_failed', 0)}", flush=True)
    for key in ("rows_inserted", "anomalies_flagged", "new_announcements", "received", "deactivated",
                "unchanged_skipped", "market_date"):
        if key in report:
            print(f"   • {key}: {report[key]}")
    for err in (report.get("errors") or [])[:10]:
        print(f"   - {err}")


def cmd_doctor(args) -> int:
    from ops.doctor import ERROR, format_report, run_doctor

    report = run_doctor(include_network=not args.offline)
    print(format_report(report))
    if args.json:
        # گزارش ماشین‌خوان برای ارسال به پشتیبان/هوش مصنوعی (بدون کلیدها و رمزها)
        import json
        from dataclasses import asdict

        from core.timeutils import now_tehran_iso

        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "checked_at": now_tehran_iso(), "version": settings.VERSION, "python": sys.version.split()[0],
            "network_tested": not args.offline, "overall": report.worst,
            "checks": [asdict(c) for c in report.checks],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"گزارش JSON: {target}")
    return 1 if report.worst == ERROR else 0


def cmd_update(args) -> int:
    from data_collector.scheduler import DataCollector

    collector = DataCollector(symbols=_symbols(args.symbols))
    print("📥 به‌روزرسانی داده‌ها آغاز شد...", flush=True)
    reports = collector.update_all(full=args.full, progress=_progress)
    titles = {"universe": "جامعه سهام", "session_candles": "کندل امروز (دیده‌بان)", "history": "تاریخچه قیمت", "client_types": "حقیقی/حقوقی", "codal": "کدال"}
    for key, report in reports.items():
        _print_report(titles.get(key, key), report)
    if not args.symbols and not args.no_features:
        _build_features(collector.db)
    collector.db.close()
    return 0 if all(r.get("status") != "failed" for r in reports.values()) else 1


def _build_features(db) -> dict:
    from features.store import build_feature_store

    print()
    print("🧮 ساخت انبار فیچر...", flush=True)
    manifest = build_feature_store(db)
    print(f"   • نسخه {manifest['version']}: {manifest['rows']:,} ردیف، {manifest['symbols']} نماد، "
          f"تا {manifest['date_max']}", flush=True)
    return manifest


def cmd_features(args) -> int:
    from database.db import DatabaseManager

    db = DatabaseManager()
    try:
        _build_features(db)
    finally:
        db.close()
    return 0


def cmd_universe(args) -> int:
    from data_collector.scheduler import DataCollector

    collector = DataCollector()
    _print_report("جامعه سهام", collector.sync_universe())
    collector.db.close()
    return 0


def cmd_live(args) -> int:
    from core.timeutils import is_market_open, now_tehran
    from data_collector.scheduler import DataCollector

    collector = DataCollector(symbols=_symbols(args.symbols))
    if not collector.db.get_universe_symbols() and not args.symbols:
        print("⚠️ جامعه سهام هنوز ثبت نشده؛ ابتدا همگام‌سازی انجام می‌شود...", flush=True)
        collector.sync_universe()
    cycles = 0
    print(f"📡 پایش زنده آغاز شد (هر {args.interval} ثانیه). برای توقف Ctrl+C را بزنید.", flush=True)
    try:
        while True:
            started = time.monotonic()
            now = now_tehran().strftime("%H:%M:%S")
            if not args.force and not is_market_open():
                print(f"[{now}] ⏸️ بازار بسته است؛ منتظر ساعات معاملات...", flush=True)
            else:
                report = collector.collect_market_snapshot(symbols=_symbols(args.symbols))
                print(f"[{now}] ✅ ذخیره {report['symbols_succeeded']} اسنپ‌شات | بدون تغییر "
                      f"{report.get('unchanged_skipped', 0)} | رد شده {report['symbols_failed']}", flush=True)
                cycles += 1
            if args.cycles and cycles >= args.cycles:
                break
            # فاصله واقعی بین دو دریافت = interval (زمان خود درخواست از انتظار کم می‌شود)
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\n🛑 پایش زنده متوقف شد.")
    finally:
        collector.db.close()
    return 0


def cmd_codal(args) -> int:
    from data_collector.scheduler import DataCollector

    collector = DataCollector()
    _print_report("کدال", collector.collect_announcements(pages=args.pages, symbol_fa=args.symbol))
    if args.judge:
        from ai.codal_judge import judge_pending_announcements

        result = judge_pending_announcements(collector.db, limit=args.limit)
        print(f"\n🤖 ارزیابی هوش مصنوعی: {result}")
    collector.db.close()
    return 0


def cmd_backup(args) -> int:
    from database.db import DatabaseManager

    db = DatabaseManager()
    target = db.backup(keep=args.keep)
    db.close()
    print(f"💾 نسخه پشتیبان ساخته شد: {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BRS — سامانه نوسان‌گیری هوشمند بورس و فرابورس")
    parser.add_argument("-v", "--verbose", action="store_true", help="نمایش جزئیات بیشتر")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="بررسی سلامت سامانه")
    p.add_argument("--offline", action="store_true", help="بدون آزمایش اتصال اینترنت")
    p.add_argument("--json", default="", help="ذخیره گزارش ماشین‌خوان در این مسیر (مثلاً logs/doctor.json)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("update", help="به‌روزرسانی همه داده‌ها")
    p.add_argument("--symbols", default="", help="نمادها با کاما (پیش‌فرض: کل سهام بورس و فرابورس)")
    p.add_argument("--full", action="store_true", help="دریافت دوباره کل تاریخچه")
    p.add_argument("--no-features", action="store_true", help="بدون ساخت دوباره انبار فیچر در پایان")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("features", help="ساخت انبار فیچر (فیچرهای روزانه و بازار)")
    p.set_defaults(func=cmd_features)

    p = sub.add_parser("universe", help="همگام‌سازی فهرست سهام")
    p.set_defaults(func=cmd_universe)

    p = sub.add_parser("live", help="پایش زنده بازار")
    p.add_argument("--symbols", default="", help="فقط این نمادها (پیش‌فرض: کل سهام)")
    p.add_argument("--interval", type=int, default=settings.POLLING_INTERVAL_SECONDS)
    p.add_argument("--cycles", type=int, default=0, help="تعداد چرخه (۰ = بی‌پایان)")
    p.add_argument("--force", action="store_true", help="اجرا حتی خارج از ساعات بازار")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("backup", help="نسخه پشتیبان از دیتابیس")
    p.add_argument("--keep", type=int, default=5, help="تعداد نسخه‌های نگه‌داشته‌شده")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("codal", help="اطلاعیه‌های کدال")
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--symbol", default=None)
    p.add_argument("--judge", action="store_true", help="ارزیابی اطلاعیه‌ها با هوش مصنوعی Jev")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_codal)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.command)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        logger.exception("خطای غیرمنتظره")
        print(f"\n❌ خطای غیرمنتظره: {e}\nجزئیات در فایل logs/brs-{args.command}.log ثبت شد.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
