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
                "unchanged_skipped", "market_date", "zero_activity_observations", "coherent_retail_pairs",
                "unknown_retail_pairs", "evidence_path"):
        if key in report:
            print(f"   • {key}: {report[key]}")
    for err in (report.get("errors") or [])[:10]:
        print(f"   - {err}")


def cmd_doctor(args) -> int:
    from ops.doctor import ERROR, format_report, run_doctor
    from ops.report_files import json_report_path, write_json_report

    target = json_report_path(args.json, forbidden_paths=[settings.DB_PATH]) if args.json else None
    report = run_doctor(include_network=not args.offline, read_only=args.read_only)
    print(format_report(report))
    if args.json:
        # گزارش ماشین‌خوان برای ارسال به پشتیبان/هوش مصنوعی (بدون کلیدها و رمزها)
        from dataclasses import asdict

        from core.timeutils import now_tehran_iso

        write_json_report(target, {
            "checked_at": now_tehran_iso(), "version": settings.VERSION, "python": sys.version.split()[0],
            "network_tested": not args.offline, "database_read_only": args.read_only, "overall": report.worst,
            "checks": [asdict(c) for c in report.checks],
        }, forbidden_paths=[settings.DB_PATH])
        print(f"گزارش JSON: {target}")
    return 1 if report.worst == ERROR else 0


def cmd_publication_review(args) -> int:
    import json

    from data_collector.daily_publications import review_archive

    print(json.dumps(review_archive(args.path), ensure_ascii=False, allow_nan=False))
    return 0


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


def cmd_broker_report(args) -> int:
    from core.timeutils import now_tehran_iso
    from database.broker import broker_report
    from database.db import DatabaseManager
    from ops.report_files import private_report_path, write_private_report

    output = private_report_path(args.report, forbidden_paths=(settings.DB_PATH,))
    with DatabaseManager(read_only=True) as db:
        db.conn.execute('BEGIN')
        try:
            report = broker_report(db)
        finally:
            db.conn.rollback()
    if report is None:
        print('گزارش سبد وارد نشده است؛ گزارش تازه ساخته نشد.')
        return 2
    report.update(report_generated_at=now_tehran_iso(), database_access='read_only_consistent_snapshot')
    write_private_report(output, report, forbidden_paths=(settings.DB_PATH,))
    print('گزارش خصوصی حساب و مقایسه فرض کارمزد، فقط از داده محلی ساخته شد؛ تعرفه تاریخی تأیید نمی‌شود.')
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
        titles = {"universe": "جامعه سهام", "session_candles": "کندل تاریخ‌دار امروز", "history": "تاریخچه قیمت", "client_types": "حقیقی/حقوقی", "codal": "کدال"}
        for key, report in reports.items():
            _print_report(titles.get(key, key), report)
        complete = all(r.get("status") in {"success", "skipped"} for r in reports.values())
        # The legacy watch is supplemental. A failed native publication stage must not
        # publish a newly dated feature manifest over an incomplete refresh.
        usable = all(r.get("status") in {'success', 'skipped'} for k, r in reports.items()
                     if k != 'codal' and (k != 'session_candles' or r.get('native_date_and_identity_required')))
        if usable and not args.symbols and not args.no_features:
            _build_features(collector.db)
        elif not usable:
            print("⚠️ داده اصلی ناقص/ناموفق بود؛ انبار فیچر تازه منتشر نشد.", flush=True)
        return 0 if complete else 1
    finally:
        collector.db.close()


def _build_features(db, *, development_only=False) -> dict:
    from features.store import build_feature_store

    print()
    print("🧮 ساخت انبار فیچر...", flush=True)
    manifest = build_feature_store(db, root=settings.DATA_DIR / 'features' / 'development' if development_only else None,
                                   before=settings.RESEARCH_CONFIG['holdout_start'] if development_only else None)
    print(f"   • نسخه {manifest['version']}: {manifest['rows']:,} ردیف، {manifest['symbols']} نماد، "
          f"تا {manifest['date_max']}", flush=True)
    return manifest


def cmd_features(args) -> int:
    from database.db import DatabaseManager

    db = DatabaseManager()
    try:
        _build_features(db, development_only=args.development_only)
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
                           holdout_strategy=args.holdout_strategy, holdout_horizon=args.holdout_horizon,
                           feature_root=settings.DATA_DIR / 'features' / 'development' if args.development_features else None)
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
                                     dip_selector=args.dip_selector, use_personal_profile=not args.no_personal_profile)
        print(f"   • آزمایش: {out['experiment_id']}")
        print(f"   • گزارش: {out['report_path']}")
    return 0


