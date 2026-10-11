# -*- coding: utf-8 -*-
"""
تست‌های واحد سامانه BRS Data Engine
اجرا:
    pytest tests/test_provider.py -v
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime

import pytest

# ──────────────────────────────────────────────
# افزودن مسیر ریشه پروژه برای دسترسی به ماژول‌ها
# ──────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import (
    PROJECT_NAME,
    VERSION,
    DB_PATH,
    TEST_SYMBOLS,
    TIMEZONE,
    PRICE_UNIT,
    POLLING_INTERVAL_SECONDS,
    MAX_RETRIES,
    TRADING_HOURS,
    TRADING_DAYS,
)
from database.models import (
    CREATE_SYMBOLS_TABLE,
    CREATE_DAILY_PRICES_TABLE,
    CREATE_SNAPSHOTS_TABLE,
    CREATE_COLLECTION_LOG_TABLE,
)


# ═══════════════════════════════════════════════
# فیکسچر: ساخت پایگاه داده موقت برای تست
# ═══════════════════════════════════════════════

@pytest.fixture
def temp_db(tmp_path):
    """
    ساخت یک پایگاه داده SQLite موقت با تمام جداول مورد نیاز.
    این فیکسچر از tmp_path پایتست استفاده می‌کند و پس از هر تست به‌صورت خودکار پاک می‌شود.
    """
    db_file = tmp_path / "test_brs.db"
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()

    # ساخت تمام جداول
    cursor.executescript(CREATE_SYMBOLS_TABLE)
    cursor.executescript(CREATE_DAILY_PRICES_TABLE)
    cursor.executescript(CREATE_SNAPSHOTS_TABLE)
    cursor.executescript(CREATE_COLLECTION_LOG_TABLE)
    conn.commit()

    yield conn, db_file

    # بستن اتصال پس از اتمام تست
    conn.close()


# ═══════════════════════════════════════════════
# تست ۱: بررسی وجود تنظیمات مورد نیاز
# ═══════════════════════════════════════════════

def test_settings_exist():
    """بررسی اینکه تمام تنظیمات مورد نیاز در config.settings تعریف شده‌اند."""

    # ─── بررسی مقادیر اصلی ───
    assert PROJECT_NAME is not None, "نام پروژه تعریف نشده"
    assert VERSION is not None, "نسخه پروژه تعریف نشده"
    assert DB_PATH is not None, "مسیر پایگاه داده تعریف نشده"
    assert TIMEZONE is not None, "منطقه زمانی تعریف نشده"
    assert PRICE_UNIT is not None, "واحد قیمت تعریف نشده"

    # ─── بررسی مقادیر عددی ───
    assert isinstance(POLLING_INTERVAL_SECONDS, (int, float)), "فاصله پایش باید عدد باشد"
    assert POLLING_INTERVAL_SECONDS > 0, "فاصله پایش باید مثبت باشد"
    assert isinstance(MAX_RETRIES, int), "تعداد تلاش مجدد باید عدد صحیح باشد"
    assert MAX_RETRIES > 0, "تعداد تلاش مجدد باید مثبت باشد"

    # ─── بررسی نمادهای آزمایشی ───
    assert isinstance(TEST_SYMBOLS, list), "نمادهای آزمایشی باید لیست باشند"
    assert len(TEST_SYMBOLS) >= 5, "حداقل ۵ نماد آزمایشی لازم است"

    # ─── بررسی ساعات معاملاتی ───
    assert "start" in TRADING_HOURS, "ساعت شروع معاملات تعریف نشده"
    assert "end" in TRADING_HOURS, "ساعت پایان معاملات تعریف نشده"

    # ─── بررسی روزهای کاری ───
    assert isinstance(TRADING_DAYS, list), "روزهای کاری باید لیست باشند"
    assert len(TRADING_DAYS) == 5, "باید ۵ روز کاری تعریف شود (شنبه تا چهارشنبه)"

    # ─── بررسی مقادیر ثابت ───
    assert TIMEZONE == "Asia/Tehran", "منطقه زمانی باید Asia/Tehran باشد"
    assert PRICE_UNIT == "Rial", "واحد قیمت باید ریال باشد"


# ═══════════════════════════════════════════════
# تست ۲: بررسی ساخت صحیح جداول پایگاه داده
# ═══════════════════════════════════════════════

def test_db_creation(temp_db):
    """بررسی اینکه تمام جداول مورد نیاز به‌درستی در پایگاه داده ساخته شده‌اند."""
    conn, db_file = temp_db

    # لیست جداول مورد انتظار
    expected_tables = {"symbols", "daily_prices", "snapshots", "collection_log"}

    # دریافت لیست جداول موجود در دیتابیس
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    actual_tables = {row[0] for row in cursor.fetchall()}

    # بررسی وجود تمام جداول
    for table_name in expected_tables:
        assert table_name in actual_tables, f"جدول «{table_name}» در پایگاه داده یافت نشد"


# ═══════════════════════════════════════════════
# تست ۳: درج نماد و بازیابی آن
# ═══════════════════════════════════════════════

def test_db_insert_symbol(temp_db):
    """بررسی درج یک نماد جدید و بازیابی صحیح آن از پایگاه داده."""
    conn, db_file = temp_db
    cursor = conn.cursor()

    # درج یک نماد آزمایشی
    now = datetime.now().isoformat()
    cursor.execute(
        """
        INSERT INTO symbols (tse_code, symbol_fa, name_fa, market, instrument_type, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("44891482026867833", "خودرو", "ایران خودرو", "bourse", "stock", 1, now, now),
    )
    conn.commit()

    # بازیابی نماد درج‌شده
    cursor.execute("SELECT * FROM symbols WHERE symbol_fa = ?", ("خودرو",))
    row = cursor.fetchone()

    # بررسی صحت داده‌ها
    assert row is not None, "نماد درج‌شده یافت نشد"
    assert row[1] == "44891482026867833", "کد tse_code نماد مطابقت ندارد"
    assert row[2] == "خودرو", "نام فارسی نماد مطابقت ندارد"
    assert row[3] == "ایران خودرو", "نام شرکت مطابقت ندارد"
    assert row[4] == "bourse", "بازار نماد مطابقت ندارد"


