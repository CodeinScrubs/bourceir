"""راستی‌آزمایی ادعاهای ممیزی با دیتابیس موقت؛ تغییری در تقویم واقعی نمی‌دهد."""

import sqlite3

import pytest


def test_writable_database_rejects_orphan_market_rows(db):
    assert db.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        db.conn.execute("INSERT INTO daily_prices(symbol_id,date) VALUES(999999,'2024-01-01')")
    db.conn.rollback()


def test_external_calendar_patch_uses_columns_that_do_not_exist(db):
    with pytest.raises(sqlite3.OperationalError, match='column'):
        db.conn.execute("INSERT INTO market_calendar(date,is_open,notes) VALUES('2024-01-01',0,'test only')")
    assert not db.get_market_calendar()
