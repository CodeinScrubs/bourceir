"""Local manual-workbench persistence. Market data and account information remain separate."""

import json
import uuid
from datetime import datetime

from config import settings
from core.journal import summarize_executions
from core.risk import RiskProfile, finite_number
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, now_tehran, now_tehran_iso


class WorkbenchRepository:
    def __init__(self, db):
        self.db = db

    def profile(self):
        row = self.db.conn.execute("SELECT * FROM personal_profile WHERE id = 1").fetchone()
        return dict(row) if row else None

    def save_profile(self, profile: RiskProfile, actual_history_complete: bool = False):
        if not isinstance(actual_history_complete, bool):
            raise ValueError("تأیید کامل بودن دفتر باید بولی باشد")
        existing = self.profile()
        if existing and existing["capital_rial"] != profile.capital_rial and self.db.conn.execute(
                "SELECT COUNT(*) FROM journal_executions").fetchone()[0]:
            raise ValueError("سرمایه مبنا پس از ثبت اجرا تغییر نمی‌کند؛ واریز/برداشت نیازمند دفتر جداست")
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO personal_profile VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "capital_rial=excluded.capital_rial, max_loss_rial=excluded.max_loss_rial, "
                "actual_history_complete=excluded.actual_history_complete, updated_at=excluded.updated_at",
                (int(profile.capital_rial), int(profile.max_loss_rial), int(actual_history_complete), now_tehran_iso()))

    def executions(self, mode: str):
        self._check_mode(mode)
        return [dict(row) for row in self.db.conn.execute(
            "SELECT j.*, s.symbol_fa FROM journal_executions j JOIN symbols s ON s.id=j.symbol_id "
            "WHERE j.mode=? ORDER BY j.executed_at, j.recorded_at, j.rowid", (mode,))]

    def plans(self, mode: str):
        self._check_mode(mode)
        return [dict(row) for row in self.db.conn.execute(
            "SELECT p.*, s.symbol_fa FROM trade_plans p JOIN symbols s ON s.id=p.symbol_id "
            "WHERE p.mode=? ORDER BY p.created_at DESC, p.rowid DESC", (mode,))]

    def market_rows(self):
        """Latest raw row for each active ordinary equity; no artificial value for absent symbols."""
        import pandas as pd
        return pd.read_sql_query(
            "SELECT s.id AS symbol_id, s.symbol_fa, s.instrument_type_code, s.is_active, s.sector_code, "
            "p.date, p.open_raw, p.high_raw, p.low_raw, p.close_raw, p.close_final_raw, p.ref_price_raw, p.volume, p.value "
            "FROM symbols s JOIN daily_prices p ON p.id=(SELECT q.id FROM daily_prices q "
            "WHERE q.symbol_id=s.id AND q.source='tsetmc' ORDER BY q.date DESC LIMIT 1) "
            "WHERE s.is_active=1 AND s.instrument_type_code IN (300,303)", self.db.conn)

    def latest_snapshot(self, symbol):
        row = self.db.conn.execute(
            "SELECT * FROM snapshots WHERE symbol_id=? AND source='tsetmc' ORDER BY captured_at DESC LIMIT 1",
            (self._symbol_id(symbol),)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _check_mode(mode):
        if mode not in ("paper", "actual"):
            raise ValueError("نوع دفتر باید کاغذی یا واقعی باشد")

    def _symbol_id(self, symbol):
        row = self.db.get_symbol(normalize_symbol(symbol))
        if not row or row["instrument_type_code"] not in (300, 303):
            raise ValueError("نماد سهام بورس/فرابورس در جامعه ثبت نشده است")
        return row["id"]

    def record_execution(self, *, mode, symbol, side, quantity, price_rial, executed_at,
                         rationale, fee_rial=None, plan_id=None, execution_id=None):
        """Append a reported execution; serialize inventory/cash checks and insert in one transaction.

        Real fills are recorded even when a risk limit was breached; a journal must not hide losses.
        Idempotency keys make repeat form submission safe. No market-feed record is created.
        """
        self._check_mode(mode)
        quantity, price = finite_number(quantity), finite_number(price_rial)
        if not quantity.is_integer() or side not in ("buy", "sell") or not rationale.strip():
            raise ValueError("تعداد صحیح، نوع اجرا و دلیل لازم است")
        timestamp = datetime.fromisoformat(executed_at)
        if timestamp.tzinfo is None or timestamp > now_tehran():
            raise ValueError("زمان اجرا باید با منطقه زمانی و حداکثر زمان کنونی باشد")
        timestamp = timestamp.astimezone(TEHRAN_TZ).isoformat()
        fee_source = "actual" if fee_rial is not None else "config_estimate"
        fee = finite_number(fee_rial, positive=False) if fee_rial is not None else quantity * price * settings.FEES[side]
        key = execution_id or uuid.uuid4().hex
        symbol_id = self._symbol_id(symbol)
        event = dict(symbol_fa=normalize_symbol(symbol), quantity=int(quantity), price_rial=price,
                     fee_rial=fee, fee_source=fee_source, side=side, executed_at=timestamp)
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            duplicate = conn.execute("SELECT * FROM journal_executions WHERE id=?", (key,)).fetchone()
            if duplicate:
                expected = (symbol_id, mode, side, int(quantity), price, fee, fee_source, timestamp, rationale.strip(), plan_id)
                actual = tuple(duplicate[k] for k in ("symbol_id", "mode", "side", "quantity", "price_rial", "fee_rial",
                                                       "fee_source", "executed_at", "rationale", "plan_id"))
                if actual != expected:
                    raise ValueError("شناسه تکراری برای اجرای متفاوت استفاده شده است")
                conn.rollback()
                return key
            profile = self.profile()
            if profile is None:
                raise ValueError("ابتدا سرمایه و سقف زیان را ثبت کنید")
            events = self.executions(mode)
            if events and timestamp < events[-1]["executed_at"]:
                raise ValueError("اجراها باید به ترتیب زمانی ثبت شوند")
            if plan_id:
                plan = conn.execute("SELECT * FROM trade_plans WHERE id=?", (plan_id,)).fetchone()
                if not plan or plan["mode"] != mode or plan["symbol_id"] != symbol_id:
                    raise ValueError("برنامه مربوط به این نماد و دفتر نیست")
            summarize_executions(events + [event], RiskProfile(profile["capital_rial"], profile["max_loss_rial"]), {})
            conn.execute("INSERT INTO journal_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (key, symbol_id, mode, side, int(quantity), price, fee, fee_source, timestamp,
                          now_tehran_iso(), rationale.strip(), plan_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return key

    def save_plan(self, *, symbol, mode, horizon, entry_rial, stop_rial, target_rial, rationale, calculation):
        self._check_mode(mode)
        if horizon not in ("intraday", "swing", "weeks_months") or not rationale.strip():
            raise ValueError("افق و دلیل برنامه لازم است")
        if calculation.get("blockers") or calculation.get("quantity", 0) < 1:
            raise ValueError("برنامه با کنترل ریسک ناموفق ذخیره نمی‌شود")
        entry, stop, target = map(finite_number, (entry_rial, stop_rial, target_rial))
        if not stop < entry < target:
            raise ValueError("حد زیان < ورود < هدف لازم است")
        key = uuid.uuid4().hex
        with self.db.conn:
            self.db.conn.execute("INSERT INTO trade_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (key, self._symbol_id(symbol), now_tehran_iso(), horizon, mode, calculation["quantity"],
                                  entry, stop, target, rationale.strip(),
                                  json.dumps(calculation, ensure_ascii=False, allow_nan=False)))
        return key
