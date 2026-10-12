# -*- coding: utf-8 -*-
"""
ماژول مدیریت و اتصال به پایگاه داده SQLite (data/brs.db)

- ساختار جداول فقط از طریق مایگریشن‌ها (database/migrations) ساخته و تغییر داده می‌شود.
- حالت WAL فعال است تا داشبورد بتواند هم‌زمان با گردآورنده داده، دیتابیس را بخواند.
- هویت هر نماد با شناسه پایدار TSETMC (ins_code) تعیین می‌شود و نام نماد یکسان‌سازی می‌شود.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from config import settings
from core import market_rules
from core.text import normalize_fa, normalize_symbol
from core.timeutils import now_tehran_iso
from database.migrator import apply_migrations, backup_database, current_version, pending_migrations

logger = logging.getLogger(__name__)

# منبع پیش‌فرض قیمت‌ها؛ داده‌های روزانه با این منبع خوانده می‌شوند
PRICE_SOURCE = "tsetmc"

SNAPSHOT_COLUMNS = [
    "captured_at", "provider_timestamp", "last_trade_time",
    "last_price", "close_price", "open_price", "high_price", "low_price",
    "ref_price", "price_limit_up", "price_limit_down", "base_volume",
    "volume", "value", "trade_count",
    "buy_queue_volume", "sell_queue_volume",
    "real_buy_count", "real_sell_count", "legal_buy_count", "legal_sell_count",
    "real_buy_volume", "real_sell_volume", "legal_buy_volume", "legal_sell_volume",
    "price_unit", "source",
] + [
    f"{side}_{field}_{level}"
    for level in range(1, 6)
    for side in ("bid", "ask")
    for field in ("price", "vol", "count")
]

CLIENT_TYPE_COLUMNS = [
    "buy_i_volume", "buy_n_volume", "buy_i_value", "buy_n_value", "buy_i_count", "buy_n_count",
    "sell_i_volume", "sell_n_volume", "sell_i_value", "sell_n_value", "sell_i_count", "sell_n_count",
]


def get_current_tehran_time() -> str:
    """زمان کنونی تهران به صورت ISO (نام قدیمی برای سازگاری)."""
    return now_tehran_iso()


def _none_if_nan(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


class DatabaseManager:
    """
    کلاس مدیریت پایگاه داده SQLite برای ذخیره و بازیابی اطلاعات بازار بورس.
    پشتیبانی از پروتکل Context Manager با استفاده از دستور with.
    """

    def __init__(self, db_path: Optional[Path | str] = None, read_only: bool = False):
        """
        read_only=True: اتصال فقط‌خواندنی (برای داشبورد)؛ هیچ قفل نوشتنی نمی‌گیرد و مایگریشن اجرا نمی‌کند،
        پس با پایش زنده یا به‌روزرسانی هم‌زمان تداخل ندارد.
        """
        self.db_path = Path(db_path) if db_path is not None else Path(settings.DB_PATH)
        self.read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = None
        self._connect()
        if not read_only:
            self.init_db()

    # ═══════════════════════════════════════════════
    # اتصال و مایگریشن
    # ═══════════════════════════════════════════════

    def _connect(self) -> None:
        try:
            if self.read_only:
                uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
                self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)
            else:
                self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
                self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=30000;")
            logger.debug("اتصال به پایگاه داده برقرار شد: %s", self.db_path)
        except Exception as e:
            logger.error("خطا در برقراری اتصال به پایگاه داده: %s", e)
            raise

    def __enter__(self):
        if self.conn is None:
            self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def init_db(self) -> List[str]:
        """
        اعمال مایگریشن‌های اجرانشده. اگر دیتابیس از قبل داده داشته باشد، ابتدا نسخه پشتیبان گرفته می‌شود.
        خروجی: نام مایگریشن‌های اعمال‌شده در این اجرا.
        """
        pending = pending_migrations(self.conn)
        has_data = self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbols';"
        ).fetchone()[0] > 0
        if pending and has_data:
            self.backup(keep=5)
        applied = apply_migrations(self.conn)
        if applied:
            logger.info("مایگریشن‌های اعمال‌شده: %s", ", ".join(applied))
        self.conn.execute("PRAGMA foreign_keys=ON;")
        return applied

    def schema_version(self) -> int:
        return current_version(self.conn)

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception as e:
                logger.error("خطا در بستن اتصال پایگاه داده: %s", e)
            finally:
                self.conn = None

    # ═══════════════════════════════════════════════
    # نمادها
    # ═══════════════════════════════════════════════

    def upsert_symbol(self, meta: Dict[str, Any]) -> Optional[int]:
        """
        ثبت یا به‌روزرسانی نماد. کلید اصلی ins_code است؛ اگر نماد با همین نام اما بدون ins_code
        وجود داشته باشد، همان ردیف تکمیل می‌شود (هرگز ردیف تکراری ساخته نمی‌شود).
        """
        symbol_fa = normalize_symbol(meta.get("symbol_fa", ""))
        if not symbol_fa:
            return None
        ins_code = meta.get("ins_code") or None
        now = now_tehran_iso()
        isin = meta.get("tse_code") if str(meta.get("tse_code") or "").startswith("IR") else None
        # فقط فیلدهای ارائه‌شده به‌روز می‌شوند؛ مقدار پیش‌فرض فقط هنگام درج ردیف جدید استفاده می‌شود
        fields = {
            "tse_code": isin,
            "name_fa": normalize_fa(meta["name_fa"]) if meta.get("name_fa") else None,
            "market": meta.get("market"),
            "instrument_type": meta.get("instrument_type"),
            "ins_code": ins_code,
            "board_code": meta.get("board_code"),
            "instrument_type_code": meta.get("instrument_type_code"),
            "flow": meta.get("flow"),
            "sector_code": meta.get("sector_code"),
            "sector_name": meta.get("sector_name"),
            "shares_outstanding": meta.get("shares_outstanding"),
            "base_volume": meta.get("base_volume"),
        }
        try:
            with self.conn:
                row = None
                if ins_code:
                    row = self.conn.execute("SELECT id FROM symbols WHERE ins_code = ?;", (ins_code,)).fetchone()
                if row is None:
                    row = self.conn.execute("SELECT id FROM symbols WHERE symbol_fa = ?;", (symbol_fa,)).fetchone()
                if row is None:
                    insert_fields = dict(fields)
                    insert_fields["tse_code"] = isin or ins_code or meta.get("tse_code") or symbol_fa
                    insert_fields["name_fa"] = fields["name_fa"] or symbol_fa
                    insert_fields["instrument_type"] = fields["instrument_type"] or "stock"
                    cols = ["symbol_fa", "is_active", "created_at", "updated_at", "last_seen_at"] + list(insert_fields)
                    vals = [symbol_fa, 1, now, now, now] + list(insert_fields.values())
                    cur = self.conn.execute(
                        f"INSERT INTO symbols ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))});", vals
                    )
                    return cur.lastrowid
                if ins_code:
                    existing = self.conn.execute("SELECT ins_code FROM symbols WHERE id = ?;", (row["id"],)).fetchone()
                    if existing["ins_code"] and existing["ins_code"] != ins_code:
                        # معمولاً یعنی انتقال نماد بین بازارها (مثلاً فرابورس به بورس) با شناسه جدید
                        logger.warning("شناسه TSETMC نماد «%s» از %s به %s تغییر کرد.", symbol_fa,
                                       existing["ins_code"], ins_code)
                updates = {k: v for k, v in fields.items() if v is not None}
                updates.update({"symbol_fa": symbol_fa, "updated_at": now, "last_seen_at": now, "is_active": 1})
                self.conn.execute(
                    f"UPDATE symbols SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?;",
                    list(updates.values()) + [row["id"]],
                )
                return row["id"]
        except sqlite3.IntegrityError as e:
            # هرگز شناسه نماد دیگری برگردانده نمی‌شود (جلوگیری از قاطی شدن داده دو نماد)
            logger.error("تعارض در ثبت نماد %s (ins_code=%s): %s", symbol_fa, ins_code, e)
            return None

    def insert_symbol(self, tse_code: str, symbol_fa: str, name_fa: Optional[str] = None,
                      market: Optional[str] = "bourse", instrument_type: Optional[str] = "stock") -> Optional[int]:
        """رابط قدیمی ثبت نماد (برای سازگاری)."""
        return self.upsert_symbol({
            "tse_code": tse_code, "symbol_fa": symbol_fa, "name_fa": name_fa,
            "market": market, "instrument_type": instrument_type,
        })

    def get_symbol(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM symbols WHERE symbol_fa = ? LIMIT 1;", (normalize_symbol(symbol_fa),)
        ).fetchone()
        return dict(row) if row else None

    def get_symbol_id(self, symbol_fa: str) -> Optional[int]:
        """یافتن شناسه نماد بر اساس نام فارسی (با یکسان‌سازی ی/ک عربی)."""
        sym = self.get_symbol(symbol_fa)
        return sym["id"] if sym else None

    def get_all_symbols(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """فهرست نمادهای فعال (یا همه نمادها با include_inactive=True)."""
        where = "" if include_inactive else "WHERE is_active = 1"
        rows = self.conn.execute(f"SELECT * FROM symbols {where} ORDER BY symbol_fa ASC;").fetchall()
        return [dict(r) for r in rows]

    def set_symbols_active(self, symbol_ids: List[int], active: bool) -> int:
        """فعال/غیرفعال کردن نمادها (داده‌های نماد هرگز حذف نمی‌شود)."""
        if not symbol_ids:
            return 0
        now = now_tehran_iso()
        with self.conn:
            cur = self.conn.executemany(
                "UPDATE symbols SET is_active = ?, status = ?, updated_at = ? WHERE id = ?;",
                [(int(active), "active" if active else "inactive", now, sid) for sid in symbol_ids],
            )
        return cur.rowcount

    def get_universe_symbols(self) -> List[Dict[str, Any]]:
        """نمادهای فعال داخل دامنه پروژه (سهام بورس و فرابورس طبق تنظیمات)."""
        allowed = list(settings.UNIVERSE["instrument_types"])
        if settings.UNIVERSE["include_base_market"]:
            allowed.append(309)
        if settings.UNIVERSE["include_sme_market"]:
            allowed.append(313)
        placeholders = ",".join("?" * len(allowed))
        rows = self.conn.execute(
            f"SELECT * FROM symbols WHERE is_active = 1 AND instrument_type_code IN ({placeholders}) "
            "ORDER BY symbol_fa ASC;", allowed,
        ).fetchall()
        return [dict(r) for r in rows if not settings.UNIVERSE["exclude_block_boards"]
                or not r["symbol_fa"][-1:].isdigit()]

    # ═══════════════════════════════════════════════
    # قیمت‌های روزانه
    # ═══════════════════════════════════════════════

    _DAILY_UPSERT = """
    INSERT INTO daily_prices (
        symbol_id, date, open, high, low, close, adj_close, volume, value, trade_count,
        price_unit, source, captured_at,
        open_raw, high_raw, low_raw, close_raw, close_final_raw, ref_price_raw, adjustment_factor
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(symbol_id, date, source) DO UPDATE SET
        open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
        volume = excluded.volume, value = excluded.value, trade_count = excluded.trade_count,
        captured_at = excluded.captured_at,
        open_raw = excluded.open_raw, high_raw = excluded.high_raw, low_raw = excluded.low_raw,
        close_raw = excluded.close_raw,
        close_final_raw = COALESCE(excluded.close_final_raw, daily_prices.close_final_raw),
        ref_price_raw = COALESCE(excluded.ref_price_raw, daily_prices.ref_price_raw);
    """

    def upsert_daily_prices_df(self, symbol_id: int, df: pd.DataFrame, source: str = PRICE_SOURCE,
                               recompute: bool = True) -> int:
        """
        ثبت گروهی کندل‌های روزانه استاندارد (خروجی BaseDataProvider.get_symbol_history).
        رکورد موجود با داده تازه منبع به‌روز می‌شود. خروجی: تعداد ردیف‌های پردازش‌شده.
        با recompute=False محاسبه دوباره ضرایب تعدیل انجام نمی‌شود (برای ثبت سریع کندل امروز کل بازار).
        """
        if df is None or df.empty:
            return 0
        now = now_tehran_iso()
        params = []
        for row in df.itertuples(index=False):
            r = row._asdict()
            o, h, lo, c = (_none_if_nan(r.get(k)) for k in ("open_raw", "high_raw", "low_raw", "close_raw"))
            vol, val, cnt = (_none_if_nan(r.get(k)) for k in ("volume", "value", "trade_count"))
            params.append((
                symbol_id, r["date"], o, h, lo, c, None,
                int(vol) if vol is not None else None, val, int(cnt) if cnt is not None else None,
                settings.PRICE_UNIT, source, now,
                o, h, lo, c, _none_if_nan(r.get("close_final_raw")), _none_if_nan(r.get("ref_price_raw")), 1.0,
            ))
        with self.conn:
            self.conn.executemany(self._DAILY_UPSERT, params)
        if recompute:
            self.recompute_adjustments(symbol_id, source=source)
        return len(params)

    def previous_close_final(self, symbol_id: int, before_date: str, source: str = PRICE_SOURCE) -> Optional[float]:
        """قیمت پایانی رسمی آخرین روز معاملاتی قبل از تاریخ داده‌شده."""
        row = self.conn.execute(
            "SELECT close_final_raw FROM daily_prices WHERE symbol_id = ? AND source = ? AND date < ? "
            "ORDER BY date DESC LIMIT 1;", (symbol_id, source, before_date),
        ).fetchone()
        return row[0] if row else None

    def insert_daily_price(
        self, symbol_id: int, date: str, open: float, high: float, low: float, close: float,
        adj_close: Optional[float] = None, volume: Optional[int] = None, value: Optional[float] = None,
        trade_count: Optional[int] = None, source: str = PRICE_SOURCE, price_unit: str = settings.PRICE_UNIT,
        open_raw: Optional[float] = None, high_raw: Optional[float] = None, low_raw: Optional[float] = None,
        close_raw: Optional[float] = None, adjustment_factor: Optional[float] = None,
        close_final_raw: Optional[float] = None, ref_price_raw: Optional[float] = None,
    ) -> bool:
        """ثبت یک کندل روزانه (رابط تک‌رکوردی؛ برای حجم بالا از upsert_daily_prices_df استفاده کنید)."""
        o_raw = open_raw if open_raw is not None else open
        h_raw = high_raw if high_raw is not None else high
        l_raw = low_raw if low_raw is not None else low
        c_raw = close_raw if close_raw is not None else close
        try:
            with self.conn:
                cur = self.conn.execute(
                    self._DAILY_UPSERT,
                    (symbol_id, date, o_raw, h_raw, l_raw, c_raw, adj_close, volume, value, trade_count,
                     price_unit, source, now_tehran_iso(),
                     o_raw, h_raw, l_raw, c_raw, close_final_raw, ref_price_raw,
                     adjustment_factor if adjustment_factor is not None else 1.0),
                )
                if adj_close is not None or adjustment_factor is not None:
                    self.conn.execute(
                        "UPDATE daily_prices SET adj_close = COALESCE(?, adj_close), "
                        "adjustment_factor = COALESCE(?, adjustment_factor) "
                        "WHERE symbol_id = ? AND date = ? AND source = ?;",
                        (adj_close, adjustment_factor, symbol_id, date, source),
                    )
                return cur.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت قیمت روزانه برای symbol_id=%s در تاریخ %s: %s", symbol_id, date, e)
            return False

    def recompute_adjustments(self, symbol_id: int, source: str = PRICE_SOURCE) -> int:
        """
        محاسبه مجدد ضریب اقدام شرکتی هر روز و قیمت پایانی تعدیل‌شده رو به عقب (فقط برای نمایش).
        فقط روزهایی که قیمت مرجع دارند معتبرند؛ بقیه ضریب ۱ می‌گیرند.
        """
        df = pd.read_sql_query(
            "SELECT id, date, close_raw, close_final_raw, ref_price_raw FROM daily_prices "
            "WHERE symbol_id = ? AND source = ? ORDER BY date ASC;",
            self.conn, params=(symbol_id, source),
        )
        if df.empty:
            return 0
        factors = market_rules.corporate_action_factors(df)
        adjusted = market_rules.backward_adjusted_close(df)
        updates = [
            (_none_if_nan(round(float(a), 4)) if pd.notna(a) else None, float(f), int(i))
            for a, f, i in zip(adjusted, factors, df["id"])
        ]
        with self.conn:
            self.conn.executemany("UPDATE daily_prices SET adj_close = ?, adjustment_factor = ? WHERE id = ?;", updates)
        return len(updates)

    def get_daily_prices_df(self, symbol_fa: str, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, raw_only: bool = True,
                            source: str = PRICE_SOURCE) -> pd.DataFrame:
        """
        سابقه قیمت روزانه یک نماد. با raw_only=True (پیش‌فرض) ستون‌های تعدیل‌شده برگردانده نمی‌شوند
        تا به‌اشتباه وارد فیچرها نشوند (قانون ۱۰ AGENTS.md).
        """
        sym_id = self.get_symbol_id(symbol_fa)
        if not sym_id:
            return pd.DataFrame()
        cols = ("date, open_raw, high_raw, low_raw, close_raw, close_final_raw, ref_price_raw, "
                "volume, value, trade_count")
        if not raw_only:
            cols += ", adj_close, adjustment_factor"
        query = f"SELECT {cols} FROM daily_prices WHERE symbol_id = ? AND source = ?"
        params: List[Any] = [sym_id, source]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date ASC;"
        return pd.read_sql_query(query, self.conn, params=params)

    def get_last_daily_date(self, symbol_id: int, source: str = PRICE_SOURCE) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices WHERE symbol_id = ? AND source = ?;", (symbol_id, source)
        ).fetchone()
        return row["d"] if row else None

    def get_latest_prices(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT dp.*, s.symbol_fa, s.name_fa, s.market
            FROM daily_prices dp JOIN symbols s ON dp.symbol_id = s.id
            ORDER BY dp.date DESC, dp.captured_at DESC LIMIT ?;
            """, (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════
    # حقیقی / حقوقی روزانه
    # ═══════════════════════════════════════════════

    def upsert_client_types_df(self, symbol_id: int, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        now = now_tehran_iso()
        cols = ["symbol_id", "date"] + CLIENT_TYPE_COLUMNS + ["source", "captured_at"]
        updates = ", ".join(f"{c} = excluded.{c}" for c in CLIENT_TYPE_COLUMNS + ["captured_at"])
        query = (
            f"INSERT INTO client_types_daily ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(symbol_id, date) DO UPDATE SET {updates};"
        )
        params = [
            [symbol_id, r["date"]] + [_none_if_nan(r.get(c)) for c in CLIENT_TYPE_COLUMNS] + ["tsetmc", now]
            for r in df.to_dict("records")
        ]
        with self.conn:
            self.conn.executemany(query, params)
        return len(params)

    def get_client_types_df(self, symbol_fa: str, start_date: Optional[str] = None) -> pd.DataFrame:
        sym_id = self.get_symbol_id(symbol_fa)
        if not sym_id:
            return pd.DataFrame()
        query = f"SELECT date, {', '.join(CLIENT_TYPE_COLUMNS)} FROM client_types_daily WHERE symbol_id = ?"
        params: List[Any] = [sym_id]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        return pd.read_sql_query(query + " ORDER BY date ASC;", self.conn, params=params)

    # ═══════════════════════════════════════════════
    # اسنپ‌شات لحظه‌ای تابلو
    # ═══════════════════════════════════════════════

    def _snapshot_params(self, symbol_id: int, data: Dict[str, Any]) -> List[Any]:
        values = dict(data)
        values.setdefault("captured_at", now_tehran_iso())
        values.setdefault("source", "tsetmc")
        values.setdefault("price_unit", settings.PRICE_UNIT)
        return [symbol_id] + [_none_if_nan(values.get(c)) for c in SNAPSHOT_COLUMNS]

    def _snapshot_query(self) -> str:
        cols = ["symbol_id"] + SNAPSHOT_COLUMNS
        return f"INSERT OR IGNORE INTO snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))});"

    def insert_snapshot(self, symbol_id: int, data_dict: Dict[str, Any]) -> bool:
        """ثبت اسنپ‌شات لحظه‌ای یک نماد (اگر captured_at نداشته باشد، زمان فعلی تهران ثبت می‌شود)."""
        try:
            with self.conn:
                cur = self.conn.execute(self._snapshot_query(), self._snapshot_params(symbol_id, data_dict))
                return cur.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت اسنپ‌شات برای symbol_id=%s: %s", symbol_id, e)
            return False

    def insert_snapshots_bulk(self, rows: Iterable[tuple]) -> int:
        """ثبت گروهی اسنپ‌شات‌ها؛ ورودی: [(symbol_id, snapshot_dict), ...]"""
        params = [self._snapshot_params(sym_id, data) for sym_id, data in rows]
        if not params:
            return 0
        with self.conn:
            cursor = self.conn.executemany(self._snapshot_query(), params)
        # INSERT OR IGNORE: تکراری‌ها درج نمی‌شوند؛ تعداد واقعی درج گزارش می‌شود، نه تعداد تلاش
        return max(cursor.rowcount, 0)

    # ═══════════════════════════════════════════════
    # اطلاعیه‌های کدال و قضاوت‌های هوش مصنوعی
    # ═══════════════════════════════════════════════

    def upsert_announcements(self, letters: List[Dict[str, Any]]) -> int:
        """ثبت اطلاعیه‌های کدال (اطلاعیه تکراری با شماره پیگیری یکسان نادیده گرفته می‌شود)."""
        inserted = 0
        now = now_tehran_iso()
        with self.conn:
            for letter in letters:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO announcements (
                        tracing_no, symbol_id, symbol_fa, company_name, title, letter_code,
                        sent_at, published_at, url, source, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'codal', ?);
                    """,
                    (
                        letter["tracing_no"], self.get_symbol_id(letter.get("symbol_fa", "")),
                        normalize_symbol(letter.get("symbol_fa", "")), normalize_fa(letter.get("company_name", "")),
                        normalize_fa(letter["title"]), letter.get("letter_code"),
                        letter.get("sent_at"), letter.get("published_at"), letter.get("url"), now,
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def get_unjudged_announcements(self, model: str, questions_version: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT a.* FROM announcements a
            WHERE NOT EXISTS (
                SELECT 1 FROM announcement_judgments j
                WHERE j.announcement_id = a.id AND j.model = ? AND j.questions_version = ?
            )
            ORDER BY a.published_at DESC LIMIT ?;
            """, (model, questions_version, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_announcement_judgment(self, announcement_id: int, model: str, questions_version: str,
                                     judgment: Dict[str, Any]) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT OR REPLACE INTO announcement_judgments (
                    announcement_id, model, questions_version, category, category_confidence,
                    impact_score, impact_confidence, is_material, answers_json, input_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    announcement_id, model, questions_version, judgment.get("category"),
                    judgment.get("category_confidence"), judgment.get("impact_score"),
                    judgment.get("impact_confidence"), judgment.get("is_material"),
                    json.dumps(judgment.get("raw", {}), ensure_ascii=False), judgment.get("input_tokens"),
                    now_tehran_iso(),
                ),
            )
            return cur.rowcount > 0

    # ═══════════════════════════════════════════════
    # لاگ، کیفیت داده، تقویم، اقدامات شرکتی، آزمایش‌ها، فیچرها
    # ═══════════════════════════════════════════════

    def get_collection_stats(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM collection_log ORDER BY started_at DESC LIMIT 20;").fetchall()
        return [dict(r) for r in rows]

    def log_collection(self, started_at: str, finished_at: str, collection_type: str, symbols_requested: int,
                       symbols_succeeded: int, symbols_failed: int, errors: Optional[str] = None,
                       status: str = "success") -> Optional[int]:
        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO collection_log (started_at, finished_at, collection_type, symbols_requested,
                        symbols_succeeded, symbols_failed, errors, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (started_at, finished_at, collection_type, symbols_requested, symbols_succeeded,
                     symbols_failed, errors, status),
                )
                return cur.lastrowid
        except Exception as e:
            logger.error("خطا در ثبت لاگ گردآوری: %s", e)
            return None

    def insert_quality_metric(self, symbol_id: int, trade_date: str, completeness_score: Optional[float] = None,
                              price_anomaly_flag: bool = False, volume_anomaly_flag: bool = False,
                              quality_score: Optional[float] = None, notes: Optional[str] = None) -> bool:
        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT OR REPLACE INTO data_quality_metrics (symbol_id, trade_date, completeness_score,
                        price_anomaly_flag, volume_anomaly_flag, quality_score, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (symbol_id, trade_date, completeness_score, int(price_anomaly_flag),
                     int(volume_anomaly_flag), quality_score, notes),
                )
                return cur.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت متریک کیفیت داده: %s", e)
            return False

    def insert_quality_metrics_bulk(self, rows: List[Dict[str, Any]]) -> int:
        """ثبت گروهی متریک‌های کیفیت (سریع؛ برای علامت‌گذاری ناهنجاری‌های تاریخچه کامل)."""
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO data_quality_metrics (symbol_id, trade_date, completeness_score,
                    price_anomaly_flag, volume_anomaly_flag, quality_score, notes)
                VALUES (:symbol_id, :trade_date, :completeness_score, :price_anomaly_flag,
                        :volume_anomaly_flag, :quality_score, :notes);
                """,
                # امتیاز کیفیت تعریف‌شده‌ای نداریم؛ عدد ساختگی ذخیره نمی‌شود (فقط پرچم ناهنجاری و توضیح)
                [{"completeness_score": None, "quality_score": None, "notes": None, **r} for r in rows],
            )
        return len(rows)

    def backup(self, keep: int = 5) -> Path:
        """نسخه پشتیبان کامل در data/backups (فقط keep نسخه آخر نگه داشته می‌شود)."""
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise ValueError("حداقل یک نسخه پشتیبان باید نگه داشته شود")
        target = backup_database(self.conn, self.db_path)
        backups = sorted((self.db_path.parent / "backups").glob(f"{self.db_path.stem}-*.db"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        return target

    def integrity_check(self) -> str:
        """بررسی سلامت فایل دیتابیس (خروجی 'ok' یعنی سالم)."""
        return self.conn.execute("PRAGMA quick_check;").fetchone()[0]

    def insert_market_calendar(self, calendar_date: str, is_trading_day: bool,
                               holiday_reason: Optional[str] = None, market_phase: str = "regular") -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR REPLACE INTO market_calendar (calendar_date, is_trading_day, holiday_reason, market_phase) "
                "VALUES (?, ?, ?, ?);", (calendar_date, int(is_trading_day), holiday_reason, market_phase),
            )
            return cur.rowcount > 0

    def is_trading_day(self, date_str: str) -> bool:
        """روز معاملاتی بر اساس تقویم دیتابیس؛ در نبود ثبت، بر اساس روزهای کاری هفته."""
        row = self.conn.execute(
            "SELECT is_trading_day FROM market_calendar WHERE calendar_date = ? LIMIT 1;", (date_str,)
        ).fetchone()
        if row:
            return bool(row["is_trading_day"])
        return datetime.strptime(date_str, "%Y-%m-%d").weekday() in settings.TRADING_DAYS

    def insert_corporate_action(self, symbol_id: int, event_date: str, event_type: str,
                                adjustment_factor: float = 1.0, cash_dividend: float = 0.0,
                                capital_increase_pct: float = 0.0, description: Optional[str] = None) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT OR REPLACE INTO corporate_actions (symbol_id, event_date, event_type, adjustment_factor,
                    cash_dividend, capital_increase_pct, description, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (symbol_id, event_date, event_type, adjustment_factor, cash_dividend, capital_increase_pct,
                 description, now_tehran_iso()),
            )
            return cur.rowcount > 0

    def log_experiment(self, experiment_id: str, strategy_name: str, symbols_tested: str, parameters_json: str,
                       metrics_json: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                       status: str = "completed", dataset_version: str = "v1.0", features_version: str = "v1.0",
                       model_version: str = "v1.0") -> Optional[int]:
        """ثبت آزمایش یا بکتست جهت رهگیری علمی و تکرارپذیری."""
        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO experiments (experiment_id, created_at, strategy_name, symbols_tested,
                        start_date, end_date, dataset_version, features_version, model_version,
                        parameters_json, metrics_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (experiment_id, now_tehran_iso(), strategy_name, symbols_tested, start_date, end_date,
                     dataset_version, features_version, model_version, parameters_json, metrics_json, status),
                )
                return cur.lastrowid
        except Exception as e:
            logger.error("خطا در ثبت آزمایش: %s", e)
            return None

    def get_experiments(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM experiments ORDER BY id DESC;").fetchall()]

    def insert_daily_features(self, symbol_id: int, date: str, features: Dict[str, Any]) -> bool:
        keys = ["return_1d", "return_5d", "volatility_20d", "atr_14d", "rsi_14d", "sma_20d", "sma_50d",
                "volume_ratio_20d", "buyer_power_ratio", "smart_money_flow"]
        with self.conn:
            cur = self.conn.execute(
                f"INSERT OR REPLACE INTO features_daily (symbol_id, date, {', '.join(keys)}, created_at) "
                f"VALUES (?, ?, {', '.join('?' * len(keys))}, ?);",
                [symbol_id, date] + [features.get(k) for k in keys] + [now_tehran_iso()],
            )
            return cur.rowcount > 0

    def table_counts(self) -> Dict[str, int]:
        """تعداد رکوردهای هر جدول (برای صفحه سلامت سیستم)."""
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
        ).fetchall()]
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0] for t in tables}


# نام مستعار جهت سازگاری با داشبورد و سایر بخش‌ها
BRSDatabase = DatabaseManager
