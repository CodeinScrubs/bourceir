# -*- coding: utf-8 -*-
"""
اسکریپت اصلی اجرای فرآیند گردآوری داده‌های بورس (BRS Data Engine v0.1)
نقطه ورود خط فرمان جهت تست اتصال، دانلود تاریخچه قیمت‌ها، یا دریافت اسنپ‌شات لحظه‌ای بازار.

نمونه دستورات اجرا:
    python run_collector.py --mode test
    python run_collector.py --mode history
    python run_collector.py --mode snapshot --symbols خودرو,فولاد
"""

import sys
import os
import argparse
import logging
from pathlib import Path

# تنظیم اینکودینگ کنسول برای پشتیبانی از یونیکد و ایموجی در محیط ویندوز
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# اطمینان از قرار داشتن مسیر پروژه در sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import settings
from data_collector.provider import TSEDataProvider
from data_collector.scheduler import DataCollector


def setup_logging(log_filename: str = 'collector.log') -> None:
    """
    تنظیم سیستم لاگینگ برای ثبت هم‌زمان پیام‌ها در ترمینال و فایل متنی با پشتیبانی کامل از یونیکد (فارسی).
    """
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file_path = settings.LOG_DIR / log_filename

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # فرمت‌دهنده لاگ
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # لاگ به فایل با اینکودینگ UTF-8
    file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # لاگ به خروجی ترمینال
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []  # پاکسازی هندلرهای قبلی در صورت وجود
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def parse_arguments() -> argparse.Namespace:
    """
    پردازش آرگومان‌های ورودی خط فرمان همراه با توضیحات فارسی.
    """
    parser = argparse.ArgumentParser(
        description="سامانه گردآوری اطلاعات بازار بورس و فرابورس ایران (BRS Data Engine)",
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        '--mode',
        type=str,
        choices=['test', 'history', 'snapshot'],
        default='test',
        help=(
            "حالت اجرای برنامه:\n"
            "  test     : آزمایش اتصال به سرورهای بورس و دریافت اطلاعات آزمایشی\n"
            "  history  : دریافت و ذخیره‌سازی تاریخچه روزانه قیمت‌ها (OHLCV)\n"
            "  snapshot : دریافت یک دور اطلاعات لحظه‌ای و وضعیت صف‌های بازار"
        )
    )

    parser.add_argument(
        '--symbols',
        type=str,
        default=None,
        help="فهرست نمادها با جداکننده کاما (مثال: خودرو,فولاد,شپنا). در صورت عدم تعیین، از نمادهای پیش‌فرض تنظیمات استفاده می‌شود."
    )

    return parser.parse_args()


def main():
    """
    تابع اصلی اجرای گردآورنده داده‌ها.
    """
    setup_logging()
    args = parse_arguments()

    # استخراج لیست نمادها در صورت مشخص شدن
    symbols_list = None
    if args.symbols:
        symbols_list = [s.strip() for s in args.symbols.split(',') if s.strip()]

    print("=" * 65)
    print(f"🚀 سامانه {settings.PROJECT_NAME} نسخه {settings.VERSION} آغاز به کار کرد.")
    print(f"📌 حالت اجرا: {args.mode}")
    print("=" * 65)

    try:
        if args.mode == 'test':
            print("\n🔍 در حال بررسی اتصال به سرورهای بازار بورس...")
            provider = TSEDataProvider()
            test_sym = symbols_list[0] if symbols_list else settings.TEST_SYMBOLS[0]
            success = provider.test_connection(test_symbol=test_sym)

            print("-" * 65)
            if success:
                print(f"✅ نتیجه: اتصال با موفقیت برقرار شد. داده‌های نماد '{test_sym}' قابل دریافت هستند.")
            else:
                print(f"⚠️ نتیجه: عدم برقراری ارتباط با سرورهای بورس برای نماد '{test_sym}'.")
                print("💡 نکته: لطفاً دسترسی به اینترنت یا تغییرات اخیر سامانه مدیریت فناوری بورس (TSETMC) را بررسی فرمایید.")
            print("-" * 65)

        elif args.mode == 'history':
            collector = DataCollector(symbols=symbols_list)
            print(f"\n📥 آغاز دریافت تاریخچه روزانه برای نمادهای: {collector.symbols}")
            report = collector.collect_history()

            print("-" * 65)
            print("📊 گزارش دریافت تاریخچه روزانه:")
            print(f"   • وضعیت نهایی: {report['status']}")
            print(f"   • تعداد نمادهای درخواست‌شده: {report['symbols_requested']}")
            print(f"   • نمادهای موفق: {report['symbols_succeeded']}")
            print(f"   • نمادهای دارای خطا: {report['symbols_failed']}")
            print(f"   • تعداد رکوردهای جدید ذخیره‌شده: {report.get('rows_inserted', 0)}")
            if report.get('errors'):
                print("   • خطاهای رخ‌داده:")
                for err in report['errors']:
                    print(f"      - {err}")
            print("-" * 65)

        elif args.mode == 'snapshot':
            collector = DataCollector(symbols=symbols_list)
            print(f"\n📸 آغاز ثبت اسنپ‌شات لحظه‌ای برای نمادهای: {collector.symbols}")
            report = collector.collect_snapshots()

            print("-" * 65)
            print("📊 گزارش دریافت اسنپ‌شات لحظه‌ای:")
            print(f"   • وضعیت نهایی: {report['status']}")
            print(f"   • تعداد نمادهای درخواست‌شده: {report['symbols_requested']}")
            print(f"   • نمادهای موفق: {report['symbols_succeeded']}")
            print(f"   • نمادهای دارای خطا: {report['symbols_failed']}")
            if report.get('errors'):
                print("   • خطاهای رخ‌داده:")
                for err in report['errors']:
                    print(f"      - {err}")
            print("-" * 65)

        print("\n✅ پایان موفقیت‌آمیز عملیات.")

    except KeyboardInterrupt:
        print("\n\n⏹️ عملیات توسط کاربر متوقف شد (KeyboardInterrupt).")
        sys.exit(0)
    except Exception as e:
        logging.exception("خطای غیرمنتظره در اجرای برنامه: %s", e)
        print(f"\n❌ خطای غیرمنتظره: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