def cmd_research_flow(args) -> int:
    from database.db import DatabaseManager
    from research.flow_run import run_flow_research
    with DatabaseManager() as db:
        out = run_flow_research(db, source_id=args.source, initial_capital_rial=args.capital_rial,
                                family=args.family, replications=args.replications)
        print(f"   • آزمایش فیلترهای ثابت: {out['experiment_id']}")
        print(f"   • گزارش: {out['report_path']}")
    return 0


def cmd_research_recheck(args) -> int:
    from ops.price_recheck import main

    argv = ['--max-rows', str(args.max_rows)]
    if args.from_preflight:
        argv += ['--from-preflight', args.from_preflight]
    if args.before:
        argv += ['--before', args.before]
    if args.fetch:
        argv += ['--fetch']
    return main(argv)


def cmd_price_recovery(args) -> int:
    from ops.price_recovery import main

    argv = ['--from-preflight', args.from_preflight] if args.from_preflight else ['--apply-run', args.apply_run]
    argv += ['--max-rows', str(args.max_rows)]
    if args.offset:
        argv += ['--offset', str(args.offset)]
    if args.include_warmup:
        argv += ['--include-warmup']
    if args.fetch:
        argv += ['--fetch']
    if args.review_note is not None:
        argv += ['--review-note', args.review_note]
    return main(argv)


def cmd_research_preflight(args) -> int:
    from ops.research_preflight import main as preflight

    argv = ['--json', args.json]
    if args.before is not None:
        argv += ['--before', args.before]
    return preflight(argv)


def cmd_trader_readiness(args) -> int:
    from ops.trader_readiness import main as readiness

    return readiness(['--json', args.json])


def cmd_research_uncertainty(args) -> int:
    from research.uncertainty import main as uncertainty

    argv = ['--strategy', args.strategy, '--benchmark', args.benchmark,
            '--initial-capital-rial', str(args.initial_capital_rial),
            '--blocks', *map(str, args.blocks), '--replications', str(args.replications),
            '--seed', str(args.seed), '--output', args.output]
    if args.calendar is not None:
        argv += ['--calendar', args.calendar]
    if args.trials_attempted is not None:
        argv += ['--trials-attempted', str(args.trials_attempted)]
    if args.family_comparisons is not None:
        argv += ['--family-comparisons', str(args.family_comparisons)]
    return uncertainty(argv)


def cmd_research_probability(args) -> int:
    import json

    from database.db import DatabaseManager
    from research.probability_run import run_probability_research

    with DatabaseManager() as db:
        report = run_probability_research(db, source_id=args.source)
    print(json.dumps(dict(experiment_id=report['experiment_id'], forecast_rows=report['forecast_rows'],
                          comparisons=len(report['summary']), accepted_for_signals=False), ensure_ascii=False))
    return 0


