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
    from core.text import normalize_symbol

    return list(dict.fromkeys(normalize_symbol(s) for s in arg.split(',') if s.strip())) if arg else None


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


def cmd_audit(args) -> int:
    from ops.data_audit import run_audit

    report = run_audit(args.json)
    print(f"بازبینی آرشیو: {report['overall']}؛ گزارش: {args.json}")
    return 1 if report["overall"] == "error" else 0


def cmd_broker_import(args) -> int:
    import json

    from data_collector.farabi import import_report
    from database.broker import BrokerRepository, broker_report
    from database.db import DatabaseManager

    output = Path(args.report).resolve()
    if not output.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('گزارش حساب خصوصی فقط در data/private ذخیره می‌شود')
    with DatabaseManager() as db:
        repo = BrokerRepository(db)
        portfolio_key = None
        for filename in args.files:
            path = Path(filename)
            key = import_report(repo, path.read_bytes(), path.name)
            row = db.conn.execute('SELECT kind FROM broker_imports WHERE id=?', (key,)).fetchone()
            if row['kind'] == 'portfolio':
                portfolio_key = key
            print(f"گزارش {row['kind']} وارد شد؛ شناسه {key[:12]}")
        if args.adopt_baseline:
            if not portfolio_key:
                raise ValueError('برای شروع مرحله جدید، فایل سبد را نیز مشخص کن')
            repo.adopt_baseline(portfolio_key)
        report = broker_report(db)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    print('گزارش تطبیق در پوشه خصوصی ذخیره شد؛ دفتر واقعی خودکار تأیید نمی‌شود.')
    return 0


def cmd_broker_quotes(args) -> int:
    import json

    from data_collector.account_quotes import refresh_account_quotes
    from data_collector.tsetmc import TsetmcProvider
    from database.broker import broker_report
    from database.db import DatabaseManager

    output = Path(args.report).resolve()
    if not output.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('گزارش حساب خصوصی فقط در data/private ذخیره می‌شود')
    with DatabaseManager() as db:
        refresh = refresh_account_quotes(db, TsetmcProvider(), progress=_progress)
        report = broker_report(db)
        report['quote_refresh'] = refresh
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    print(f"قیمت عمومی: {refresh['succeeded']} موفق، {refresh['failed']} ناموفق؛ گزارش خصوصی ذخیره شد.")
    return 0 if not refresh['failed'] else 1


def cmd_update(args) -> int:
    from data_collector.scheduler import DataCollector

    collector = DataCollector(symbols=_symbols(args.symbols))
    try:
        print("📥 به‌روزرسانی داده‌ها آغاز شد...", flush=True)
        reports = collector.update_all(full=args.full, progress=_progress)
        titles = {"universe": "جامعه سهام", "session_candles": "کندل امروز (دیده‌بان)", "history": "تاریخچه قیمت", "client_types": "حقیقی/حقوقی", "codal": "کدال"}
        for key, report in reports.items():
            _print_report(titles.get(key, key), report)
        complete = all(r.get("status") in {"success", "skipped"} for r in reports.values())
        # Validated daily history/client types are required; the session watch is supplemental.
        # Its failure remains visible in reports/exit status, never relabeled as success.
        usable = all(r.get("status") in {'success', 'skipped'} for k, r in reports.items()
                     if k not in {"codal", "session_candles"})
        if usable and not args.symbols and not args.no_features:
            _build_features(collector.db)
        elif not usable:
            print("⚠️ داده اصلی ناقص/ناموفق بود؛ انبار فیچر تازه منتشر نشد.", flush=True)
        return 0 if complete else 1
    finally:
        collector.db.close()


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


def cmd_research(args) -> int:
    from database.db import DatabaseManager
    from research.run import run_research

    db = DatabaseManager()
    try:
        print("🔬 پژوهش استراتژی‌های پایه (چند دقیقه)...", flush=True)
        out = run_research(db, unseal_holdout=args.unseal_holdout, random_trials=args.trials,
                           holdout_strategy=args.holdout_strategy, holdout_horizon=args.holdout_horizon)
        print(f"   • آزمایش ثبت شد: {out['experiment_id']}")
        print(f"   • گزارش: {out['report_path']}")
    finally:
        db.close()
    return 0