# ═══════════════════════════════════════════════
# تست ۴: جلوگیری از درج تکراری قیمت روزانه
# ═══════════════════════════════════════════════

def test_db_duplicate_prevention(temp_db):
    """بررسی اینکه UNIQUE constraint از درج رکوردهای تکراری قیمت روزانه جلوگیری می‌کند."""
    conn, db_file = temp_db
    cursor = conn.cursor()

    # ابتدا یک نماد درج می‌کنیم
    now = datetime.now().isoformat()
    cursor.execute(
        "INSERT INTO symbols (tse_code, symbol_fa, created_at) VALUES (?, ?, ?)",
        ("12345", "تست", now),
    )
    conn.commit()
    symbol_id = cursor.lastrowid

    # درج اولین رکورد قیمت روزانه
    cursor.execute(
        """
        INSERT INTO daily_prices (symbol_id, date, open, high, low, close, adj_close, volume, value, source, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol_id, "2024-01-15", 1000, 1050, 980, 1030, 1025, 50000, 51500000, "pytse", now),
    )
    conn.commit()

    # تلاش برای درج رکورد تکراری — باید خطا بدهد
    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute(
            """
            INSERT INTO daily_prices (symbol_id, date, open, high, low, close, adj_close, volume, value, source, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol_id, "2024-01-15", 1100, 1150, 1080, 1130, 1125, 60000, 67800000, "pytse", now),
        )

    # بررسی اینکه فقط یک رکورد ثبت شده
    cursor.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol_id = ? AND date = ?", (symbol_id, "2024-01-15"))
    count = cursor.fetchone()[0]
    assert count == 1, f"تعداد رکوردها باید ۱ باشد اما {count} است — جلوگیری از تکرار کار نکرده"


# ═══════════════════════════════════════════════
# تست ۵: درج و بازیابی قیمت روزانه با بررسی یکپارچگی داده
# ═══════════════════════════════════════════════