def cmd_intraday_audit(args) -> int:
    import json

    from database.db import DatabaseManager
    from ops.intraday_audit import audit_intraday
    from ops.report_files import private_report_path, write_private_report

    target = private_report_path(args.report, forbidden_paths=[settings.DB_PATH]) if args.report else None
    with DatabaseManager(read_only=True) as db:
        report = audit_intraday(db, args.date)
    if target:
        write_private_report(target, report, forbidden_paths=[settings.DB_PATH])
    print(json.dumps({k: v for k, v in report.items() if k != 'per_symbol'}, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['rows'] or report['capture']['protocols'] else 1


def cmd_research_review(args) -> int:
    import json

    from database.db import DatabaseManager
    from ops.research_review import format_review, review_research

    with DatabaseManager(read_only=True) as db:
        report = review_research(db, args.experiment)
    if args.json:
        target = Path(args.json).resolve()
        if not target.is_relative_to((settings.DATA_DIR / 'private').resolve()):
            raise ValueError('گزارش پژوهش را در data/private/ ذخیره کنید')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(format_review(report))
    return 0  # موفقیت بازبینی فایل‌ها، تأیید سودآوری نیست.


def cmd_capital_study(args) -> int:
    import json

    from database.db import DatabaseManager
    from ops.capital_capacity import run_capacity_study

    with DatabaseManager() as db:
        result = run_capacity_study(db, args.experiment, owner_capital=args.owner_capital)
    print(json.dumps(dict(experiment_id=result['experiment_id'], folder=result['folder'],
                          cases=len(result['cases']), accepted_for_signals=False), ensure_ascii=False))
    return 0


def cmd_capital_review(args) -> int:
    import json

    from ops.capital_capacity import read_capacity_study

    result = read_capacity_study(Path(args.study), deep=True)
    print(json.dumps(dict(experiment_id=result['experiment_id'], artifacts_verified=result['artifacts_verified'],
                          cases=len(result['cases']), accepted_for_signals=False), ensure_ascii=False))
    return 0


def cmd_instrument_history(args) -> int:
    import json

    from data_collector.tsetmc import TsetmcProvider
    from database.db import DatabaseManager
    from ops.instrument_history import capture_instrument_history

    symbols = list(dict.fromkeys(_symbols(args.symbols) or []))
    with DatabaseManager(read_only=True) as db:
        instruments = [db.get_symbol(symbol) for symbol in symbols]
    if any(item is None for item in instruments):
        raise ValueError('نماد در فهرست محلی ثبت نشده است؛ حدس هویت انجام نمی‌شود')
    result = capture_instrument_history(TsetmcProvider(), instruments, args.dates.split(','))
    print(json.dumps(dict(folder=result['folder'], attempts=len(result['cases']), status_counts=result['status_counts'],
                          candidate_fields_complete=result['candidate_fields_complete'], feature_eligible=False), ensure_ascii=False))
    return 0 if all(case['assessment']['status'] == 'observed_unverified' for case in result['cases']) else 1


def cmd_instrument_history_review(args) -> int:
    import json

    from ops.instrument_history import load_instrument_history

    result = load_instrument_history(args.study)
    print(json.dumps(dict(artifacts_verified=True, attempts=len(result['cases']), status_counts=result['status_counts'],
                          feature_eligible=False, accepted_for_signals=False), ensure_ascii=False))
    return 0


def cmd_dividend_source(args) -> int:
    import json

    from data_collector.tsetmc import TsetmcProvider
    from database.db import DatabaseManager
    from ops.dividend_evidence import capture_dividend_evidence

    symbols = list(dict.fromkeys(_symbols(args.symbols) or []))
    if not 1 <= len(symbols) <= 10:
        raise ValueError('یک تا ده نماد ثبت‌شده لازم است')
    with DatabaseManager(read_only=True) as db:
        instruments = [db.get_symbol(symbol) for symbol in symbols]
    if any(item is None for item in instruments):
        raise ValueError('نماد در فهرست محلی نیست؛ هویت حدس زده نمی‌شود')
    result = capture_dividend_evidence(TsetmcProvider(), instruments)
    print(json.dumps(dict(folder=result['folder'], attempts=len(result['cases']), status_counts=result['status_counts'],
        feature_eligible=False, economic_entitlements_verified=False, settlement_verified=False), ensure_ascii=False))
    return 0 if all(case['status'] == 'observed_unverified' and case['assessment']['development_rows'] > 0 for case in result['cases']) else 1


def cmd_dividend_source_review(args) -> int:
    import json

    from ops.dividend_evidence import load_dividend_evidence

    result = load_dividend_evidence(args.study)
    print(json.dumps(dict(artifacts_verified=True, attempts=len(result['cases']), status_counts=result['status_counts'],
        feature_eligible=False, economic_entitlements_verified=False, accepted_for_signals=False), ensure_ascii=False))
    return 0


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


def cmd_codal_document(args) -> int:
    import json

    from ops.codal_documents import capture_codal_document

    result = capture_codal_document(args.url)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['status'] == 'captured' else 1


def cmd_codal_tables(args) -> int:
    import json

    from ops.codal_tables import capture_codal_tables

    result = capture_codal_tables(args.url, args.expected_tracing_no)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['status'] == 'captured' else 1


def cmd_codal_normalize(args) -> int:
    import json

    from ops.codal_tables import normalize_codal_archive
    result = normalize_codal_archive(args.archive)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['normalized']['verified'] else 2


def cmd_dip_diagnostics(args) -> int:
    import json

    from data_collector.tsetmc import TsetmcProvider
    from database.db import DatabaseManager
    from ops.dip_diagnostics import run_dip_diagnostics
    with DatabaseManager(read_only=True) as db:
        result = run_dip_diagnostics(db, args.experiment, max_trade_days=args.max_trade_days,
                                     provider=TsetmcProvider() if args.fetch else None)
    print(json.dumps(dict(report_path=result['report_path'], witnesses=result['intraday_witnesses']), ensure_ascii=False))
    return 0


def cmd_live(args) -> int:
    from core.timeutils import is_market_open, now_tehran
    from data_collector.scheduler import DataCollector
    from database.capture import CaptureRepository

    if args.interval <= 0 or args.cycles < 0:
        raise ValueError("فاصله دریافت باید مثبت و تعداد چرخه نامنفی باشد")
    selected = _symbols(args.symbols)
    quote_source = getattr(args, 'quote_source', 'market-watch')
    if quote_source != 'market-watch' and not 1 <= len(selected or []) <= 10:
        raise ValueError('برای منبع تاریخ‌دار، یک تا ده نماد صریح تعیین کنید')
    collector_options = {}
    if getattr(args, 'fast_live', False):
        from data_collector.tsetmc import TsetmcProvider
        collector_options['provider'] = TsetmcProvider(realtime=True)
    collector = DataCollector(symbols=selected, **collector_options)
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
                               provider=collector.provider.name, explicit_scope=bool(selected), quote_source=quote_source,
                               request_profile=getattr(collector.provider, 'request_profile', None))
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
                    options = dict(quote_source=quote_source) if quote_source != 'market-watch' else {}
                    report = collector.collect_market_snapshot(instruments=instruments, **options)
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


def cmd_query(args) -> int:
    import json
    from datetime import timedelta

    from core.timeutils import now_tehran
    from data_collector.tsetmc import TsetmcProvider
    from database.db import DatabaseManager
    from ops.query_screener import evaluate_query, read_query, start_query

    if args.resume and (args.spec or args.start or args.end or args.refresh_quotes):
        raise ValueError('ادامه فقط همان قرارداد محفوظ را می‌خواند؛ برای تغییر دامنه پرسش تازه بساز')
    if args.max_trade_days and not args.verify_trades:
        raise ValueError('دریافت ریزمعاملات به --verify-trades نیاز دارد')
    provider = TsetmcProvider() if args.verify_trades or args.refresh_quotes else None
    if args.resume:
        folder = Path(args.resume)
    else:
        today = now_tehran().date()
        spec = json.loads(Path(args.spec).read_text(encoding='utf-8')) if args.spec else dict(
            start=args.start or (today-timedelta(days=365)).isoformat(), end=args.end or (today-timedelta(days=1)).isoformat(),
            cap_low=args.cap_low, cap_high=args.cap_high, dip_pct=args.dip, rally_pct=args.rally, kind=args.kind)
        with DatabaseManager() as db:
            folder = start_query(db, spec, quote_provider=provider if args.refresh_quotes else None)
        print(f'قرارداد و ورودی محفوظ: {folder}', flush=True)
    if args.verify_trades:
        result, revision = evaluate_query(folder, provider=provider, max_trade_days=args.max_trade_days,
            refresh_archive=args.refresh_archive, progress=lambda r: print(json.dumps(r,ensure_ascii=False),flush=True))
    else:
        result, revision = read_query(folder)
    print(f"جامعه {result['caps']['universe_total']}؛ ارزش نامعلوم {result['caps']['caps_unknown']}؛ روز کاندید {result['candidate_days']}")
    print(f'گزارش: {revision / "report.html"}')
    print('شمار تأییدشده کران پایین روزهای موجود است؛ رتبه قطعی سالانه و سودآوری تأیید نشده است.')
    if result.get('collection_stopped_reason'):
        print('دریافت پس از سه پاسخ ناموجود متوالی متوقف شد؛ گزارش ناقص و نسخه‌های قبلی محفوظ‌اند.')
        return 2
    return 0


def cmd_archive_site(args) -> int:
    import json

    from data_collector.public_site import PublicSiteArchive

    output = Path(args.output).resolve()
    if not output.is_relative_to((settings.DATA_DIR/'private').resolve()):
        raise ValueError('آرشیو عمومی نیز در data/private/ نگه داشته می‌شود')
    crawler = PublicSiteArchive(args.url, output, interval=args.interval, max_pages=args.max_pages)
    result = crawler.crawl(progress=lambda s: print(json.dumps({k:v for k,v in s.items()
        if k not in ('sitemap_unresolved','skipped_by_robots')},ensure_ascii=False),flush=True))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if not result['stopped'] and not result['sitemap_unresolved'] else 2


def cmd_catalog_sources(args) -> int:
    from ops.source_catalog import catalog_claim_files, catalog_site
    if not args.site and not args.files:
        raise ValueError('آرشیو سایت یا فایل متن برای فهرست‌سازی لازم است')
    if args.site:
        root=Path(args.site).resolve()
        if not root.is_relative_to((settings.DATA_DIR/'private').resolve()):
            raise ValueError('آرشیو سایت باید در پوشه خصوصی باشد')
        result=catalog_site(root)
        print(f"فهرست سایت: {len(result['pages'])} نشانی موفق؛ فرمول خصوصی و سودآوری تأیید نشده است.")
    if args.files:
        root=Path(args.output).resolve()
        if not root.is_relative_to((settings.DATA_DIR/'private').resolve()):
            raise ValueError('فهرست ضمیمه باید در پوشه خصوصی باشد')
        result=catalog_claim_files(args.files,root)
        print(f"فهرست ثابت {result['source_count']} فایل و {result['indexed_lines']} خط ساخته شد؛ کد اجرا نشده است.")
    return 0


def cmd_market_review(args) -> int:
    from data_collector.tsetmc import TsetmcProvider
    from ops.market_review import collect_market_review, reanalyze_market_review
    from ops.report_files import private_report_path, write_private_report

    target = private_report_path(args.json, forbidden_paths=[settings.DB_PATH])
    archive_root = (settings.DATA_DIR / 'private' / 'market-reviews').resolve()
    if target.is_relative_to(archive_root) or (args.from_run and target.is_relative_to(Path(args.from_run).resolve())):
        raise ValueError('گزارش خروجی نمی‌تواند داخل آرشیو شواهد نوشته شود')
    from database.db import DatabaseManager

    registry = None
    if settings.DB_PATH.exists():
        with DatabaseManager(read_only=True) as db:
            fields = ('ins_code', 'symbol_fa', 'instrument_type_code', 'sector_code', 'is_active')
            registry = [{key: row.get(key) for key in fields} for row in db.get_universe_symbols(include_inactive=True)]
    result = (reanalyze_market_review(args.from_run, registry=registry) if args.from_run else
              collect_market_review(TsetmcProvider(), provider_factory=TsetmcProvider, workers=args.workers, registry=registry))
    write_private_report(target, result['report'])
    summary = result['report']['summary']
    print(f"گزارش بازار: {result['report']['status']} | کندل معتبر {summary['traded_count']} "
          f"از {summary['scope_count']} نماد | {result['folder']}")
    return 0 if result['report']['status'] == 'observed' else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BRS — سامانه نوسان‌گیری هوشمند بورس و فرابورس")
    parser.add_argument("-v", "--verbose", action="store_true", help="نمایش جزئیات بیشتر")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="بررسی سلامت سامانه")
    p.add_argument("--offline", action="store_true", help="بدون آزمایش اتصال اینترنت")
    p.add_argument("--json", default="", help="ذخیره گزارش ماشین‌خوان در این مسیر (مثلاً logs/doctor.json)")
    p.add_argument('--read-only', action='store_true', help='دیتابیس مفقود ساخته و ساختار قدیمی ارتقا داده نمی‌شود')
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("audit", help="گزارش فقط‌خواندنی کیفیت آرشیو واقعی، بدون اتصال اینترنت")
    p.add_argument("--json", default="logs/data-audit.json", help="مسیر گزارش کیفیت داده")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser('publication-review', help='بازخوانی بایت و محاسبات آرشیو دریافت روزانه؛ تأیید سود نیست')
    p.add_argument('path', type=Path, help='پوشه DAYPUB در آرشیو خصوصی دریافت روزانه')
    p.set_defaults(func=cmd_publication_review)

    p = sub.add_parser('market-review', help='بررسی توصیفی جلسه جاری با تاریخ مستقل هر سهم و شواهد محفوظ')
    p.add_argument('--workers', type=int, choices=(1, 2, 3, 4), default=3)
    p.add_argument('--from-run', default='', help='بازحساب آفلاین آرشیو اصلی؛ زمان قدیمی بازار حفظ می‌شود')
    p.add_argument('--json', default=str(settings.DATA_DIR / 'private' / 'market-review.json'))
    p.set_defaults(func=cmd_market_review)

    p = sub.add_parser('broker-import', help='ورود محلی گزارش فارابی بدون اتصال به حساب یا ثبت سفارش')
    p.add_argument('files', nargs='+', help='فایل سبد JSON و خروجی‌های رسمی XLSX')
    p.add_argument('--report', default=str(settings.DATA_DIR / 'private' / 'farabi-reconciliation.json'))
    p.add_argument('--adopt-baseline', action='store_true', help='شروع صریح مرحله جدید با سرمایه اسنپ‌شات و سقف زیان ۱۰٪')
    p.set_defaults(func=cmd_broker_import)

    p = sub.add_parser('broker-report', help='گزارش آفلاین و فقط‌خواندنی حساب و مقایسه فرض کارمزد؛ بدون اتصال به کارگزاری')
    p.add_argument('--report', default=str(settings.DATA_DIR / 'private' / 'farabi-reconciliation.json'))
    p.set_defaults(func=cmd_broker_report)

    p = sub.add_parser('broker-refresh-quotes', help='تازه‌سازی قیمت عمومی همه دارایی‌های واردشده، بدون اتصال به کارگزاری')
    p.add_argument('--report', default=str(settings.DATA_DIR / 'private' / 'farabi-reconciliation.json'))
    p.set_defaults(func=cmd_broker_quotes)

    p = sub.add_parser("update", help="به‌روزرسانی همه داده‌ها")
    p.add_argument("--symbols", default="", help="نمادها با کاما (پیش‌فرض: کل سهام بورس و فرابورس)")
    p.add_argument("--full", action="store_true", help="دریافت دوباره کل تاریخچه")
    p.add_argument("--no-features", action="store_true", help="بدون ساخت دوباره انبار فیچر در پایان")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("features", help="ساخت انبار فیچر (فیچرهای روزانه و بازار)")
    p.add_argument('--development-only', action='store_true', help='فقط پیش از holdout؛ انبار مستقل، بدون بازکردن دوره مهر و موم')
    p.set_defaults(func=cmd_features)

    p = sub.add_parser("research", help="برچسب‌های خالص و ارزیابی استراتژی‌های پایه (فاز ۱-ج)")
    p.add_argument('--development-features', action='store_true', help='استفاده از انبار مستقل توسعه')
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
    p.add_argument('--no-personal-profile', action='store_true', help='استفاده از سقف پژوهشی ثابت؛ بدون خواندن پروفایل شخصی')
    p.set_defaults(func=cmd_research_portfolio)

    p = sub.add_parser('research-flow', help='آزمون سه فیلتر ثابت با نقد مشترک و فشار اجرا؛ فقط دوره توسعه')
    p.add_argument('--family', choices=['v3', 'v4'], default='v3', help='v4: دو فرضیه چندعاملی مستقل؛ بدون تغییر تعریف v3')
    p.add_argument('--replications', type=int, default=20000, help='تعداد بازنمونه بلوکی برای v4')
    p.add_argument('--source', default=None, help='شناسه پژوهش پایه محفوظ و سازگار با داده خام')
    p.add_argument('--capital-rial', type=float, default=None, help='سرمایه فرضی به ریال؛ پیش‌فرض تنظیمات بکتست')
    p.set_defaults(func=cmd_research_flow)

    p = sub.add_parser('dip-diagnostics', help='کالبدشکافی حسابداری DIP محفوظ؛ شاهد محدود ترتیب معاملات')
    p.add_argument('--experiment', required=True, help='شناسه صریح PORTFOLIO محفوظ')
    p.add_argument('--max-trade-days', type=int, default=50)
    p.add_argument('--fetch', action='store_true', help='دریافت حداکثر ۵۰ روز ریزمعاملات پس از ثبت فهرست')
    p.set_defaults(func=cmd_dip_diagnostics)

    p = sub.add_parser('capital-study', help='بازاجرای اندازه سرمایه و تفکیک کارمزد روی تمام انتخاب‌های محفوظ FLOW v4')
    p.add_argument('--experiment', required=True, help='شناسه صریح FLOW v4؛ تعریف روش تغییر نمی‌کند')
    p.add_argument('--owner-capital', action='store_true', help='افزودن اندازه سرمایه مرحله از پروفایل محلی؛ بدون خواندن موجودی کارگزاری')
    p.set_defaults(func=cmd_capital_study)

    p = sub.add_parser('capital-review', help='بازحساب نقد و هزینه از تمام شواهد محفوظ مطالعه ظرفیت')
    p.add_argument('--study', required=True, help='مسیر خصوصی CAPACITY-...')
    p.set_defaults(func=cmd_capital_review)

    p = sub.add_parser('instrument-history', help='آرشیو مشخصات تاریخی نماد؛ بدون تغییر دیتابیس، فیچر یا پذیرش اعتبار تاریخی')
    p.add_argument('--symbols', required=True, help='نمادهای ثبت‌شده سهام عادی، جداشده با کاما؛ حداکثر ۲۰')
    p.add_argument('--dates', required=True, help='تاریخ‌های میلادی پیش از holdout، جداشده با کاما؛ حداکثر ۲۰ و مجموع ۱۰۰ درخواست')
    p.set_defaults(func=cmd_instrument_history)

    p = sub.add_parser('instrument-history-review', help='بازبینی آفلاین کد، پاسخ و حدود شاهد مشخصات تاریخی')
    p.add_argument('--study', required=True, help='مسیر خصوصی HISTORY-...')
    p.set_defaults(func=cmd_instrument_history_review)

    p = sub.add_parser('dividend-source', help='بایگانی منبع عمومی سود نقدی؛ واحد، استحقاق و وصول خودکار تأیید نمی‌شوند')
    p.add_argument('--symbols', required=True, help='یک تا ده نماد ثبت‌شده، جداشده با کاما')
    p.set_defaults(func=cmd_dividend_source)

    p = sub.add_parser('dividend-source-review', help='بازحساب آفلاین منبع سود نقدی با مرز توسعه و تمام تلاش‌ها')
    p.add_argument('--study', required=True, help='پوشه خصوصی DIVIDENDS-...')
    p.set_defaults(func=cmd_dividend_source_review)

    p = sub.add_parser('research-preflight', help='بررسی فقط‌خواندنی قرارداد داده پیش از پژوهش؛ بدون بازکردن آزمون نهایی')
    p.add_argument('--before', default=None, help='پایان انحصاری میلادی؛ حداکثر مرز دوره مهر و موم')
    p.add_argument('--json', default=str(settings.DATA_DIR / 'private' / 'research-preflight.json'))
    p.set_defaults(func=cmd_research_preflight)

    p = sub.add_parser('trader-readiness', help='گزارش آفلاین نصب و داده بازار؛ تأیید سودآوری نیست')
    p.add_argument('--json', default=str(settings.DATA_DIR / 'private' / 'trader-readiness.json'))
    p.set_defaults(func=cmd_trader_readiness)

    p = sub.add_parser('research-recheck', help='برنامه بازبینی قیمت‌های ناسازگار و مقایسه فقط‌خواندنی با منبع')
    p.add_argument('--before', default=None, help='پایان انحصاری توسعه؛ حداکثر مرز دوره مهر و موم')
    p.add_argument('--max-rows', type=int, default=5, help='حداکثر پنجاه مورد؛ پیش‌فرض پنج مورد')
    p.add_argument('--from-preflight', default=None, help='نمونه از گزارش خصوصی محفوظ؛ سلامت فعلی کل دیتابیس را بررسی نمی‌کند')
    p.add_argument('--fetch', action='store_true', help='دریافت واقعی؛ بدون این گزینه فقط برنامه محلی ثبت می‌شود')
    p.set_defaults(func=cmd_research_recheck)

    p = sub.add_parser('recover-prices', help='بازیابی گشایش/آخرین معامله گمشده از مسیر کامل؛ اعمال صریح با رسید و پشتیبان')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--from-preflight', help='گزارش خصوصی کیفیت جاری برای انتخاب نمونه')
    mode.add_argument('--apply-run', help='پوشه برنامه/شواهد محفوظ؛ هیچ دریافت شبکه‌ای ندارد')
    p.add_argument('--fetch', action='store_true', help='دریافت واقعی ریزمعاملات؛ پیش‌فرض بدون شبکه')
    p.add_argument('--max-rows', type=int, default=5, help='یک تا پنجاه مورد؛ ابتدا تازه‌ترین نقص‌های توسعه')
    p.add_argument('--offset', type=int, default=0, help='آغاز دسته در فهرست مرتب همان گزارش ثابت؛ پیش‌فرض صفر')
    p.add_argument('--include-warmup', action='store_true', help='سابقه پیش از توسعه برای ورودی فیچر نیز بررسی شود؛ فقط پیش از holdout')
    p.add_argument('--review-note', default=None, help='توضیح بررسی برای اعمال صریح؛ تصدیق انسانی/بیرونی نیست')
    p.set_defaults(func=cmd_price_recovery)

    p = sub.add_parser('research-probability', help='آزمون زمانی احتمال/بازده قواعد ثابت از ورودی محفوظ؛ بدون سیگنال')
    p.add_argument('--source', required=True, help='شناسه صریح BASELINES؛ دوره توسعه، بدون بازکردن holdout')
    p.set_defaults(func=cmd_research_probability)

    p = sub.add_parser('research-uncertainty', help='عدم قطعیت اختلاف بازده روزانه دو منحنی توسعه؛ تأیید سودآوری نیست')
    p.add_argument('--strategy', required=True, help='Parquet با date و equity_rial؛ فقط دوره توسعه')
    p.add_argument('--benchmark', required=True, help='منحنی مقایسه با همان تاریخ‌ها و سرمایه آغازین')
    p.add_argument('--initial-capital-rial', type=float, required=True)
    p.add_argument('--blocks', nargs='+', type=int, default=[5, 10, 20])
    p.add_argument('--replications', type=int, default=5000)
    p.add_argument('--seed', type=int, default=41)
    p.add_argument('--trials-attempted', type=int, default=None, help='کل روش‌های آزموده‌شده؛ پیش‌فرض نامعلوم')
    p.add_argument('--family-comparisons', type=int, default=None, help='کل مقایسه‌های خانواده ثبت‌شده، شامل بلوک‌ها/برش‌ها')
    p.add_argument('--calendar', default=None, help='آرایه JSON جلسات مورد انتظار همان برش توسعه؛ اختیاری')
    p.add_argument('--output', default=str(settings.DATA_DIR / 'private' / 'uncertainty.json'))
    p.set_defaults(func=cmd_research_uncertainty)

    p = sub.add_parser('research-review', help='بازبینی شواهد پژوهش محفوظ؛ بدون صدور سیگنال یا بازکردن holdout')
    p.add_argument('--experiment', required=True, help='شناسه صریح آزمایش PORTFOLIO یا FLOW')
    p.add_argument('--json', default='', help='مسیر خروجی اختیاری در data/private/')
    p.set_defaults(func=cmd_research_review)

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

    p = sub.add_parser('query',help='پرسش توصیفی ارزش شرکت و ترتیب منفی به مثبت؛ بدون سیگنال')
    p.add_argument('--spec',default=None,help='قرارداد JSON با شرط‌های مجاز؛ کد اجرا نمی‌شود')
    p.add_argument('--start',default=None,help='روز میلادی؛ پیش‌فرض ۳۶۵ روز پیش')
    p.add_argument('--end',default=None,help='روز میلادی؛ پیش‌فرض دیروز')
    p.add_argument('--cap-low',type=float,default=10.,help='صدک از سمت کوچک‌ترین‌ها')
    p.add_argument('--cap-high',type=float,default=40.)
    p.add_argument('--dip',type=float,default=-2.)
    p.add_argument('--rally',type=float,default=2.)
    p.add_argument('--kind',choices=['market_caps','dip_rally'],default='dip_rally')
    p.add_argument('--refresh-quotes',action='store_true',help='دریافت مظنه عمومی دارای تاریخ؛ ورودی مستقل محفوظ می‌شود')
    p.add_argument('--resume',default=None,help='پوشه QUERY برای ادامه همان قرارداد بدون تغییر ورودی')
    p.add_argument('--verify-trades',action='store_true',help='برداشت فقط‌خواندنی آرشیو ریزمعاملات عمومی')
    p.add_argument('--max-trade-days',type=int,default=0,help='سقف درخواست روز-نماد در این اجرا؛ صفر فقط آرشیو موجود')
    p.add_argument('--refresh-archive',action='store_true',help='نسخه مستقل تازه از ریزمعاملات؛ نسخه قبلی حذف نمی‌شود')
    p.set_defaults(func=cmd_query)

    p = sub.add_parser('archive-site',help='آرشیو قابل ادامه صفحات عمومی sitemap و پیوندها؛ بدون اجرای کد سایت')
    p.add_argument('--url',default='https://algosaham.ir/')
    p.add_argument('--output',default=str(settings.DATA_DIR/'private'/'algosaham-public'))
    p.add_argument('--interval',type=float,default=1.,help='حداقل فاصله درخواست‌ها به ثانیه')
    p.add_argument('--max-pages',type=int,default=4000)
    p.set_defaults(func=cmd_archive_site)

    p = sub.add_parser('catalog-sources',help='فهرست‌سازی آفلاین آرشیو سایت و ادعاهای متنی؛ بدون اجرای کد ضمیمه')
    p.add_argument('--site',default=None,help='پوشه آرشیو archive-site')
    p.add_argument('--files',nargs='*',default=[])
    p.add_argument('--output',default=str(settings.DATA_DIR/'private'/'source-catalog'))
    p.set_defaults(func=cmd_catalog_sources)

    p = sub.add_parser("live", help="پایش زنده بازار")
    p.add_argument("--symbols", default="", help="فقط این نمادها (پیش‌فرض: کل سهام)")
    p.add_argument("--interval", type=int, default=settings.POLLING_INTERVAL_SECONDS)
    p.add_argument("--cycles", type=int, default=0, help="تعداد چرخه (۰ = بی‌پایان)")
    p.add_argument("--force", action="store_true", help="اجرا حتی خارج از ساعات بازار")
    p.add_argument('--fast-live', action='store_true',
                   help='دریافت بدون retry خودکار با timeout ورودی/خروجی حداکثر۵ثانیه؛ سقف کل زمان نیست')
    p.add_argument('--quote-source', choices=('market-watch', 'dated-quote', 'dated-quote-context'), default='market-watch',
                   help='منبع پیش‌فرض دیده‌بان یا پاسخ تاریخ‌دار تک‌نماد برای حداکثر ده نماد صریح')
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

    p = sub.add_parser('codal-document', help='حفظ نسخه متن/PDF عمومی کدال؛ بدون استخراج سیگنال')
    p.add_argument('--url', required=True, help='پیوند عمومی Decision/Attachment/DownloadFile کدال')
    p.set_defaults(func=cmd_codal_document)
    p = sub.add_parser('codal-tables', help='حفظ سلول‌های JSON گزارش تولیدی ماهانه؛ اعداد هنوز تأیید مالی نشده‌اند')
    p.add_argument('--url', required=True, help='پیوند رسمی گزارش یک‌ماهه نوع ۵۸')
    p.add_argument('--expected-tracing-no', required=True, type=int, help='شماره پیگیری مستقل از فهرست اطلاعیه‌ها')
    p.set_defaults(func=cmd_codal_tables)

    p = sub.add_parser('codal-normalize', help='تطبیق سخت‌گیر واحد و ستون ماهانه در خروجی مستقل؛ بدون ارتقای هویت سند')
    p.add_argument('--archive', required=True, help='پوشه CODALTABLE محفوظ')
    p.set_defaults(func=cmd_codal_normalize)
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
