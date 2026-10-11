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
    stamp = now_tehran_iso()[:19].replace(":", "").replace("-", "")
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
    """
    اجرای دستورهای SQL یکی‌یکی در تراکنش جاری. (executescript پیش از اجرا commit می‌کند و مایگریشن
    نیمه‌کاره را قابل برگشت نمی‌گذارد.)
    """
    statement = ""
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    leftover = [ln for ln in statement.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    if leftover:
        raise RuntimeError(f"دستور SQL ناتمام در انتهای {path.name}")


def _file_sha256(path: Path) -> str:
    # پایان خط ویندوز/لینوکس اثری ندارد
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _verify_checksums(conn: sqlite3.Connection, migrations: List[Tuple[int, str, Path]], version: int) -> None:
    """
    مایگریشنی که یک بار اجرا شده نباید ویرایش شود (دیتابیس‌های موجود تغییر را هرگز نمی‌بینند و ساختارها
    بی‌صدا از هم جدا می‌شوند). تغییر لازم = فایل مایگریشن جدید با شماره بعدی.
    """
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS migration_checksums (version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL);")
    recorded = dict(conn.execute("SELECT version, sha256 FROM migration_checksums;").fetchall())
    for number, name, path in migrations:
        if number > version:
            continue
        digest = _file_sha256(path)
        if number not in recorded:
            # دیتابیس‌های قدیمی‌تر از این قابلیت: اثر انگشت فعلی مبنا قرار می‌گیرد
            with conn:
                conn.execute("INSERT INTO migration_checksums (version, sha256) VALUES (?, ?);", (number, digest))
        elif recorded[number] != digest:
            raise RuntimeError(
                f"فایل مایگریشن اجراشده {name} ویرایش شده است. به جای ویرایش، مایگریشن جدید با شماره بعدی بسازید.")


def apply_migrations(conn: sqlite3.Connection) -> List[str]:
    """
    اعمال همه مایگریشن‌های اجرانشده به ترتیب. هر مایگریشن «همه یا هیچ» است: خودش و ثبتش در
    schema_migrations در یک تراکنش؛ اگر وسط کار خطا رخ دهد، هیچ تغییری باقی نمی‌ماند.
    خروجی: نام مایگریشن‌های اعمال‌شده.
    """
    applied_now = []
    version = current_version(conn)
    migrations = discover_migrations()
    _verify_checksums(conn, migrations, version)
    for number, name, path in migrations:
        if number <= version:
            continue
        logger.info("اعمال مایگریشن %s ...", name)
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            if path.suffix == ".sql":
                _run_sql_migration(conn, path)
            else:
                _run_python_migration(conn, path)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?);",
                (number, name, now_tehran_iso()),
            )
            conn.execute("INSERT OR REPLACE INTO migration_checksums (version, sha256) VALUES (?, ?);",
                         (number, _file_sha256(path)))
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("مایگریشن %s ناموفق بود؛ هیچ تغییری اعمال نشد.", name)
            raise
        applied_now.append(name)
    return applied_now
