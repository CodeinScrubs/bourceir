# -*- coding: utf-8 -*-
"""
آزمون‌های یکپارچه گردآورنده داده (DataCollector) با منبع داده ساختگیِ آزمونی و دیتابیس موقت
"""

from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from tests.conftest import FOOLAD_INS, FakeSession


class _NoCodal:
    def get_latest(self, pages=3, symbol_fa=None):
        return []


def _collector(db, routes):
    return DataCollector(db=db, provider=TsetmcProvider(session=FakeSession(routes)), codal=_NoCodal())


def test_universe_sync_and_history_collection(db, market_watch_rows, closing_price_daily):
    collector = _collector(db, {
        "GetMarketWatch": {"marketwatch": market_watch_rows},
        "GetClosingPriceDailyList": {"closingPriceDaily": closing_price_daily},
    })
    assert collector.sync_universe()["symbols_succeeded"] == 2
    assert {s["symbol_fa"] for s in db.get_universe_symbols()} == {"فولاد", "خزامیا"}

    report = collector.collect_history(symbols=["فولاد"])
    assert report["status"] == "success" and report["rows_inserted"] == len(closing_price_daily) - 1
    assert db.get_last_daily_date(db.get_symbol_id("فولاد")) == "2026-10-10"
    assert db.conn.execute("SELECT status FROM collection_log ORDER BY id DESC LIMIT 1;").fetchone()[0] == "success"


def test_out_of_scope_symbol_is_rejected(db):
    fund = {"insCode": "333", "lVal18AFC": "اهرم", "lastDate": 1}
    collector = _collector(db, {
        "GetInstrumentSearch": {"instrumentSearch": [fund]},
        "GetInstrumentInfo": {"instrumentInfo": {"insCode": "333", "lVal18AFC": "اهرم", "yVal": "305", "flow": 1}},
    })
    report = collector.collect_history(symbols=["اهرم"])
    assert report["status"] == "failed"
    assert db.get_symbol_id("اهرم") is None


def test_market_snapshot_saves_valid_and_skips_unchanged(db, market_watch_rows, client_type_all):
    routes = {
        "GetMarketWatch": {"marketwatch": market_watch_rows},
        "GetClientTypeAll": {"clientTypeAllDto": list(client_type_all.values())},
    }
    collector = _collector(db, routes)
    db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": FOOLAD_INS, "instrument_type_code": 300})

    first = collector.collect_market_snapshot()
    assert first["symbols_succeeded"] == 1
    saved = db.conn.execute("SELECT captured_at, bid_count_1, price_limit_up FROM snapshots;").fetchone()
    assert saved["captured_at"] and saved["bid_count_1"] == 31 and saved["price_limit_up"] == 4220.0

    second = collector.collect_market_snapshot()  # بدون تغییر در بازار → ذخیره تکراری نمی‌شود
    assert second["unchanged_skipped"] == 1 and second["symbols_succeeded"] == 0


def test_invalid_snapshot_is_quarantined(db, market_watch_rows):
    broken = dict(market_watch_rows[0], pdv=5000.0)  # آخرین معامله بالاتر از سقف دامنه
    collector = _collector(db, {"GetMarketWatch": {"marketwatch": [broken]}, "GetClientTypeAll": {}})
    sym_id = db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": FOOLAD_INS})
    report = collector.collect_market_snapshot()
    assert report["symbols_failed"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM snapshots;").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM data_quality_metrics WHERE symbol_id = ?;", (sym_id,)).fetchone()[0] == 1


def test_market_snapshot_failure_is_logged_not_faked(db):
    report = _collector(db, {}).collect_market_snapshot()
    assert report["status"] == "failed"
    assert db.conn.execute("SELECT COUNT(*) FROM snapshots;").fetchone()[0] == 0


def test_session_candles_from_market_watch(db, market_watch_rows, closing_price_daily):
    """کندل روز از دیده‌بان بازار؛ تاریخ از آخرین روز معاملاتی رسمی گرفته می‌شود (نه ساعت سیستم)."""
    routes = {
        "GetMarketWatch": {"marketwatch": market_watch_rows},
        "GetClosingPriceInfo": {"closingPriceInfo": {"finalLastDate": 20261011}},
        "GetClosingPriceDailyList": {"closingPriceDaily": closing_price_daily},
    }
    collector = _collector(db, routes)
    collector.sync_universe()
    collector.collect_history(symbols=["فولاد"])
    report = collector.collect_session_candles()
    if report["status"] == "skipped":  # اجرای آزمون در روز ۱۹ مهر ۱۴۰۵ قبل از ساعت ۱۳
        return
    assert report["market_date"] == "2026-10-11" and report["symbols_succeeded"] == 2
    row = db.get_daily_prices_df("فولاد").set_index("date").loc["2026-10-11"]
    assert (row["close_final_raw"], row["ref_price_raw"], row["high_raw"]) == (4180.0, 4100.0, 4220.0)
