# -*- coding: utf-8 -*-
"""
BRS Migration 004: Baseline columns
ستون‌هایی که در نسخه‌های 0.4.x به‌صورت دستی (ALTER) اضافه می‌شدند، اکنون رسماً مایگریشن دارند.
روی دیتابیس قدیمی و دیتابیس تازه هر دو امن است (فقط ستون‌های غایب اضافه می‌شوند).
"""

from database.migrator import add_column_if_missing

COLUMNS = [
    ("symbols", "status", "TEXT DEFAULT 'active'"),
    ("symbols", "listed_date", "TEXT"),
    ("symbols", "delisted_date", "TEXT"),
    ("symbols", "base_volume", "INTEGER DEFAULT 1"),
    ("daily_prices", "open_raw", "REAL"),
    ("daily_prices", "high_raw", "REAL"),
    ("daily_prices", "low_raw", "REAL"),
    ("daily_prices", "close_raw", "REAL"),
    ("daily_prices", "adjustment_factor", "REAL DEFAULT 1.0"),
    ("snapshots", "base_volume", "INTEGER DEFAULT 1"),
    ("experiments", "dataset_version", "TEXT DEFAULT 'v1.0'"),
    ("experiments", "features_version", "TEXT DEFAULT 'v1.0'"),
    ("experiments", "model_version", "TEXT DEFAULT 'v1.0'"),
]

# مظنه‌های سه‌گانه اول دفتر سفارش
for level in (1, 2, 3):
    COLUMNS += [
        ("snapshots", f"bid_price_{level}", "REAL"),
        ("snapshots", f"bid_vol_{level}", "INTEGER"),
        ("snapshots", f"ask_price_{level}", "REAL"),
        ("snapshots", f"ask_vol_{level}", "INTEGER"),
    ]


def upgrade(conn):
    for table, column, declaration in COLUMNS:
        add_column_if_missing(conn, table, column, declaration)

    # پر کردن ستون‌های خام برای رکوردهای قدیمی که فقط ستون‌های legacy داشتند
    conn.execute(
        """
        UPDATE daily_prices
        SET open_raw = COALESCE(open_raw, open),
            high_raw = COALESCE(high_raw, high),
            low_raw = COALESCE(low_raw, low),
            close_raw = COALESCE(close_raw, close)
        WHERE open_raw IS NULL OR high_raw IS NULL OR low_raw IS NULL OR close_raw IS NULL;
        """
    )
