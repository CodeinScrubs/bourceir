# -*- coding: utf-8 -*-
"""
اسکریپت اجرای نشست پایش زنده بازار بورس تهران (BRS Live Market Session Runner)
==============================================================================
این اسکریپت در ساعات معاملاتی بازار (۰۹:۰۰ تا ۱۲:۳۰ به وقت تهران) فید داده‌های لحظه‌ای
نمادهای منتخب را هر ۳۰ ثانیه واکشی کرده، تاخیر سرور TSETMC، خطاهای کش، جهش‌های قیمتی
و قدرت خریدار/فروشنده حقیقی را به صورت برخط تحلیل و در دیتابیس ثبت می‌کند.

دستورات اجرا:
    python run_live_session.py
    python run_live_session.py --symbols خودرو,فولاد,شپنا --interval 15 --force
    python run_live_session.py --cycles 3 --force
"""

import sys
import os
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# تنظیم اینکودینگ کنسول ویندوز برای پشتیبانی کامل از حروف فارسی و ایموجی‌ها
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

# تنظیم مسیر پروژه در پایتون
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import settings
from data_collector.provider import TSEDataProvider
from data_collector.monitor import DataFreshnessMonitor
from database.db import DatabaseManager

# تنظیم لاگر
logger = logging.getLogger("BRS_LiveSession")


