"""آزمون خطاهایی که بازبینی مستقل پایه پیدا کرد؛ همگی آفلاین و با دیتابیس موقت."""

import copy
import json
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from config import settings
from core.timeutils import TEHRAN_TZ, deven_to_iso, heven_to_time, is_market_open
from core.validation import DataValidator
from data_collector.monitor import DataFreshnessMonitor
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from data_collector.universe import UniverseManager
from database import migrator
from features.daily import compute_daily_features
from features.store import build_feature_store, load_feature_store
from ops.doctor import ERROR, DoctorReport, check_tsetmc
from tests.conftest import FakeSession
from tests.test_features import synthetic_history


@pytest.mark.parametrize("timestamp", [None, "", "broken", "2026-10-12T09:30:00+03:30"])
def test_missing_invalid_or_future_source_time_is_not_fresh(timestamp):
    now = datetime(2026, 10, 11, 9, 30, tzinfo=TEHRAN_TZ)
    report = DataFreshnessMonitor().inspect_snapshot("فولاد", {"last_price": 100, "provider_timestamp": timestamp}, now)
    assert report["status"] == "⚪ Unknown" and not report["is_healthy"]
    assert report["buyer_power"] is None
    assert report["provider_time"] == timestamp


def test_time_only_watch_never_fabricates_a_session_date(market_watch_rows):
    snap = TsetmcProvider.snapshot_from_market_watch(market_watch_rows[0], None, "2026-10-11T09:30:00+03:30")
    assert snap["provider_timestamp"] is None and snap["last_trade_time"] == "12:29:59"
    row = dict(market_watch_rows[0], dEven=20261010)
    dated = TsetmcProvider.snapshot_from_market_watch(row, None, "2026-10-11T09:30:00+03:30")
    assert dated["provider_timestamp"] == "2026-10-10T12:29:59+03:30"
    row.pop("blDs")
    missing_depth = TsetmcProvider.snapshot_from_market_watch(row, None, "2026-10-11T09:30:00+03:30")
    assert missing_depth["buy_queue_volume"] is None and missing_depth["sell_queue_volume"] is None


def test_scope_does_not_guess_by_name_or_overwrite_identity(db):
    assert not UniverseManager.is_common_stock("نام ناشناس")
    assert UniverseManager.is_common_stock("امین", "IRO1AMIN0001")
    first = db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1", "instrument_type_code": 300})
    assert db.upsert_symbol({"symbol_fa": "الف", "ins_code": "2", "instrument_type_code": 300}) is None
    assert db.get_symbol_id("الف") == first and db.get_symbol("الف")["ins_code"] == "1"
    db.upsert_symbol({"symbol_fa": "صندوق", "ins_code": "3", "instrument_type_code": 305})
    collector = DataCollector(db=db, provider=TsetmcProvider(session=FakeSession({})))
    assert collector._resolve("صندوق") is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "bad", 0.5])
def test_non_integral_or_nonfinite_volume_rejected_at_storage(db, value):
    sid = db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1"})
    row = {"date": "2026-10-10", "open_raw": 100, "high_raw": 110, "low_raw": 90,
           "close_raw": 105, "close_final_raw": 104, "ref_price_raw": 100, "volume": value, "value": 10500}
    with pytest.raises(ValueError):
        db.upsert_daily_prices_df(sid, pd.DataFrame([row]))
    assert db.conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 0


def test_invalid_dates_and_conflicting_duplicates_are_rejected():
    row = {"date": "2026-02-30", "open": 100, "high": 110, "low": 90, "close": 105, "volume": 100, "value": 10500}
    assert not DataValidator.validate_daily_candle(row)[0]
    row["date"] = "2026-10-10"
    frame, rejected = DataValidator.validate_daily_dataframe(pd.DataFrame([row, dict(row, close=106)]))
    assert frame.empty and rejected == 2
    with pytest.raises(ValueError):
        deven_to_iso(20260230)
    with pytest.raises(ValueError):
        heven_to_time(256100)
    assert not is_market_open(datetime(2026, 10, 11, 12, 30, tzinfo=TEHRAN_TZ))