def test_db_insert_daily_price(temp_db):
    """بررسی درج قیمت روزانه و صحت‌سنجی تمام فیلدهای ذخیره‌شده."""
    conn, db_file = temp_db
    cursor = conn.cursor()

    # درج نماد
    now = datetime.now().isoformat()
    cursor.execute(
        "INSERT INTO symbols (tse_code, symbol_fa, created_at) VALUES (?, ?, ?)",
        ("99999", "فولاد", now),
    )
    conn.commit()
    symbol_id = cursor.lastrowid

    # داده‌های آزمایشی قیمت
    test_data = {
        "symbol_id": symbol_id,
        "date": "2024-03-10",
        "open": 5200.0,
        "high": 5350.0,
        "low": 5100.0,
        "close": 5300.0,
        "adj_close": 5280.0,
        "volume": 120000,
        "value": 634000000.0,
        "trade_count": 850,
        "price_unit": "Rial",
        "source": "pytse",
        "captured_at": now,
    }

    # درج رکورد قیمت
    cursor.execute(
        """
        INSERT INTO daily_prices
            (symbol_id, date, open, high, low, close, adj_close, volume, value, trade_count, price_unit, source, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            test_data["symbol_id"],
            test_data["date"],
            test_data["open"],
            test_data["high"],
            test_data["low"],
            test_data["close"],
            test_data["adj_close"],
            test_data["volume"],
            test_data["value"],
            test_data["trade_count"],
            test_data["price_unit"],
            test_data["source"],
            test_data["captured_at"],
        ),
    )
    conn.commit()

    # بازیابی و بررسی صحت داده‌ها
    cursor.execute(
        "SELECT * FROM daily_prices WHERE symbol_id = ? AND date = ?",
        (symbol_id, "2024-03-10"),
    )
    row = cursor.fetchone()

    assert row is not None, "رکورد قیمت روزانه یافت نشد"

    # ستون‌ها بر اساس ترتیب CREATE TABLE:
    # id, symbol_id, date, open, high, low, close, adj_close, volume, value, trade_count, price_unit, source, captured_at
    assert row[1] == symbol_id, "شناسه نماد مطابقت ندارد"
    assert row[2] == "2024-03-10", "تاریخ مطابقت ندارد"
    assert row[3] == 5200.0, "قیمت بازگشایی مطابقت ندارد"
    assert row[4] == 5350.0, "بالاترین قیمت مطابقت ندارد"
    assert row[5] == 5100.0, "پایین‌ترین قیمت مطابقت ندارد"
    assert row[6] == 5300.0, "آخرین قیمت مطابقت ندارد"
    assert row[7] == 5280.0, "قیمت پایانی مطابقت ندارد"
    assert row[8] == 120000, "حجم معاملات مطابقت ندارد"
    assert row[9] == 634000000.0, "ارزش معاملات مطابقت ندارد"
    assert row[10] == 850, "تعداد معاملات مطابقت ندارد"
    assert row[11] == "Rial", "واحد قیمت مطابقت ندارد"
    assert row[12] == "pytse", "منبع داده مطابقت ندارد"


def test_data_validator():
    """
    تست لایه اعتبارسنجی داده‌ها (DataValidator) برای تشخیص کندل‌های معتبر و نامعتبر
    """
    from data_collector.validator import DataValidator

    # ۱. کندل سالم
    valid_candle = {
        "date": "2026-10-10",
        "open": 5000.0,
        "high": 5200.0,
        "low": 4900.0,
        "close": 5100.0,
        "volume": 100000,
        "value": 510000000.0
    }
    is_valid, errors = DataValidator.validate_daily_candle(valid_candle)
    assert is_valid is True, f"کندل سالم نباید خطا داشته باشد: {errors}"

    # ۲. کندل ناسالم (قیمت منفی و high < low)
    invalid_candle = {
        "date": "2026-10-10",
        "open": 5000.0,
        "high": 4800.0,
        "low": 4900.0,
        "close": -100.0,
        "volume": -50,
        "value": 0
    }
    is_valid, errors = DataValidator.validate_daily_candle(invalid_candle)
    assert is_valid is False, "کندل با قیمت منفی و High < Low باید نامعتبر شناخته شود"
    assert len(errors) >= 2


def test_universe_manager_filter():
    """
    تست فیلتر جامعه سهام (UniverseManager) برای تفکیک سهام عادی از صندوق‌ها و حق‌تقدم‌ها
    """
    from data_collector.universe import UniverseManager

    # سهام عادی باید تأیید شوند
    assert UniverseManager.is_common_stock("فولاد", "IRO1FOLD0001") is True
    assert UniverseManager.is_common_stock("خودرو", "IRO1IKCO0001") is True

    # حق تقدم‌ها، صندوق‌ها و اوراق بدهی باید رد شوند
    assert UniverseManager.is_common_stock("خزامیاح") is False  # حق تقدم
    assert UniverseManager.is_common_stock("اخزا101") is False  # اسناد خزانه
    assert UniverseManager.is_common_stock("اهرم") is False     # صندوق اهرمی
    assert UniverseManager.is_common_stock("دارا یکم") is False  # صندوق دولتی