def setup_logging(log_filename: str = 'live_session.log') -> None:
    """پیکربندی سیستم لاگینگ برای نشست زنده."""
    log_path = settings.LOG_DIR / log_filename
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # خروجی فایل متنی
    fh = logging.FileHandler(str(log_path), encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    fh.setFormatter(fh_fmt)
    logger.addHandler(fh)


def is_market_open() -> bool:
    """بررسی اینکه آیا هم‌اکنون بازار بورس تهران باز و در ساعت معاملات است یا خیر."""
    now = datetime.now()
    weekday = now.weekday()
    if weekday not in settings.TRADING_DAYS:
        return False

    current_time_str = now.strftime("%H:%M")
    start_time = settings.TRADING_HOURS.get("start", "09:00")
    end_time = settings.TRADING_HOURS.get("end", "12:30")

    return start_time <= current_time_str <= end_time


def print_banner(symbols: List[str], interval: int, force: bool) -> None:
    """چاپ بنر آغاز نشست در ترمینال."""
    print("=" * 78)
    print(" 🚀 سامانه پایش زنده بازار بورس تهران — BRS Live Market Runner v0.4.1")
    print(f" 🕒 زمان محلی تهران: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" 📌 نمادهای تحت رصد ({len(symbols)}): {', '.join(symbols)}")
    print(f" ⏱️  بازه زمانی هر چرخه: {interval} ثانیه")
    print(f" ⚡ حالت اجبار (Force Run): {'فعال (نادیده‌گرفتن ساعت بازار)' if force else 'غیرفعال (فقط در ساعات بازار)'}")
    print("=" * 78)


def run_session(
    symbols: List[str],
    interval: int = 30,
    max_cycles: int = 0,
    force: bool = False,
    record_db: bool = True
) -> None:
    """حلقه اصلی اجرای نشست زنده و پایش مداوم فید بازار."""
    setup_logging()
    print_banner(symbols, interval, force)

    provider = TSEDataProvider()
    monitor = DataFreshnessMonitor(stale_threshold_seconds=settings.STALE_DATA_THRESHOLD_SECONDS)
    db = DatabaseManager() if record_db else None

    # اطمینان از ثبت نمادها در دیتابیس
    if db:
        for sym in symbols:
            db.insert_symbol(tse_code=sym, symbol_fa=sym)

    cycle_count = 0
    total_snapshots_saved = 0
    anomalies_detected = 0

    print("\n🔍 در حال برقراری ارتباط با فید زنده TSETMC...\n")

    try:
        while True:
            cycle_count += 1
            now_str = datetime.now().strftime("%H:%M:%S")

            # بررسی ساعات رسمی بازار در صورت غیرفعال بودن --force
            if not force and not is_market_open():
                print(f"[{now_str}] ⏸️ بازار بورس تهران هم‌اکنون بسته است (ساعات فعالیت: ۰۹:۰۰ تا ۱۲:۳۰ شنبه تا چهارشنبه).")
                print("   جهت اجرای آزمایشی خارج از ساعات بازار، از فلگ --force استفاده کنید.")
                time.sleep(interval)
                continue

            print("-" * 78)
            print(f"📡 چرخه #{cycle_count} | ساعت رسمی تهران: {now_str}")
            print(f"{'نماد':<8} | {'آخرین قیمت (ریال)':<18} | {'تاخیر':<8} | {'وضعیت سلامت':<16} | {'قدرت خریدار':<12}")
            print("-" * 78)

            cycle_success = 0
            for sym in symbols:
                try:
                    snapshot = provider.get_live_snapshot(sym)
                    if not snapshot:
                        print(f"{sym:<8} | {'دریافت ناموفق':<18} | {'-':<8} | {'❌ قطع ارتباط':<16} | {'-':<12}")
                        continue

                    # بررسی سلامت، تاخیر و جهش قیمت توسط مانیتور
                    health_report = monitor.inspect_snapshot(sym, snapshot)

                    # ذخیره در پایگاه داده
                    if db and snapshot.get("last_price"):
                        sym_id = db.get_symbol_id(sym)
                        if sym_id:
                            db.insert_snapshot(sym_id, snapshot)
                            total_snapshots_saved += 1

                    cycle_success += 1
                    price_str = f"{snapshot.get('last_price', 0):,}" if snapshot.get('last_price') else "نامشخص"
                    delay_str = f"{health_report['delay_seconds']}s"
                    status_str = health_report['status']
                    bp_str = f"{health_report['buyer_power']:.2f}x"

                    print(f"{sym:<8} | {price_str:<18} | {delay_str:<8} | {status_str:<16} | {bp_str:<12}")

                    # چاپ هشدارها در صورت وجود
                    if health_report["warnings"]:
                        anomalies_detected += len(health_report["warnings"])
                        for w in health_report["warnings"]:
                            print(f"   ↳ {w}")

                except Exception as exc:
                    logger.error("خطا در پردازش نماد %s: %s", sym, exc)
                    print(f"{sym:<8} | {'خطای پردازش':<18} | {'-':<8} | {'⚠️ استثناء':<16} | {'-':<12}")

            # بررسی شرط پایان چرخه
            if max_cycles > 0 and cycle_count >= max_cycles:
                print("\n🏁 به سقف تعداد چرخه‌های درخواستی رسیدیم.")
                break

            print(f"\n⏳ استراحت به مدت {interval} ثانیه تا چرخه بعدی (برای خروج Ctrl+C را فشار دهید)...")
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\n🛑 نشست زنده توسط کاربر متوقف شد.")

    finally:
        if db:
            db.close()
        print("\n" + "=" * 78)
        print("📊 خلاصه گزارش نشست زنده BRS:")
        print(f"   • مجموع چرخه‌های اجرا شده: {cycle_count}")
        print(f"   • اسنپ‌شات‌های ثبت‌شده در دیتابیس: {total_snapshots_saved}")
        print(f"   • ناهنجاری‌ها و هشدارهای ثبت‌شده: {anomalies_detected}")
        print("=" * 78)
        print("✅ عملیات نشست زنده با موفقیت خاتمه یافت.\n")


def main():
    parser = argparse.ArgumentParser(description="BRS Live Market Session Runner")
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(settings.TEST_SYMBOLS),
        help="لیست نمادها با کاما (مثال: خودرو,فولاد,شپنا)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=settings.POLLING_INTERVAL_SECONDS,
        help="فاصله زمانی بین هر بار دریافت به ثانیه (پیش‌فرض: ۳۰)"
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="تعداد چرخه‌های اجرا (۰ به معنای مداوم تا فشرده شدن Ctrl+C)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="اجرای اجباری حتی در ساعات خارج از بازار یا روزهای تعطیل"
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="عدم ذخیره‌سازی داده‌ها در دیتابیس (فقط نمایش در ترمینال)"
    )

    args = parser.parse_args()
    symbols_list = [s.strip() for s in args.symbols.split(",") if s.strip()]

    run_session(
        symbols=symbols_list,
        interval=args.interval,
        max_cycles=args.cycles,
        force=args.force,
        record_db=not args.no_db
    )


if __name__ == "__main__":
    main()
