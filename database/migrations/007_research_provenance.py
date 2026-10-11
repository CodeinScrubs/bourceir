"""ثبت منشأ خام و سابقه اصلاح قیمت/حقیقی‌حقوقی بدون بازنویسی نسخه‌های قبلی."""

from core.timeutils import now_tehran_iso
from database.migrator import add_column_if_missing


def _archive(conn, table, columns):
    archive = f"{table}_revisions"
    declarations = ", ".join(f'"{row[1]}" {row[2]}' for row in conn.execute(f"PRAGMA table_info({table})")
                              if row[1] in columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {archive} (revision_id INTEGER PRIMARY KEY, {declarations}, "
                 "archived_at TEXT NOT NULL, revision_origin TEXT NOT NULL)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{archive}_identity ON {archive}(symbol_id,date,revision_id)")
    names = ",".join(columns)
    # زمان دسترسی تاریخی نسخه‌های قدیمی قابل اثبات نیست؛ فقط زمان شروع آرشیو مشخص است.
    conn.execute(f"INSERT INTO {archive} ({names},archived_at,revision_origin) "
                 f"SELECT {names},?,'migration_baseline' FROM {table} "
                 f"WHERE NOT EXISTS (SELECT 1 FROM {archive})", (now_tehran_iso(),))
    values = ",".join(f"NEW.{col}" for col in columns)
    changed = " OR ".join(f"OLD.{col} IS NOT NEW.{col}" for col in columns if col != "captured_at")
    for event, condition in (("INSERT", ""), ("UPDATE", f"WHEN {changed}")):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS archive_{table}_{event.lower()} AFTER {event} ON {table} "
                     f"{condition} BEGIN INSERT INTO {archive} ({names},archived_at,revision_origin) "
                     f"VALUES ({values}, NEW.captured_at, 'ingested'); END")


def upgrade(conn):
    add_column_if_missing(conn, "daily_prices", "raw_price_provenance", "TEXT DEFAULT 'unverified'")
    add_column_if_missing(conn, "collection_log", "details_json", "TEXT")
    _archive(conn, "daily_prices", [
        "symbol_id", "date", "source", "captured_at", "price_unit", "raw_price_provenance",
        "open_raw", "high_raw", "low_raw", "close_raw", "close_final_raw", "ref_price_raw",
        "volume", "value", "trade_count",
    ])
    _archive(conn, "client_types_daily", [
        "symbol_id", "date", "source", "captured_at",
        "buy_i_volume", "buy_n_volume", "buy_i_value", "buy_n_value", "buy_i_count", "buy_n_count",
        "sell_i_volume", "sell_n_volume", "sell_i_value", "sell_n_value", "sell_i_count", "sell_n_count",
    ])
