# -*- coding: utf-8 -*-
"""
آزمون‌های تنظیمات، مایگریشن‌ها و لایه پایگاه داده
"""

import sqlite3

import pandas as pd
import pytest

from config import settings
from database.db import DatabaseManager
from database.migrator import apply_migrations, discover_migrations

LEGACY_V041_SCHEMA = """
CREATE TABLE symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, tse_code TEXT UNIQUE NOT NULL, symbol_fa TEXT NOT NULL,
    name_fa TEXT, market TEXT, instrument_type TEXT DEFAULT 'stock', is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE daily_prices (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume INTEGER, value REAL, trade_count INTEGER,
    price_unit TEXT DEFAULT 'Rial', source TEXT DEFAULT 'pytse', captured_at TIMESTAMP,
    UNIQUE(symbol_id, date, source));
CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER NOT NULL, captured_at TIMESTAMP NOT NULL,
    last_price REAL, close_price REAL, provider_timestamp TIMESTAMP, source TEXT DEFAULT 'pytse',
    UNIQUE(symbol_id, captured_at, source));
CREATE TABLE collection_log (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TIMESTAMP, finished_at TIMESTAMP,
    collection_type TEXT, symbols_requested INTEGER, symbols_succeeded INTEGER, symbols_failed INTEGER,
    errors TEXT, status TEXT);
CREATE TABLE data_quality_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL, completeness_score REAL, price_anomaly_flag BOOLEAN, volume_anomaly_flag BOOLEAN,
    quality_score REAL, notes TEXT, UNIQUE(symbol_id, trade_date));
"""


def test_settings_loaded_from_yaml():
    assert settings.TIMEZONE == "Asia/Tehran"
    assert settings.PRICE_UNIT == "Rial"
    assert settings.FEES == {"buy": 0.003712, "sell": 0.0088}
    assert settings.BACKTEST_CONFIG["commission_buy"] == settings.FEES["buy"]
    assert settings.UNIVERSE["instrument_types"] == [300, 303]
    assert settings.UNIVERSE["include_base_market"] is False
    assert len(settings.TRADING_DAYS) == 5 and 4 not in settings.TRADING_DAYS  # جمعه تعطیل
    assert settings.MAX_RETRIES > 0 and settings.POLLING_INTERVAL_SECONDS > 0


def test_fresh_database_has_full_schema(db):
    latest = discover_migrations()[-1][0]
    assert db.schema_version() == latest
    tables = set(db.table_counts())
    for required in ("symbols", "daily_prices", "snapshots", "client_types_daily", "announcements",
                     "announcement_judgments", "features_daily", "experiments", "schema_migrations"):
        assert required in tables
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(daily_prices)")]
    for col in ("open_raw", "close_raw", "close_final_raw", "ref_price_raw", "adjustment_factor"):
        assert col in cols
    snap_cols = [r[1] for r in db.conn.execute("PRAGMA table_info(snapshots)")]
    assert "bid_count_5" in snap_cols and "price_limit_up" in snap_cols


def test_migrations_are_idempotent(db):
    assert apply_migrations(db.conn) == []