def test_sql_migration_failure_rolls_back_ddl_and_ledger(tmp_path, monkeypatch):
    path = tmp_path / "001_broken.sql"
    path.write_text("CREATE TABLE partial (id INTEGER);\nINSERT INTO missing VALUES (1);\n")
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", tmp_path)
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(sqlite3.OperationalError):
            migrator.apply_migrations(conn)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='partial'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
        path.write_text("CREATE TABLE complete (id INTEGER);\n")
        assert migrator.apply_migrations(conn) == ["001_broken"]
        path.write_text("CREATE TABLE changed (id INTEGER);\n")
        with pytest.raises(RuntimeError, match="مایگریشن"):
            migrator.apply_migrations(conn)


def test_raw_revisions_and_backup_names_are_preserved(db):
    sid = db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1"})
    for close in (105, 105, 106):
        assert db.insert_daily_price(sid, "2026-10-10", 100, 110, 90, close, volume=100, value=10500)
    revisions = db.conn.execute("SELECT close_raw FROM daily_prices_revisions ORDER BY revision_id").fetchall()
    assert [r[0] for r in revisions] == [105, 106]
    assert db.get_daily_prices_df("الف", verified_only=True).empty  # دستی/قدیمی، منشأ مستقیم ندارد
    first, second = db.backup(), db.backup()
    assert first != second and first.exists() and second.exists()
    with pytest.raises(ValueError):
        db.backup(0)


def test_duplicate_snapshots_count_only_actual_inserts(db):
    sid = db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1"})
    snap = {"captured_at": "2026-10-11T09:30:00+03:30", "last_price": 100, "volume": 100}
    assert db.insert_snapshots_bulk([(sid, snap), (sid, snap)]) == 1
    assert db.insert_snapshots_bulk([(sid, snap)]) == 0


def test_feature_runs_are_immutable_and_archive_exact_inputs(db, tmp_path):
    prices, ct = synthetic_history()
    sid = db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1", "instrument_type_code": 300})
    db.upsert_daily_prices_df(sid, prices, raw_provenance="tsetmc:daily")
    db.upsert_client_types_df(sid, ct)
    first = build_feature_store(db, root=tmp_path)
    original = load_feature_store(root=tmp_path, run_id=first["run_id"])
    ct.loc[0, "buy_i_count"] += 10
    db.upsert_client_types_df(sid, ct)
    second = build_feature_store(db, root=tmp_path)
    assert first["run_id"] != second["run_id"]
    pd.testing.assert_frame_equal(original, load_feature_store(root=tmp_path, run_id=first["run_id"]))
    assert first["files_sha256"]["input_client_types.parquet"] != second["files_sha256"]["input_client_types.parquet"]
    assert db.conn.execute("SELECT COUNT(*) FROM client_types_daily_revisions").fetchone()[0] == len(ct) + 1
    target = tmp_path / second["version"] / second["run_id"] / "daily.parquet"
    target.write_bytes(target.read_bytes() + b"broken")
    with pytest.raises(ValueError, match="اثر انگشت"):
        load_feature_store(root=tmp_path)


def test_feature_build_rejects_legacy_provenance_and_missing_reference(db, tmp_path):
    sid = db.upsert_symbol({"symbol_fa": "الف", "ins_code": "1", "instrument_type_code": 300})
    prices, _ = synthetic_history()
    db.upsert_daily_prices_df(sid, prices)
    with pytest.raises(ValueError, match="خام"):
        build_feature_store(db, root=tmp_path)
    prices.loc[5, "ref_price_raw"] = None
    with pytest.raises(ValueError, match="ref_price_raw"):
        compute_daily_features(prices)


@pytest.mark.parametrize("section,key,value", [
    ("fees", "buy", float("nan")), ("backtest", "max_volume_participation_pct", 0.06),
    ("market", "price_unit", "Toman"), ("governance", "enforce_point_in_time", False),
    ("universe", "include_base_market", "false"), ("data_collection", "request_timeout_seconds", -1),
])
def test_invalid_configuration_fails_explicitly(section, key, value):
    config = copy.deepcopy(settings.CONFIG)
    config[section][key] = value
    with pytest.raises(ValueError):
        settings.validate_config(config)


def test_doctor_empty_http_success_is_not_market_success(monkeypatch):
    from ops import doctor
    from tests.conftest import FakeResponse

    monkeypatch.setattr(doctor, "_timed_get", lambda *a, **k: (FakeResponse({"marketwatch": []}), 0.1))
    report = DoctorReport()
    assert check_tsetmc(report) is None and report.worst == ERROR


