# -*- coding: utf-8 -*-
"""
آزمون‌های استحکام پایه: پاسخ ناقص TSETMC، نمادهای حذف‌شده، تعارض شناسه، دیتابیس فقط‌خواندنی،
ایندکس‌ها، پشتیبان‌گیری و اجرای کارهای پس‌زمینه
"""

import sqlite3
import time

import pytest

from data_collector.tsetmc import TsetmcProvider
from data_collector.universe import UniverseManager
from database.db import DatabaseManager
from tests.conftest import FakeSession


def _watch_row(symbol, ins, qtc=1.0, **kw):
    row = {"lva": symbol, "lvc": f"شرکت {symbol}", "insCode": ins, "insID": f"IRO1{ins}", "qtc": qtc}
    row.update(kw)
    return row


def _info(symbol, ins, y_val, flow=1):
    return {"instrumentInfo": {"insCode": ins, "lVal18AFC": symbol, "lVal30": f"شرکت {symbol}",
                               "yVal": str(y_val), "flow": flow, "cgrValCot": "N1"}}


def test_universe_fallback_when_market_watch_lacks_instrument_types():
    """عصرها TSETMC در دیده‌بان، yVal را برنمی‌گرداند؛ نوع نمادهای جدید از صفحه مشخصات خوانده می‌شود."""
    rows = [_watch_row("فولاد", "1"), _watch_row("کیا", "2"), _watch_row("جدید", "3"), _watch_row("فولاد2", "4")]
    session = FakeSession({
        "GetMarketWatch": {"marketwatch": rows},
        "GetInstrumentInfo/2": _info("کیا", "2", 313, flow=2),   # بازار SME → خارج از دامنه
        "GetInstrumentInfo/3": _info("جدید", "3", 303, flow=2),  # سهم فرابورس جدید → داخل دامنه
    })
    known = {"1": {"ins_code": "1", "instrument_type_code": 300, "flow": 1, "market": "bourse", "board_code": "N1"}}
    universe = TsetmcProvider(session=session).get_stock_universe(known=known)
    by_symbol = {u["symbol_fa"]: u for u in universe}
    assert set(by_symbol) == {"فولاد", "جدید"}
    assert by_symbol["فولاد"]["instrument_type_code"] == 300 and by_symbol["فولاد"]["market"] == "bourse"
    assert by_symbol["جدید"]["market"] == "farabourse"
    # نماد شناخته‌شده و تابلوی بلوکی هیچ درخواست اضافه‌ای نساختند
    assert not any("GetInstrumentInfo/1" in c or "GetInstrumentInfo/4" in c for c in session.calls)


def test_universe_fallback_gives_up_when_too_many_lookups_fail():
    rows = [_watch_row(f"نماد{chr(1570 + i)}", str(100 + i)) for i in range(20)]
    session = FakeSession({"GetMarketWatch": {"marketwatch": rows}})  # مشخصات هیچ نمادی در دسترس نیست
    assert TsetmcProvider(session=session).get_stock_universe() == []  # فهرست ناقص برنمی‌گرداند


def test_duplicate_names_keep_most_active_instrument():
    rows = [
        _watch_row("فولاد", "1", qtc=10.0, yVal=300, flow=1),
        _watch_row("فولاد", "9", qtc=99.0, yVal=300, flow=1),
    ]
    universe = TsetmcProvider(session=FakeSession({"GetMarketWatch": {"marketwatch": rows}})).get_stock_universe()
    assert [u["ins_code"] for u in universe] == ["9"]


class _StaticProvider:
    def __init__(self, universe):
        self.universe = universe

    def get_stock_universe(self, known=None):
        return self.universe


def _meta(i):
    return {"symbol_fa": f"سهم{chr(1570 + i)}", "ins_code": str(i), "instrument_type_code": 300}


def test_delisted_symbols_are_deactivated_not_deleted(db):
    UniverseManager(db=db, provider=_StaticProvider([_meta(i) for i in range(10)])).sync_universe()
    sym_id = db.get_symbol_id(_meta(9)["symbol_fa"])
    db.insert_daily_price(sym_id, "2026-10-10", 100, 110, 90, 105, volume=1, value=1.0)

    result = UniverseManager(db=db, provider=_StaticProvider([_meta(i) for i in range(9)])).sync_universe()
    assert result["deactivated"] == 1
    assert len(db.get_universe_symbols()) == 9
    assert db.get_symbol(_meta(9)["symbol_fa"])["is_active"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol_id = ?;", (sym_id,)).fetchone()[0] == 1


def test_suspiciously_small_universe_does_not_deactivate(db):
    UniverseManager(db=db, provider=_StaticProvider([_meta(i) for i in range(10)])).sync_universe()
    result = UniverseManager(db=db, provider=_StaticProvider([_meta(0)])).sync_universe()
    assert result["deactivated"] == 0 and len(db.get_universe_symbols()) == 10


def test_symbol_conflict_never_returns_another_symbols_id(db):
    db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1"})
    db.upsert_symbol({"symbol_fa": "ب", "ins_code": "2"})
    # نماد ۲ به نام «الف» تغییر نام داده، ولی «الف» متعلق به نماد دیگری است
    assert db.upsert_symbol({"symbol_fa": "الف", "ins_code": "2"}) is None
    assert db.get_symbol("ب")["ins_code"] == "2"


def test_read_only_connection_cannot_write(db):
    db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "1"})
    db.close()
    with DatabaseManager(db_path=db.db_path, read_only=True) as ro:
        assert ro.get_symbol_id("فولاد") is not None
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute("DELETE FROM symbols;")


def test_query_indexes_exist(db):
    indexes = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index';")}
    assert {"idx_daily_prices_date", "idx_client_types_daily_date", "ux_symbols_ins_code"} <= indexes
    plan = " ".join(str(r[3]) for r in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM daily_prices WHERE date = '2026-10-11';"))
    assert "idx_daily_prices_date" in plan


def test_backup_rotation_and_integrity(db):
    assert db.integrity_check() == "ok"
    for _ in range(3):
        db.backup(keep=2)
        time.sleep(1.1)  # نام فایل پشتیبان تا ثانیه دقیق است
    backups = list((db.db_path.parent / "backups").glob("test_brs-*.db"))
    assert len(backups) == 2
    conn = sqlite3.connect(backups[0])
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations;").fetchone()[0] >= 6
    finally:
        conn.close()


def test_background_job_lifecycle(tmp_path, monkeypatch):
    """دکمه‌های داشبورد: اجرای brs.py در پس‌زمینه، ثبت وضعیت و خواندن گزارش."""
    from ops import jobs

    monkeypatch.setenv("BRS_DB_PATH", str(tmp_path / "job.db"))
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs.settings, "LOG_DIR", tmp_path)
    assert jobs.start_job("doctor", ["doctor", "--offline"]) is True
    deadline = time.time() + 90
    while jobs.job_status("doctor")["running"] and time.time() < deadline:
        time.sleep(0.5)
    status = jobs.job_status("doctor", tail_lines=50)
    assert not status["running"]
    assert any("گزارش سلامت" in line for line in status["log_tail"])
    assert jobs.stop_job("doctor") is False  # کار تمام‌شده را نمی‌توان متوقف کرد
