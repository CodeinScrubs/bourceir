# -*- coding: utf-8 -*-
"""
BRS Migration 005: Phase 1 Foundation

۱. شناسه پایدار TSETMC (ins_code) و متادیتای بازار برای نمادها + یکسان‌سازی حروف فارسی
۲. ادغام نمادهای تکراری (نسخه 0.4.1 برای هر نماد یک ردیف دوم با کد ISIN جعلی می‌ساخت)
۳. اصلاح معنای ستون‌های قیمت:
   - ستون adj_close در نسخه‌های قبلی در واقع «قیمت پایانی رسمی» خام بود (pytse نام adjClose
     را روی <CLOSE> گذاشته است) → به ستون جدید close_final_raw منتقل می‌شود
   - ستون ref_price_raw (قیمت مرجع/دیروز) اضافه می‌شود
   - adjustment_factor قبلی (نسبت قیمت پایانی به آخرین معامله) بی‌معنا بود → به ۱ بازنشانی می‌شود
     و پس از دریافت مجدد تاریخچه، از روی قیمت مرجع به‌درستی محاسبه می‌شود
۴. جداول جدید: داده‌های حقیقی/حقوقی روزانه، اطلاعیه‌های کدال و قضاوت‌های هوش مصنوعی روی آن‌ها
"""

from core.text import normalize_symbol
from database.migrator import add_column_if_missing, table_columns

CHILD_TABLES = [
    "daily_prices", "snapshots", "data_quality_metrics", "corporate_actions",
    "intraday_trades", "features_daily", "features_intraday",
]


def _merge_duplicate_symbols(conn):
    rows = conn.execute("SELECT id, tse_code, symbol_fa FROM symbols ORDER BY id;").fetchall()

    # یکسان‌سازی نام‌ها
    for sym_id, _, symbol_fa in rows:
        normalized = normalize_symbol(symbol_fa)
        if normalized != symbol_fa:
            conn.execute("UPDATE symbols SET symbol_fa = ? WHERE id = ?;", (normalized, sym_id))

    groups = {}
    for sym_id, tse_code, symbol_fa in rows:
        groups.setdefault(normalize_symbol(symbol_fa), []).append((sym_id, tse_code or ""))

    for members in groups.values():
        if len(members) < 2:
            continue
        # ردیف اصلی: ردیفی که کد ISIN واقعی (IR...) دارد، وگرنه قدیمی‌ترین ردیف
        members.sort(key=lambda m: (not m[1].startswith("IR"), m[0]))
        canonical_id = members[0][0]
        for dup_id, _ in members[1:]:
            for table in CHILD_TABLES:
                if "symbol_id" not in table_columns(conn, table):
                    continue
                # رکوردهای غیرتکراری به نماد اصلی منتقل می‌شوند؛ رکوردهای تکراری حذف می‌شوند
                conn.execute(f"UPDATE OR IGNORE {table} SET symbol_id = ? WHERE symbol_id = ?;", (canonical_id, dup_id))
                conn.execute(f"DELETE FROM {table} WHERE symbol_id = ?;", (dup_id,))
            conn.execute("DELETE FROM symbols WHERE id = ?;", (dup_id,))


def upgrade(conn):
    # ─── ۱. متادیتای نمادها ───
    for column, declaration in [
        ("ins_code", "TEXT"),                 # شناسه پایدار نماد در TSETMC (insCode)
        ("board_code", "TEXT"),               # کد تابلو (cGrValCot) مثل N1، N2، Z1
        ("instrument_type_code", "INTEGER"),  # نوع ابزار (yVal): 300 سهام بورس، 303 سهام فرابورس
        ("flow", "INTEGER"),                  # بازار: 1 بورس، 2 فرابورس، 4 پایه
        ("sector_code", "TEXT"),
        ("sector_name", "TEXT"),
        ("shares_outstanding", "REAL"),
        ("last_seen_at", "TEXT"),
    ]:
        add_column_if_missing(conn, "symbols", column, declaration)

    _merge_duplicate_symbols(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_symbols_symbol_fa ON symbols(symbol_fa);")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_symbols_ins_code ON symbols(ins_code) WHERE ins_code IS NOT NULL;")

    # ─── ۲. اصلاح معنای ستون‌های قیمت روزانه ───
    add_column_if_missing(conn, "daily_prices", "close_final_raw", "REAL")  # قیمت پایانی رسمی
    add_column_if_missing(conn, "daily_prices", "ref_price_raw", "REAL")    # قیمت مرجع (دیروز)
    conn.execute(
        """
        UPDATE daily_prices
        SET close_final_raw = adj_close, adj_close = NULL, adjustment_factor = 1.0
        WHERE source = 'pytse' AND close_final_raw IS NULL;
        """
    )
    # pytse فقط یک کلاینت برای TSETMC بود؛ منبع واقعی داده TSETMC است
    conn.execute("UPDATE OR IGNORE daily_prices SET source = 'tsetmc' WHERE source = 'pytse';")

    # ─── ۳. اسنپ‌شات کامل تابلو (دامنه نوسان و ۵ مظنه با تعداد سفارش) ───
    for column, declaration in [
        ("ref_price", "REAL"),
        ("price_limit_up", "REAL"),
        ("price_limit_down", "REAL"),
        ("last_trade_time", "TEXT"),
    ]:
        add_column_if_missing(conn, "snapshots", column, declaration)
    for level in range(1, 6):
        for column, declaration in [
            (f"bid_price_{level}", "REAL"), (f"bid_vol_{level}", "INTEGER"), (f"bid_count_{level}", "INTEGER"),
            (f"ask_price_{level}", "REAL"), (f"ask_vol_{level}", "INTEGER"), (f"ask_count_{level}", "INTEGER"),
        ]:
            add_column_if_missing(conn, "snapshots", column, declaration)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at ON snapshots(captured_at);")

    # ─── ۴. جداول جدید ───
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS client_types_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            buy_i_volume REAL, buy_n_volume REAL,
            buy_i_value REAL, buy_n_value REAL,
            buy_i_count INTEGER, buy_n_count INTEGER,
            sell_i_volume REAL, sell_n_volume REAL,
            sell_i_value REAL, sell_n_value REAL,
            sell_i_count INTEGER, sell_n_count INTEGER,
            source TEXT DEFAULT 'tsetmc',
            captured_at TEXT,
            FOREIGN KEY (symbol_id) REFERENCES symbols(id),
            UNIQUE(symbol_id, date)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracing_no INTEGER UNIQUE NOT NULL,
            symbol_id INTEGER,
            symbol_fa TEXT,
            company_name TEXT,
            title TEXT NOT NULL,
            letter_code TEXT,
            sent_at TEXT,
            published_at TEXT,
            url TEXT,
            source TEXT DEFAULT 'codal',
            captured_at TEXT,
            FOREIGN KEY (symbol_id) REFERENCES symbols(id)
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_announcements_published ON announcements(published_at);")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS announcement_judgments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            announcement_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            questions_version TEXT NOT NULL,
            category TEXT,
            category_confidence REAL,
            impact_score REAL,
            impact_confidence REAL,
            is_material REAL,
            answers_json TEXT,
            input_tokens INTEGER,
            created_at TEXT,
            FOREIGN KEY (announcement_id) REFERENCES announcements(id),
            UNIQUE(announcement_id, model, questions_version)
        );
        """
    )