def cmd_research_dip(args) -> int:
    from database.db import DatabaseManager
    from research.dip_run import run_dip_research

    db = DatabaseManager()
    try:
        print("🔬 آزمون روش «خرید در اصلاح» روی دوره توسعه (چند دقیقه)...", flush=True)
        out = run_dip_research(db)
        print(f"   • آزمایش ثبت شد: {out['experiment_id']}")
    finally:
        db.close()
    return 0


def cmd_research_portfolio(args) -> int:
    from database.db import DatabaseManager
    from research.portfolio_run import run_portfolio_research
    with DatabaseManager() as db:
        print("🔬 پژوهش سرمایه مشترک و ارزیابی زمانی؛ فقط دوره توسعه...", flush=True)
        out = run_portfolio_research(db, source_id=args.source, initial_capital_rial=args.capital_rial,
                                     dip_selector=args.dip_selector)
        print(f"   • آزمایش: {out['experiment_id']}")
        print(f"   • گزارش: {out['report_path']}")
    return 0


def cmd_research_flow(args) -> int:
    from database.db import DatabaseManager
    from research.flow_run import run_flow_research
    with DatabaseManager() as db:
        out = run_flow_research(db, source_id=args.source, initial_capital_rial=args.capital_rial)
        print(f"   • آزمایش فیلترهای ثابت: {out['experiment_id']}")
        print(f"   • گزارش: {out['report_path']}")
    return 0


def cmd_intraday_audit(args) -> int:
    import json

    from database.db import DatabaseManager
    from ops.intraday_audit import audit_intraday
    with DatabaseManager(read_only=True) as db:
        report = audit_intraday(db, args.date)
    if args.report:
        target = Path(args.report).resolve()
        if not target.is_relative_to((settings.DATA_DIR / 'private').resolve()):
            raise ValueError('گزارش کیفیت را در data/private/ ذخیره کنید')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'per_symbol'}, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['rows'] or report['capture']['protocols'] else 1


def cmd_universe(args) -> int:
    from data_collector.scheduler import DataCollector

    collector = DataCollector()
    try:
        report = collector.sync_universe()
        _print_report("جامعه سهام", report)
    finally:
        collector.db.close()
    return 0 if report.get('status') == 'success' else 1


def cmd_stock_study(args) -> int:
    import json

    from database.db import DatabaseManager
    from ops.stock_study import build_stock_study

    spec=json.loads(Path(args.spec).read_text(encoding='utf-8-sig'))
    observation=json.loads(Path(args.observations).read_text(encoding='utf-8-sig')) if args.observations else None
    news=json.loads(Path(args.news).read_text(encoding='utf-8-sig')) if args.news else None
    with DatabaseManager() as db:
        result=build_stock_study(db,spec,market_observation=observation,news=news)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