def test_backtest_blocks_unknown_liquidity_and_cumulative_value_cap():
    engine = BacktestEngine(initial_capital=1e8)
    assert engine.execute_buy("x", 100, 100, "2026-10-10") is None
    limits = dict(daily_volume=10000, daily_value=100000, day_low=90, day_high=110, is_locked_limit_up=False)
    trade = engine.execute_buy("x", 100, 1000, "2026-10-10", **limits)
    assert trade and trade.gross_value <= 5000 and trade.quantity <= 500
    assert engine.execute_buy("x", 100, 1000, "2026-10-10", **limits) is None
    assert engine.execute_buy("y", 100, 100, "2026-10-10", **dict(limits, daily_volume=0)) is None


@pytest.mark.parametrize("price,quantity", [(float("nan"), 10), (float("inf"), 10), (-1, 10), (100, 0.5), (100, True)])
def test_backtest_rejects_invalid_orders_without_changing_cash(price, quantity):
    engine = BacktestEngine(allow_unverified_fills=True)
    before = engine.cash
    assert engine.execute_buy("x", price, quantity, "2026-10-10") is None
    assert engine.cash == before


def test_partial_exits_do_not_inflate_win_rate_and_undefined_metrics_are_null():
    engine = BacktestEngine(initial_capital=100000, allow_unverified_fills=True, default_slippage_bps=0)
    engine.execute_buy("x", 100, 100, "d1")
    engine.execute_sell("x", 110, 50, "d2")
    assert engine.calculate_metrics().winning_trades == 0
    engine.execute_sell("x", 110, 50, "d3")
    report = engine.calculate_metrics()
    assert report.winning_trades == 1 and report.profit_factor is None and report.sharpe_ratio is None
    assert report.unverified_fill_mode and report.realized_pnl == report.net_pnl


def test_drawdown_includes_initial_capital_and_raw_callback_hides_adjustment():
    engine = BacktestEngine(initial_capital=100000, allow_unverified_fills=True)
    engine.execute_buy("x", 100, 100, "d1")
    engine.record_equity("d1")
    assert engine.calculate_metrics().max_drawdown_pct > 0
    frame = pd.DataFrame({"date": ["d1", "d2"], "close": [100, 100], "volume": [10000, 10000],
                          "adj_close": [90, 100], "adjustment_factor": [.9, 1]})
    seen = []
    BacktestEngine(allow_unverified_fills=True).run_replay_simulation(frame, "x", lambda row, e: seen.append(set(row.index)))
    assert all(not {"adj_close", "adjustment_factor", "close"} & columns for columns in seen)


def test_experiments_cannot_be_silently_replaced(db):
    params = dict(experiment_id="immutable", strategy_name="test", symbols_tested="الف", parameters_json="{}", metrics_json="{}")
    assert db.log_experiment(**params)
    assert db.log_experiment(**dict(params, metrics_json='{"profit":99}')) is None
    assert json.loads(db.get_experiments()[0]["metrics_json"]) == {}


def test_same_day_during_session_is_not_final_history(monkeypatch, closing_price_daily):
    from data_collector import tsetmc
    monkeypatch.setattr(tsetmc, "now_tehran", lambda: datetime(2026, 10, 10, 10, tzinfo=TEHRAN_TZ))
    provider = TsetmcProvider(session=FakeSession({"GetClosingPriceDailyList": {"closingPriceDaily": closing_price_daily}}))
    frame = provider.get_symbol_history("1")
    assert "2026-10-10" not in set(frame["date"])


def test_corporate_action_with_open_position_requires_explicit_accounting():
    rows = pd.DataFrame([
        ["2026-10-01", 90, 110, 100, 100, 100, 10000, 1000000],
        ["2026-10-02", 90, 110, 100, 100, 100, 10000, 1000000],
        ["2026-10-03", 45, 55, 50, 50, 50, 10000, 500000],
    ], columns=["date", "low_raw", "high_raw", "close_raw", "close_final_raw", "ref_price_raw", "volume", "value"])
    engine = BacktestEngine()
    with pytest.raises(ValueError, match="اقدام شرکتی"):
        engine.run_replay_simulation(rows, "x", lambda row, e: "BUY" if row["date"] == "2026-10-01" else None)
