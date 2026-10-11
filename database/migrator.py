# -*- coding: utf-8 -*-
"""
اجراکننده مایگریشن‌های پایگاه داده (Migration Runner)

- مایگریشن‌ها در پوشه database/migrations با پیشوند عددی (001_، 002_، ...) قرار دارند.
- فایل‌های .sql مستقیماً اجرا می‌شوند؛ فایل‌های .py باید تابع upgrade(conn) داشته باشند.
- نسخه‌های اعمال‌شده در جدول schema_migrations ثبت می‌شوند و هر مایگریشن فقط یک بار اجرا می‌شود.
- همه مایگریشن‌ها باید «تکرارپذیر» (idempotent) نوشته شوند تا روی دیتابیس‌های قدیمی هم امن باشند.
"""

import hashlib
import importlib.util
import logging
import re
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import uuid4

from core.timeutils import now_tehran_iso

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_VERSION_RE = re.compile(r"^(\d{3})_[\w\-]+\.(sql|py)$")


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """فهرست نام ستون‌های یک جدول."""
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()]


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> bool:
    """افزودن ستون فقط در صورت عدم وجود (SQLite دستور ADD COLUMN IF NOT EXISTS ندارد)."""
    if column in table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration};")
    return True


def discover_migrations() -> List[Tuple[int, str, Path]]:
    """یافتن و مرتب‌سازی فایل‌های مایگریشن بر اساس شماره نسخه."""
    found = []
    for path in MIGRATIONS_DIR.iterdir():
        match = _VERSION_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path.stem, path))
    found.sort(key=lambda item: item[0])
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"شماره نسخه تکراری در پوشه مایگریشن‌ها: {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    """آخرین نسخه مایگریشن اعمال‌شده (۰ یعنی هیچ)."""
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
    ).fetchone()[0]
    if not exists:
        # فقط وقتی جدول وجود ندارد دستور نوشتنی اجرا می‌شود (بدون گرفتن قفل نوشتن در حالت عادی)
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);"
            )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations;").fetchone()
    return int(row[0] or 0)


def pending_migrations(conn: sqlite3.Connection) -> List[str]:
    """نام مایگریشن‌هایی که هنوز اعمال نشده‌اند."""
    version = current_version(conn)
    return [name for number, name, _ in discover_migrations() if number > version]


def backup_database(conn: sqlite3.Connection, db_path: Path) -> Optional[Path]:
    """تهیه نسخه پشتیبان کامل از دیتابیس (با API رسمی backup در SQLite) در data/backups."""
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_tehran_iso().replace(":", "").replace("-", "").replace("+", "_") + "-" + uuid4().hex[:8]
    target = backup_dir / f"{db_path.stem}-{stamp}-v{current_version(conn)}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()  # «with sqlite3.connect» اتصال را نمی‌بندد؛ فایل باز می‌ماند
    logger.info("نسخه پشتیبان دیتابیس ساخته شد: %s", target)
    return target


def _run_python_migration(conn: sqlite3.Connection, path: Path) -> None:
    spec = importlib.util.spec_from_file_location(f"brs_migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "upgrade"):
        raise RuntimeError(f"مایگریشن {path.name} تابع upgrade(conn) ندارد")
    module.upgrade(conn)


def _run_sql_migration(conn: sqlite3.Connection, path: Path) -> None:
    # executescript تراکنش باز را commit می‌کند؛ هر دستور باید در همان تراکنش اجرا شود.
    pending = ""
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip() and any(not line.strip().startswith("--") for line in pending.splitlines() if line.strip()):
        raise RuntimeError(f"دستور SQL ناتمام در {path.name}")


def apply_migrations(conn: sqlite3.Connection) -> List[str]:
    """اعمال همه مایگریشن‌های اجرانشده به ترتیب. خروجی: نام مایگریشن‌های اعمال‌شده."""
    applied_now = []
    current_version(conn)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS migration_checksums (version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL)")
    recorded = {row[0]: row[1] for row in conn.execute("SELECT version,name FROM schema_migrations")}
    for number, name, path in discover_migrations():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checksum = conn.execute("SELECT sha256 FROM migration_checksums WHERE version=?", (number,)).fetchone()
        if checksum and checksum[0] != digest:
            raise RuntimeError(f"فایل مایگریشن اعمال‌شده تغییر کرده است: {path.name}")
        if number in recorded:
            if recorded[number] != name:
                raise RuntimeError(f"نام مایگریشن با دیتابیس سازگار نیست: {path.name}")
            # نسخه‌های قبل checksum نداشتند؛ مبنای محلی ثبت می‌شود، نه اثبات تاریخچه قدیمی.
            with conn:
                conn.execute("INSERT OR IGNORE INTO migration_checksums VALUES (?,?)", (number, digest))
            continue
        logger.info("اعمال مایگریشن %s ...", name)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if path.suffix == ".sql":
                _run_sql_migration(conn, path)
            else:
                _run_python_migration(conn, path)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?);",
                (number, name, now_tehran_iso()),
            )
            conn.execute("INSERT INTO migration_checksums VALUES (?,?)", (number, digest))
        applied_now.append(name)
    return applied_now