def cmd_live(args) -> int:
    from core.timeutils import is_market_open, now_tehran
    from data_collector.scheduler import DataCollector
    from database.capture import CaptureRepository

    if args.interval <= 0 or args.cycles < 0:
        raise ValueError("فاصله دریافت باید مثبت و تعداد چرخه نامنفی باشد")
    selected = _symbols(args.symbols)
    collector = DataCollector(symbols=selected)
    cycles = 0
    returncode = 0
    run_id = None
    stop_reason = 'error'
    try:
        if not collector.db.get_universe_symbols() and not selected:
            print("⚠️ جامعه سهام هنوز ثبت نشده؛ ابتدا همگام‌سازی انجام می‌شود...", flush=True)
            report = collector.sync_universe()
            if report.get('status') != 'success' or not collector.db.get_universe_symbols():
                print('❌ جامعه سهام معتبر دریافت نشد؛ پایش آغاز نشد. اتصال را بررسی و دوباره اجرا کنید.', flush=True)
                return 1
        if selected:
            instruments = [collector._resolve(symbol) for symbol in selected]
            if any(row is None for row in instruments):
                raise ValueError('حداقل یک نماد درخواستی با شناسه معتبر داخل دامنه شناسایی نشد؛ پایش آغاز نشد')
            instruments = list({str(row['ins_code']): row for row in instruments}.values())
        else:
            instruments = collector.db.get_universe_symbols()
        calendar = collector.db.get_market_calendar()
        journal = CaptureRepository(collector.db)
        run_id = journal.start(instruments, interval=args.interval, cycles=args.cycles, forced=args.force,
                               provider=collector.provider.name, explicit_scope=bool(selected))
        print(f"📡 پایش زنده آغاز شد (هر {args.interval} ثانیه). برای توقف Ctrl+C را بزنید.", flush=True)
        print(f'شناسه ثبت مستقل پایش: {run_id}', flush=True)
        while True:
            started = time.monotonic()
            clock = now_tehran()
            now = clock.strftime("%H:%M:%S")
            cycle = cycles + 1
            if not args.force and not is_market_open(clock, calendar=calendar):
                journal.append(run_id, cycle, 'market_closed', dict(clock=clock.isoformat()))
                print(f"[{now}] ⏸️ بازار بسته است؛ منتظر ساعات معاملات...", flush=True)
            else:
                journal.append(run_id, cycle, 'poll_started')
                try:
                    report = collector.collect_market_snapshot(instruments=instruments)
                except Exception as exc:
                    journal.append(run_id, cycle, 'poll_finished', dict(status='error', error_type=type(exc).__name__,
                                   elapsed_seconds=time.monotonic() - started, coverage=None))
                    raise
                journal.append(run_id, cycle, 'poll_finished', dict(status=report['status'],
                               elapsed_seconds=time.monotonic() - started, coverage=report['coverage']))
                success = report.get('status') == 'success'
                returncode = max(returncode, 0 if success else 1)
                print(f"[{now}] {'✅' if success else '⚠️'} {report.get('status')} | ذخیره {report['symbols_succeeded']} اسنپ‌شات | بدون تغییر "
                      f"{report.get('unchanged_skipped', 0)} | ناموفق/غایب {report['symbols_failed']}", flush=True)
            cycles += 1
            if args.cycles and cycles >= args.cycles:
                stop_reason = 'cycles_completed'
                break
            # فاصله واقعی بین دو دریافت = interval (زمان خود درخواست از انتظار کم می‌شود)
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        stop_reason = 'user_interrupted'
        returncode = 130
        print("\n🛑 پایش زنده متوقف شد.")
    finally:
        try:
            if run_id:
                journal.append(run_id, 0, 'run_stopped', dict(reason=stop_reason, completed_loop_cycles=cycles))
        finally:
            collector.db.close()
    return returncode


def cmd_codal(args) -> int:
    from data_collector.scheduler import DataCollector

    collector = DataCollector()
    try:
        report = collector.collect_announcements(pages=args.pages, symbol_fa=args.symbol)
        _print_report("کدال", report)
        if report.get('pagination'):
            print(f"   • وضعیت صفحه‌ها: {report['pagination']} — پوشش کامل تاریخی تأیید نشده است")
        if args.judge:
            from ai.codal_judge import judge_pending_announcements

            result = judge_pending_announcements(collector.db, limit=args.limit)
            print(f"\n🤖 ارزیابی هوش مصنوعی: {result}")
    finally:
        collector.db.close()
    return 0 if report.get('status') == 'success' else 1