def test_legacy_v041_database_is_repaired(tmp_path):
    """دیتابیس واقعی نسخه 0.4.1: نماد تکراری با ISIN جعلی و adj_close که در واقع قیمت پایانی بود."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_V041_SCHEMA)
    conn.execute("INSERT INTO symbols (id, tse_code, symbol_fa) VALUES (1, 'IRO1FOLD0001', 'فولاد');")
    conn.execute("INSERT INTO symbols (id, tse_code, symbol_fa) VALUES (9, 'فولاد', 'فولاد');")  # تکراری
    conn.execute("INSERT INTO symbols (id, tse_code, symbol_fa) VALUES (3, 'IRO1XYZ', 'خزاميا');")  # ي عربی
    conn.execute("INSERT INTO daily_prices (symbol_id, date, open, high, low, close, adj_close, volume, source) "
                 "VALUES (1, '2026-10-04', 3680, 3810, 3600, 3600, 3690, 100, 'pytse');")
    conn.execute("INSERT INTO snapshots (symbol_id, captured_at, last_price) VALUES (9, '2026-10-11T09:00:00', 750);")
    conn.execute("INSERT INTO data_quality_metrics (symbol_id, trade_date, notes) VALUES (9, '2026-10-11', 'x');")
    conn.commit()
    conn.close()

    db = DatabaseManager(db_path=path)
    symbols = db.conn.execute("SELECT id, symbol_fa FROM symbols ORDER BY id;").fetchall()
    assert [tuple(r) for r in symbols] == [(1, "فولاد"), (3, "خزامیا")]
    # رکوردهای وابسته به نماد تکراری به نماد اصلی منتقل شدند
    assert db.conn.execute("SELECT symbol_id FROM snapshots;").fetchone()[0] == 1
    assert db.conn.execute("SELECT symbol_id FROM data_quality_metrics;").fetchone()[0] == 1
    row = db.conn.execute("SELECT source, close_raw, close_final_raw, adj_close, adjustment_factor, open_raw "
                          "FROM daily_prices;").fetchone()
    assert tuple(row) == ("tsetmc", 3600.0, 3690.0, None, 1.0, 3680.0)
    # یک نسخه پشتیبان قبل از مایگریشن ساخته شده است
    assert list((tmp_path / "backups").glob("legacy-*.db"))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO symbols (tse_code, symbol_fa) VALUES ('X', 'فولاد');")
    db.close()


def test_upsert_symbol_never_duplicates(db):
    first = db.upsert_symbol({"symbol_fa": "خزاميا", "ins_code": "111", "tse_code": "IRO3"})
    again = db.upsert_symbol({"symbol_fa": "خزامیا", "ins_code": "111", "name_fa": "صنایع خزامیا"})
    legacy = db.insert_symbol(tse_code="خزامیا", symbol_fa="خزامیا")
    assert first == again == legacy
    assert db.get_symbol_id("خزاميا") == first  # جستجو با ی عربی هم کار می‌کند
    assert db.conn.execute("SELECT COUNT(*) FROM symbols;").fetchone()[0] == 1
    assert db.get_symbol("خزامیا")["name_fa"] == "صنایع خزامیا"


def test_daily_upsert_updates_and_recomputes_adjustments(db, closing_price_daily):
    from data_collector.tsetmc import TsetmcProvider
    from tests.conftest import FakeSession

    provider = TsetmcProvider(session=FakeSession({"GetClosingPriceDailyList": {"closingPriceDaily": closing_price_daily}}))
    df = provider.get_symbol_history("x")
    sym_id = db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "x"})
    assert db.upsert_daily_prices_df(sym_id, df) == len(df)
    assert db.upsert_daily_prices_df(sym_id, df) == len(df)  # اجرای دوباره رکورد تکراری نمی‌سازد
    assert db.conn.execute("SELECT COUNT(*) FROM daily_prices;").fetchone()[0] == len(df)

    full = db.get_daily_prices_df("فولاد", raw_only=False).set_index("date")
    assert full.loc["2025-03-12", "adjustment_factor"] == pytest.approx(3982 / 5530)
    assert full.loc["2026-10-05", "adjustment_factor"] == 1.0
    # قیمت تعدیل‌شده قبل از اقدام شرکتی کوچک‌تر از قیمت خام است؛ آخرین روز دست‌نخورده است
    assert full.loc["2025-03-11", "adj_close"] < full.loc["2025-03-11", "close_final_raw"]
    assert full.loc["2026-10-10", "adj_close"] == full.loc["2026-10-10", "close_final_raw"]
    # ستون‌های تعدیل‌شده به‌صورت پیش‌فرض برگردانده نمی‌شوند (قانون ۱۰)
    assert "adj_close" not in db.get_daily_prices_df("فولاد").columns


def test_unique_constraint_prevents_duplicate_daily_rows(db):
    sym_id = db.upsert_symbol({"symbol_fa": "تست", "ins_code": "1"})
    db.insert_daily_price(sym_id, "2024-01-15", 1000, 1050, 980, 1030, volume=5, value=1.0)
    db.insert_daily_price(sym_id, "2024-01-15", 1100, 1150, 1080, 1130, volume=6, value=2.0)
    rows = db.conn.execute("SELECT COUNT(*), MAX(open_raw) FROM daily_prices;").fetchone()
    assert tuple(rows) == (1, 1100.0)  # دومی رکورد قبلی را به‌روز کرد، تکراری نساخت


def test_client_types_upsert(db):
    sym_id = db.upsert_symbol({"symbol_fa": "تست", "ins_code": "1"})
    df = pd.DataFrame([{"date": "2026-10-10", "buy_i_volume": 10, "sell_i_volume": 5, "buy_i_count": 2, "sell_i_count": 1}])
    assert db.upsert_client_types_df(sym_id, df) == 1
    assert db.upsert_client_types_df(sym_id, df) == 1
    out = db.get_client_types_df("تست")
    assert len(out) == 1 and out.iloc[0]["buy_i_volume"] == 10


def test_revision_log_keeps_replaced_values_only_when_they_change(db):
    """مایگریشن 007: اگر TSETMC عدد گذشته را عوض کند، مقدار قبلی می‌ماند؛ به‌روزرسانی با همان اعداد ردیفی نمی‌سازد."""
    import pandas as pd

    sym_id = db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "1"})
    row = {"date": "2026-10-11", "open_raw": 3800.0, "high_raw": 3880.0, "low_raw": 3780.0, "close_raw": 3850.0,
           "close_final_raw": 3840.0, "ref_price_raw": 3800.0, "volume": 1000, "value": 3.84e6, "trade_count": 10}
    db.upsert_daily_prices_df(sym_id, pd.DataFrame([row]), recompute=False)
    db.upsert_daily_prices_df(sym_id, pd.DataFrame([row]), recompute=False)            # همان اعداد
    count = "SELECT COUNT(*) FROM daily_prices_revisions"
    assert db.conn.execute(count).fetchone()[0] == 0
    db.upsert_daily_prices_df(sym_id, pd.DataFrame([{**row, "close_final_raw": 3850.0}]), recompute=False)
    rev = db.conn.execute("SELECT close_final_raw, revised_at FROM daily_prices_revisions").fetchone()
    assert rev["close_final_raw"] == 3840.0 and rev["revised_at"]                        # مقدار قبلی حفظ شد
    assert db.get_daily_prices_df("فولاد")["close_final_raw"].iloc[0] == 3850.0           # جدول اصلی = آخرین نسخه

    ct = pd.DataFrame({"date": ["2026-10-11"], "buy_i_volume": [5.0]})
    db.upsert_client_types_df(sym_id, ct)
    db.upsert_client_types_df(sym_id, ct)
    db.upsert_client_types_df(sym_id, ct.assign(buy_i_volume=6.0))
    assert [r[0] for r in db.conn.execute("SELECT buy_i_volume FROM client_types_daily_revisions")] == [5.0]
