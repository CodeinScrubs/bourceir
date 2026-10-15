"""Risk limits, ledger accounting, migration and UI flows with temporary data only."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from config import settings
from core.journal import summarize_executions
from core.risk import RiskProfile, plan_trade
from core.screening import current_daily_row, current_snapshot, descriptive_screen
from core.timeutils import TEHRAN_TZ, now_tehran, now_tehran_iso
from database.workbench import WorkbenchRepository
from tests.test_dashboard import dashboard_db as dashboard_db


def quote(**changes):
    inputs = dict(profile=RiskProfile(1_000_000, 100_000), entry_rial=1000, stop_rial=950, target_rial=1100,
                  trade_loss_budget_rial=10_000, available_cash_rial=1_000_000, equity_rial=1_000_000,
                  daily_value_rial=1_000_000, data_current=True, account_complete=True)
    inputs.update(changes)
    return plan_trade(**inputs)


def test_plan_enforces_cash_fees_and_participation():
    result = quote()
    assert result["quantity"] == min(result["caps"].values())
    assert result["entry_cost_rial"] <= 1_000_000
    assert result["stop_loss_scenario_rial"] <= 10_000
    assert result["quantity"] * 1000 * (1 + result["slippage_bps"] / 10000) <= 50_000
    assert result["breakeven_exit_rial"] > 1012
    assert result["target_net_scenario_rial"] > 0 and not result["stop_guaranteed"]


@pytest.mark.parametrize("changes", [dict(daily_value_rial=None), dict(data_current=False),
                                      dict(account_complete=False), dict(equity_rial=900_000),
                                      dict(reserved_risk_rial=100_000), dict(locked_up=True), dict(locked_down=True)])
def test_unknowns_or_loss_limit_block_new_plan(changes):
    result = quote(**changes)
    assert result["quantity"] == 0 and result["blockers"]


def test_open_risk_uses_remaining_total_budget():
    result = quote(trade_loss_budget_rial=20_000, reserved_risk_rial=98_000)
    assert result["remaining_loss_budget_rial"] == 2000
    assert result["stop_loss_scenario_rial"] <= 2000


@pytest.mark.parametrize("changes", [dict(entry_rial=float("nan")), dict(stop_rial=1001),
                                      dict(target_rial=999), dict(requested_quantity=1.5), dict(slippage_bps=10000)])
def test_invalid_plan_inputs_are_rejected(changes):
    with pytest.raises(ValueError):
        quote(**changes)


def test_round_trip_and_partial_exit_include_actual_fees():
    events = [dict(symbol_fa="فولاد", side="buy", quantity=10, price_rial=1000, fee_rial=40),
              dict(symbol_fa="فولاد", side="sell", quantity=4, price_rial=1100, fee_rial=40)]
    state = summarize_executions(events, RiskProfile(100_000, 10_000), {"فولاد": 1100})
    assert state["cash_rial"] == 94320
    assert state["realized_net_rial"] == pytest.approx(344)
    assert state["positions"]["فولاد"]["quantity"] == 6
    assert state["positions"]["فولاد"]["cost_rial"] == pytest.approx(6024)
    assert state["equity_rial"] == pytest.approx(94320 + 6600 * (1 - settings.FEES["sell"]))
    unknown = summarize_executions(events, RiskProfile(100_000, 10_000), {})
    assert unknown["equity_rial"] is None and unknown["unknown_marks"] == ["فولاد"]


@pytest.fixture
def workbench(db):
    db.upsert_symbol(dict(symbol_fa="فولاد", ins_code="123", instrument_type_code=300, sector_code="27"))
    repo = WorkbenchRepository(db)
    repo.save_profile(RiskProfile(100_000, 10_000))
    return repo


def execution(repo, **changes):
    args = dict(mode="paper", symbol="فولاد", side="buy", quantity=10, price_rial=1000,
                fee_rial=40, executed_at=(now_tehran() - timedelta(seconds=2)).isoformat(), rationale="دلیل دستی")
    args.update(changes)
    return repo.record_execution(**args)


def test_journal_modes_idempotency_and_append_only(workbench, db):
    timestamp = (now_tehran() - timedelta(seconds=2)).isoformat()
    first = execution(workbench, execution_id="same", executed_at=timestamp)
    assert execution(workbench, execution_id="same", executed_at=timestamp) == first
    assert len(workbench.executions("paper")) == 1 and workbench.executions("actual") == []
    with pytest.raises(ValueError, match="متفاوت"):
        execution(workbench, execution_id="same", executed_at=timestamp, quantity=11)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("DELETE FROM journal_executions WHERE id='same'")
    db.conn.rollback()
    with pytest.raises(ValueError, match="موجودی"):
        execution(workbench, side="sell", quantity=11)
    assert len(workbench.executions("paper")) == 1


def test_realized_loss_is_recorded_even_when_limit_was_breached(workbench):
    execution(workbench, quantity=80)
    execution(workbench, side="sell", quantity=80, price_rial=500)
    state = summarize_executions(workbench.executions("paper"), RiskProfile(100_000, 10_000), {})
    assert state["loss_limit_reached"] and state["realized_net_rial"] < -40_000


def test_journal_rejects_future_naive_time_and_backdating(workbench):
    for timestamp in ("2024-01-01T12:00:00", (now_tehran() + timedelta(days=1)).isoformat()):
        with pytest.raises(ValueError):
            execution(workbench, executed_at=timestamp)
    execution(workbench)
    with pytest.raises(ValueError, match="ترتیب"):
        execution(workbench, executed_at=(now_tehran() - timedelta(days=1)).isoformat())
    with pytest.raises(ValueError, match="سرمایه مبنا"):
        workbench.save_profile(RiskProfile(200_000, 10_000))


def test_market_rows_and_weekend_freshness(workbench, db):
    at = datetime(2026, 10, 15, 15, tzinfo=TEHRAN_TZ)  # Thursday: last expected session Wednesday
    assert current_daily_row("2026-10-14", at) and not current_daily_row("2026-10-11", at)
    assert current_snapshot((at - timedelta(seconds=20)).isoformat(), at)
    assert not current_snapshot((at - timedelta(seconds=60)).isoformat(), at)
    assert not current_snapshot("2026-10-15T15:00:00", at)
    sid = db.get_symbol_id("فولاد")
    db.insert_daily_price(sid, "2026-10-14", open=1000, high=1050, low=950, close=1000,
                          close_raw=1000, close_final_raw=1000, ref_price_raw=1000, volume=1000, value=1_000_000)
    rows = workbench.market_rows()
    assert len(rows) == 1
    result = descriptive_screen(rows, 500_000, at)
    assert result.iloc[0]["current"] and result.iloc[0]["liquid"]


def test_migration_eight_is_repeatable_and_keeps_existing_prices(db):
    from database.migrator import MIGRATIONS_DIR, _run_sql_migration
    _run_sql_migration(db.conn, MIGRATIONS_DIR / "008_personal_workbench.sql")
    db.conn.commit()
    assert db.schema_version() >= 8
    assert {"personal_profile", "trade_plans", "journal_executions"} <= set(db.table_counts())


def test_saved_plan_retains_cost_assumptions_and_reason(workbench):
    calculation = quote()
    key = workbench.save_plan(symbol="فولاد", mode="paper", horizon="swing", entry_rial=1000,
                              stop_rial=950, target_rial=1100, rationale="برنامه کاربر", calculation=calculation)
    saved = workbench.plans("paper")[0]
    assert saved["id"] == key and saved["rationale"] == "برنامه کاربر"
    assert now_tehran_iso() >= saved["created_at"]


def test_workbench_ui_stale_data_blocks_and_records_manual_fill(dashboard_db):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD
    with DatabaseManager(db_path=dashboard_db) as db:
        repo = WorkbenchRepository(db)
        repo.save_profile(RiskProfile(100_000, 10_000))
        sid = db.upsert_symbol(dict(symbol_fa="فولاد", ins_code="123", instrument_type_code=300))
        db.insert_daily_price(sid, "2024-01-01", open=1000, high=1050, low=950, close=1000,
                              close_raw=1000, close_final_raw=1000, ref_price_raw=1000, volume=1000, value=1_000_000)
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value("🛡️ برنامه و دفتر معاملات").run()
    assert not at.exception
    by_label = {widget.label: widget for widget in at.number_input}
    by_label["قیمت سناریوی خروج با زیان (تومان)"].set_value(95.0)
    by_label["قیمت هدف خودت (تومان)"].set_value(110.0)
    by_label["بودجه زیان همین معامله (تومان)"].set_value(1000)
    at.run()
    assert not at.exception and any("به‌روز" in w.value for w in at.warning)
    assert not any(b.label == "ثبت برنامه دستی" for b in at.button)
    next(w for w in at.number_input if w.label == "قیمت اجرای خودت (تومان)").set_value(100.0)
    next(w for w in at.text_area if w.label == "دلیل و توضیح اجرا").set_value("اجرای دستی کاغذی")
    next(b for b in at.button if b.label == "ثبت اجرای گزارش‌شده").click().run()
    assert not at.exception
    with DatabaseManager(db_path=dashboard_db, read_only=True) as db:
        assert len(WorkbenchRepository(db).executions("paper")) == 1
        assert WorkbenchRepository(db).executions("actual") == []


def test_workbench_intraday_queue_uses_the_live_book_in_the_ui(dashboard_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from core import risk, screening
    from database.db import DatabaseManager
    from tests.test_audit_followup import live_book
    from tests.test_dashboard import DASHBOARD
    clock, snapshot = live_book(buy_queue_volume=500000, bid_price_1=1030,
                                bid_vol_1=500000, ask_price_1=0, ask_vol_1=0, ask_count_1=0)
    monkeypatch.setattr(risk, 'now_tehran', lambda: clock)
    monkeypatch.setattr(screening, 'now_tehran', lambda: clock)
    with DatabaseManager(db_path=dashboard_db) as db:
        WorkbenchRepository(db).save_profile(RiskProfile(1_000_000, 100_000))
        sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
        db.insert_daily_price(sid, '2026-10-13', 1000, 1050, 950, 1000, volume=1000, value=1_000_000,
                              close_final_raw=1000, ref_price_raw=1000)
        db.insert_snapshot(sid, snapshot)
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🛡️ برنامه و دفتر معاملات').run()
    by_label = {widget.label: widget for widget in at.number_input}
    by_label['قیمت سناریوی خروج با زیان (تومان)'].set_value(95.)
    by_label['قیمت هدف خودت (تومان)'].set_value(110.)
    by_label['بودجه زیان همین معامله (تومان)'].set_value(1000)
    at.run()
    assert not at.exception
    assert not any(button.label == 'ثبت برنامه دستی' for button in at.button)
    assert any('عرضه' in warning.value or 'صف' in warning.value for warning in at.warning)