def cmd_backup(args) -> int:
    from database.db import DatabaseManager

    db = DatabaseManager()
    try:
        target = db.backup(keep=args.keep)
    finally:
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

    p = sub.add_parser("audit", help="گزارش فقط‌خواندنی کیفیت آرشیو واقعی، بدون اتصال اینترنت")
    p.add_argument("--json", default="logs/data-audit.json", help="مسیر گزارش کیفیت داده")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser('broker-import', help='ورود محلی گزارش فارابی بدون اتصال به حساب یا ثبت سفارش')
    p.add_argument('files', nargs='+', help='فایل سبد JSON و خروجی‌های رسمی XLSX')
    p.add_argument('--report', default=str(settings.DATA_DIR / 'private' / 'farabi-reconciliation.json'))
    p.add_argument('--adopt-baseline', action='store_true', help='شروع صریح مرحله جدید با سرمایه اسنپ‌شات و سقف زیان ۱۰٪')
    p.set_defaults(func=cmd_broker_import)

    p = sub.add_parser('broker-refresh-quotes', help='تازه‌سازی قیمت عمومی همه دارایی‌های واردشده، بدون اتصال به کارگزاری')
    p.add_argument('--report', default=str(settings.DATA_DIR / 'private' / 'farabi-reconciliation.json'))
    p.set_defaults(func=cmd_broker_quotes)

    p = sub.add_parser("update", help="به‌روزرسانی همه داده‌ها")
    p.add_argument("--symbols", default="", help="نمادها با کاما (پیش‌فرض: کل سهام بورس و فرابورس)")
    p.add_argument("--full", action="store_true", help="دریافت دوباره کل تاریخچه")
    p.add_argument("--no-features", action="store_true", help="بدون ساخت دوباره انبار فیچر در پایان")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("features", help="ساخت انبار فیچر (فیچرهای روزانه و بازار)")
    p.set_defaults(func=cmd_features)

    p = sub.add_parser("research", help="برچسب‌های خالص و ارزیابی استراتژی‌های پایه (فاز ۱-ج)")
    p.add_argument("--unseal-holdout", action="store_true",
                   help="ارزیابی دوره بیرون از نمونه (فقط برای مدل نهایی؛ نتیجه آن نباید در انتخاب مدل استفاده شود)")
    p.add_argument("--trials", type=int, default=None, help="تعداد تکرار انتخاب تصادفی")
    p.add_argument("--holdout-strategy", default=None, help="نام روش نهایی که پیش از باز کردن آزمون انتخاب شده")
    p.add_argument("--holdout-horizon", type=int, default=None, help="افق روش نهایی، پیش از باز کردن آزمون")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("research-dip", help="آزمون روش «خرید در اصلاح»: خرید در افت روز بعد، فروش با هدف سود")
    p.set_defaults(func=cmd_research_dip)

    p = sub.add_parser("research-portfolio", help="پژوهش سرمایه مشترک چندنماد و ارزیابی زمانی؛ فقط دوره توسعه")
    p.add_argument('--dip-selector', choices=['buyer_power', 'buy_queue'], default='buyer_power',
                   help='فرضیه ثابت انتخاب نامزد؛ صف خرید با فرضیه قدرت خریدار یکسان نیست')
    p.add_argument("--source", default=None, help="شناسه پژوهش پایه ثبت‌شده؛ پیش‌فرض آخرین اجرای موفق")
    p.add_argument('--capital-rial', type=float, default=None, help='سرمایه فرضی پژوهش به ریال؛ مستقل از گزارش حساب')
    p.set_defaults(func=cmd_research_portfolio)

    p = sub.add_parser('research-flow', help='آزمون سه فیلتر ثابت با نقد مشترک و فشار اجرا؛ فقط دوره توسعه')
    p.add_argument('--source', default=None, help='شناسه پژوهش پایه محفوظ و سازگار با داده خام')
    p.add_argument('--capital-rial', type=float, default=None, help='سرمایه فرضی به ریال؛ پیش‌فرض تنظیمات بکتست')
    p.set_defaults(func=cmd_research_flow)

    p = sub.add_parser('intraday-audit', help='بررسی محلی کیفیت ثبت داده درون‌روزی؛ بدون تحلیل سود')
    p.add_argument('--date', default=None, help='روز میلادی؛ پیش‌فرض آخرین روز ثبت اسنپ‌شات')
    p.add_argument('--report', default=None, help='فایل JSON اختیاری در data/private/')
    p.set_defaults(func=cmd_intraday_audit)

    p = sub.add_parser("universe", help="همگام‌سازی فهرست سهام")
    p.set_defaults(func=cmd_universe)

    p = sub.add_parser('stock-study',help='مطالعه توصیفی نمادها با گروه مقایسه؛ بدون سیگنال یا آموزش مدل')
    p.add_argument('--spec',required=True,help='فایل JSON شامل cases با symbol/start/end میلادی')
    p.add_argument('--observations',default=None,help='مشاهده عمومی محفوظ برای ارزش بازار فعلی؛ اختیاری')
    p.add_argument('--news',default=None,help='فهرست اطلاعیه عمومی با زمان انتشار؛ پیش‌فرض آرشیو محلی')
    p.set_defaults(func=cmd_stock_study)

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
