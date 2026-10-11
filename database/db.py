# -*- coding: utf-8 -*-
"""
ماژول مدیریت و اتصال به پایگاه داده SQLite
طراحی‌شده با متدهای ساده و خوانا برای استفاده در BRS Data Engine
"""

import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, List, Dict, Any

from config import settings
from database.models import (
    CREATE_SYMBOLS_TABLE,
    CREATE_DAILY_PRICES_TABLE,
    CREATE_SNAPSHOTS_TABLE,
    CREATE_COLLECTION_LOG_TABLE,
    CREATE_DATA_SOURCES_TABLE,
    CREATE_CORPORATE_ACTIONS_TABLE,
    CREATE_MARKET_CALENDAR_TABLE,
    CREATE_INTRADAY_TRADES_TABLE,
    CREATE_DATA_QUALITY_TABLE,
    CREATE_EXPERIMENTS_TABLE,
    CREATE_FEATURES_DAILY_TABLE,
    CREATE_FEATURES_INTRADAY_TABLE,
)

logger = logging.getLogger(__name__)


def get_current_tehran_time() -> str:
    """دریافت زمان کنونی با احتساب منطقه زمانی تهران (Asia/Tehran) به صورت ایزو."""
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        # در ویندوز اگر پکیج tzdata نصب نباشد، از افست ثابت رسمی تهران UTC+03:30 استفاده می‌شود
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")
    return datetime.now(tz).isoformat()


class DatabaseManager:
    """
    کلاس مدیریت پایگاه داده SQLite برای ذخیره و بازیابی اطلاعات بازار بورس.
    پشتیبانی از پروتکل Context Manager با استفاده از دستور with.
    """

    def __init__(self, db_path: Optional[Path | str] = None):
        """
        مقداردهی اولیه مسیر فایل دیتابیس و اتصال اولیه.
        """
        if db_path is None:
            self.db_path = Path(settings.DB_PATH)
        else:
            self.db_path = Path(db_path)

        # اطمینان از ایجاد پوشه والد دیتابیس
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn: Optional[sqlite3.Connection] = None
        self._connect()
        self.init_db()

    def _connect(self) -> None:
        """برقراری اتصال امن با دیتابیس SQLite."""
        try:
            self.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False
            )
            self.conn.row_factory = sqlite3.Row
            logger.info("اتصال به پایگاه داده با موفقیت برقرار شد: %s", self.db_path)
        except Exception as e:
            logger.error("خطا در برقراری اتصال به پایگاه داده: %s", e)
            raise

    def __enter__(self):
        """ورود به بافت کانتکست منیجر."""
        if self.conn is None:
            self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """خروج از کانتکست منیجر و بستن ارتباط."""
        self.close()

    def init_db(self) -> None:
        """
        ایجاد تمام جداول مورد نیاز بر اساس اسکیماهای تعریف‌شده در models.py.
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(CREATE_SYMBOLS_TABLE)
                cursor.execute(CREATE_DAILY_PRICES_TABLE)
                cursor.execute(CREATE_SNAPSHOTS_TABLE)
                cursor.execute(CREATE_COLLECTION_LOG_TABLE)
                cursor.execute(CREATE_DATA_SOURCES_TABLE)
                cursor.execute(CREATE_CORPORATE_ACTIONS_TABLE)
                cursor.execute(CREATE_MARKET_CALENDAR_TABLE)
                cursor.execute(CREATE_INTRADAY_TRADES_TABLE)
                cursor.execute(CREATE_DATA_QUALITY_TABLE)
                cursor.execute(CREATE_EXPERIMENTS_TABLE)
                cursor.execute(CREATE_FEATURES_DAILY_TABLE)
                cursor.execute(CREATE_FEATURES_INTRADAY_TABLE)
            self._run_column_migrations()
            logger.info("ساختار کلیه جداول و مایگریشن‌های پایگاه داده با موفقیت آماده‌سازی شد.")
        except Exception as e:
            logger.error("خطا در ایجاد جداول پایگاه داده: %s", e)
            raise

    def _run_column_migrations(self) -> None:
        """اجرای مایگریشن‌های نرم برای افزودن ستون‌های جدید به جداول از پیش موجود."""
        migration_columns = [
            ("symbols", "status", "TEXT DEFAULT 'active'"),
            ("symbols", "listed_date", "TEXT"),
            ("symbols", "delisted_date", "TEXT"),
            ("symbols", "base_volume", "INTEGER DEFAULT 1"),
            ("daily_prices", "open_raw", "REAL"),
            ("daily_prices", "high_raw", "REAL"),
            ("daily_prices", "low_raw", "REAL"),
            ("daily_prices", "close_raw", "REAL"),
            ("daily_prices", "adjustment_factor", "REAL DEFAULT 1.0"),
            ("snapshots", "base_volume", "INTEGER DEFAULT 1"),
            ("snapshots", "bid_price_1", "REAL"),
            ("snapshots", "bid_vol_1", "INTEGER"),
            ("snapshots", "ask_price_1", "REAL"),
            ("snapshots", "ask_vol_1", "INTEGER"),
            ("experiments", "dataset_version", "TEXT DEFAULT 'v1.0'"),
            ("experiments", "features_version", "TEXT DEFAULT 'v1.0'"),
            ("experiments", "model_version", "TEXT DEFAULT 'v1.0'"),
        ]
        with self.conn:
            cursor = self.conn.cursor()
            for table, col, col_def in migration_columns:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")
                except sqlite3.OperationalError:
                    # ستون از قبل وجود دارد؛ نادیده گرفته می‌شود
                    pass

            # بک‌فیل خودکار رکوردهای قبلی جهت اطمینان از مقدار داشتن ستون‌های Point-in-Time
            try:
                cursor.execute("""
                UPDATE daily_prices 
                SET open_raw = open, high_raw = high, low_raw = low, close_raw = close,
                    adjustment_factor = CASE WHEN close > 0 THEN adj_close / close ELSE 1.0 END
                WHERE open_raw IS NULL;
                """)
            except Exception as backfill_err:
                logger.debug("عدم نیاز به بک‌فیل یا خطا: %s", backfill_err)

    def insert_symbol(
        self,
        tse_code: str,
        symbol_fa: str,
        name_fa: Optional[str] = None,
        market: Optional[str] = 'bourse',
        instrument_type: Optional[str] = 'stock'
    ) -> Optional[int]:
        """
        افزودن نماد جدید یا دریافت شناسه آن (INSERT OR IGNORE برای جلوگیری از تکرار).
        """
        now = get_current_tehran_time()
        query = """
        INSERT OR IGNORE INTO symbols (tse_code, symbol_fa, name_fa, market, instrument_type, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(query, (tse_code, symbol_fa, name_fa, market, instrument_type, now, now))
                if cursor.rowcount > 0:
                    return cursor.lastrowid

            # در صورتی که نماد از قبل ثبت شده بود، شناسه موجود بازگردانده می‌شود
            return self.get_symbol_id(symbol_fa)
        except Exception as e:
            logger.error("خطا در افزودن نماد %s: %s", symbol_fa, e)
            return None

    def get_symbol_id(self, symbol_fa: str) -> Optional[int]:
        """یافتن شناسه عددی نماد (id) بر اساس نام فارسی نماد."""
        query = "SELECT id FROM symbols WHERE symbol_fa = ? LIMIT 1;"
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, (symbol_fa,))
            row = cursor.fetchone()
            if row:
                return row["id"]
            return None
        except Exception as e:
            logger.error("خطا در جستجوی شناسه نماد %s: %s", symbol_fa, e)
            return None

    def get_all_symbols(self) -> List[Dict[str, Any]]:
        """دریافت لیست تمام نمادهای فعال در سامانه."""
        query = "SELECT * FROM symbols WHERE is_active = 1 ORDER BY symbol_fa ASC;"
        try:
            cursor = self.conn.cursor()
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("خطا در دریافت لیست نمادها: %s", e)
            return []

    def insert_daily_price(
        self,
        symbol_id: int,
        date: str,
        open: float,
        high: float,
        low: float,
        close: float,
        adj_close: float,
        volume: int,
        value: float,
        trade_count: int,
        source: str = 'pytse',
        price_unit: str = settings.PRICE_UNIT,
        open_raw: Optional[float] = None,
        high_raw: Optional[float] = None,
        low_raw: Optional[float] = None,
        close_raw: Optional[float] = None,
        adjustment_factor: Optional[float] = None,
    ) -> bool:
        """
        ثبت قیمت روزانه معامله با ذخیره قطعی مقادیر خام (Point-in-Time) و ضریب تعدیل.
        از درج رکوردهای تکراری بر اساس (symbol_id, date, source) جلوگیری می‌شود.
        """
        now = get_current_tehran_time()

        # تضمین پر بودن داده‌های خام Point-in-Time
        o_raw = open_raw if open_raw is not None else open
        h_raw = high_raw if high_raw is not None else high
        l_raw = low_raw if low_raw is not None else low
        c_raw = close_raw if close_raw is not None else close

        if adjustment_factor is not None:
            adj_fac = adjustment_factor
        elif close and close > 0 and adj_close is not None:
            adj_fac = round(adj_close / close, 6)
        else:
            adj_fac = 1.0

        query = """
        INSERT OR IGNORE INTO daily_prices (
            symbol_id, date, open, high, low, close, adj_close,
            volume, value, trade_count, price_unit, source, captured_at,
            open_raw, high_raw, low_raw, close_raw, adjustment_factor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    query,
                    (
                        symbol_id, date, open, high, low, close, adj_close,
                        volume, value, trade_count, price_unit, source, now,
                        o_raw, h_raw, l_raw, c_raw, adj_fac
                    )
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت قیمت روزانه برای symbol_id=%s در تاریخ %s: %s", symbol_id, date, e)
            return False

    def insert_snapshot(self, symbol_id: int, data_dict: Dict[str, Any]) -> bool:
        """
        ثبت اسنپ‌شات لحظه‌ای بازار با جزئیات صفوف خرید/فروش و حقیقی/حقوقی.
        """
        now = get_current_tehran_time()
        captured_at = data_dict.get('captured_at', now)
        source = data_dict.get('source', 'pytse')
        price_unit = data_dict.get('price_unit', settings.PRICE_UNIT)

        query = """
        INSERT OR IGNORE INTO snapshots (
            symbol_id, captured_at, last_price, close_price, open_price,
            high_price, low_price, volume, value, trade_count,
            buy_queue_volume, sell_queue_volume,
            real_buy_count, real_sell_count, legal_buy_count, legal_sell_count,
            real_buy_volume, real_sell_volume, legal_buy_volume, legal_sell_volume,
            price_unit, source, provider_timestamp
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?
        );
        """
        params = (
            symbol_id,
            captured_at,
            data_dict.get('last_price'),
            data_dict.get('close_price'),
            data_dict.get('open_price'),
            data_dict.get('high_price'),
            data_dict.get('low_price'),
            data_dict.get('volume'),
            data_dict.get('value'),
            data_dict.get('trade_count'),
            data_dict.get('buy_queue_volume'),
            data_dict.get('sell_queue_volume'),
            data_dict.get('real_buy_count'),
            data_dict.get('real_sell_count'),
            data_dict.get('legal_buy_count'),
            data_dict.get('legal_sell_count'),
            data_dict.get('real_buy_volume'),
            data_dict.get('real_sell_volume'),
            data_dict.get('legal_buy_volume'),
            data_dict.get('legal_sell_volume'),
            price_unit,
            source,
            data_dict.get('provider_timestamp')
        )
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت اسنپ‌شات برای symbol_id=%s: %s", symbol_id, e)
            return False

    def get_latest_prices(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        دریافت آخرین قیمت‌های ثبت شده همراه با اطلاعات و نام فارسی نماد.
        """
        query = """
        SELECT 
            dp.*,
            s.symbol_fa,
            s.name_fa,
            s.market
        FROM daily_prices dp
        JOIN symbols s ON dp.symbol_id = s.id
        ORDER BY dp.date DESC, dp.captured_at DESC
        LIMIT ?;
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("خطا در دریافت آخرین قیمت‌ها: %s", e)
            return []

    def get_collection_stats(self) -> List[Dict[str, Any]]:
        """دریافت خلاصه گزارش وضعیت لاگ‌های گردآوری داده."""
        query = """
        SELECT * FROM collection_log
        ORDER BY started_at DESC
        LIMIT 20;
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("خطا در دریافت آمار گردآوری داده: %s", e)
            return []

    def log_collection(
        self,
        started_at: str,
        finished_at: str,
        collection_type: str,
        symbols_requested: int,
        symbols_succeeded: int,
        symbols_failed: int,
        errors: Optional[str] = None,
        status: str = 'success'
    ) -> Optional[int]:
        """ثبت گزارش فرآیند دریافت اطلاعات در جدول لاگ."""
        query = """
        INSERT INTO collection_log (
            started_at, finished_at, collection_type,
            symbols_requested, symbols_succeeded, symbols_failed,
            errors, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    query,
                    (started_at, finished_at, collection_type, symbols_requested, symbols_succeeded, symbols_failed, errors, status)
                )
                return cursor.lastrowid
        except Exception as e:
            logger.error("خطا در ثبت لاگ گردآوری: %s", e)
            return None

    def close(self) -> None:
        """بستن ایمن اتصال پایگاه داده."""
        if self.conn:
            try:
                self.conn.close()
                self.conn = None
                logger.info("اتصال به پایگاه داده بسته شد.")
            except Exception as e:
                logger.error("خطا در بستن اتصال پایگاه داده: %s", e)

    def log_experiment(
        self,
        experiment_id: str,
        strategy_name: str,
        symbols_tested: str,
        parameters_json: str,
        metrics_json: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: str = "completed"
    ) -> Optional[int]:
        """ثبت آزمایش یا بکتست جهت رهگیری علمی استراتژی‌ها."""
        query = """
        INSERT OR REPLACE INTO experiments (
            experiment_id, created_at, strategy_name, symbols_tested,
            start_date, end_date, parameters_json, metrics_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    query,
                    (
                        experiment_id,
                        get_current_tehran_time(),
                        strategy_name,
                        symbols_tested,
                        start_date,
                        end_date,
                        parameters_json,
                        metrics_json,
                        status
                    )
                )
                return cursor.lastrowid
        except Exception as e:
            logger.error("خطا در ثبت آزمایش: %s", e)
            return None

    def get_experiments(self) -> List[Dict[str, Any]]:
        """دریافت سوابق کلیه آزمایش‌ها و نتایج بکتست‌ها."""
        query = "SELECT * FROM experiments ORDER BY id DESC;"
        try:
            cursor = self.conn.cursor()
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("خطا در خواندن آزمایش‌ها: %s", e)
            return []

    def insert_corporate_action(
        self,
        symbol_id: int,
        event_date: str,
        event_type: str,
        adjustment_factor: float = 1.0,
        cash_dividend: float = 0.0,
        capital_increase_pct: float = 0.0,
        description: Optional[str] = None
    ) -> bool:
        """ثبت رویداد شرکتی و ضریب تعدیل قیمت."""
        query = """
        INSERT OR REPLACE INTO corporate_actions (
            symbol_id, event_date, event_type, adjustment_factor,
            cash_dividend, capital_increase_pct, description, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    query,
                    (symbol_id, event_date, event_type, adjustment_factor, cash_dividend, capital_increase_pct, description, get_current_tehran_time())
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت رویداد شرکتی: %s", e)
            return False

    def insert_market_calendar(
        self,
        calendar_date: str,
        is_trading_day: bool,
        holiday_reason: Optional[str] = None,
        market_phase: str = 'regular'
    ) -> bool:
        """ثبت وضعیت تقویم بازار."""
        query = """
        INSERT OR REPLACE INTO market_calendar (calendar_date, is_trading_day, holiday_reason, market_phase)
        VALUES (?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(query, (calendar_date, int(is_trading_day), holiday_reason, market_phase))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت تقویم بازار: %s", e)
            return False

    def is_trading_day(self, date_str: str) -> bool:
        """بررسی باز بودن بازار در تاریخ مشخص بر اساس تقویم دیتابیس."""
        query = "SELECT is_trading_day FROM market_calendar WHERE calendar_date = ? LIMIT 1;"
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, (date_str,))
            row = cursor.fetchone()
            if row:
                return bool(row["is_trading_day"])
            # در صورت عدم ثبت در جدول تقویم، از روزهای هفته پیش‌فرض استفاده می‌شود
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.weekday() in settings.TRADING_DAYS
        except Exception:
            return True

    def insert_quality_metric(
        self,
        symbol_id: int,
        trade_date: str,
        completeness_score: float = 100.0,
        price_anomaly_flag: bool = False,
        volume_anomaly_flag: bool = False,
        quality_score: float = 100.0,
        notes: Optional[str] = None
    ) -> bool:
        """ثبت ارزیابی کیفیت و سلامت داده نماد."""
        query = """
        INSERT OR REPLACE INTO data_quality_metrics (
            symbol_id, trade_date, completeness_score, price_anomaly_flag,
            volume_anomaly_flag, quality_score, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    query,
                    (symbol_id, trade_date, completeness_score, int(price_anomaly_flag), int(volume_anomaly_flag), quality_score, notes)
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت متریک کیفیت داده: %s", e)
            return False

    def insert_daily_features(self, symbol_id: int, date: str, features: Dict[str, Any]) -> bool:
        """ذخیره فاکتورهای محاسبه‌شده روزانه در Feature Store."""
        query = """
        INSERT OR REPLACE INTO features_daily (
            symbol_id, date, return_1d, return_5d, volatility_20d,
            atr_14d, rsi_14d, sma_20d, sma_50d, volume_ratio_20d,
            buyer_power_ratio, smart_money_flow, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    query,
                    (
                        symbol_id,
                        date,
                        features.get("return_1d"),
                        features.get("return_5d"),
                        features.get("volatility_20d"),
                        features.get("atr_14d"),
                        features.get("rsi_14d"),
                        features.get("sma_20d"),
                        features.get("sma_50d"),
                        features.get("volume_ratio_20d"),
                        features.get("buyer_power_ratio"),
                        features.get("smart_money_flow"),
                        get_current_tehran_time()
                    )
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("خطا در ثبت فیچرهای روزانه: %s", e)
            return False

    def get_daily_prices_df(
        self,
        symbol_fa: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        raw_only: bool = True
    ):
        """
        دریافت سابقه قیمت‌های روزانه به صورت پانداز دیتافریم با تفکیک قطعی داده خام از تعدیل‌شده.
        """
        import pandas as pd
        sym_id = self.get_symbol_id(symbol_fa)
        if not sym_id:
            return pd.DataFrame()

        query = """
        SELECT
            date,
            open_raw, high_raw, low_raw, close_raw,
            open, high, low, close, adj_close, adjustment_factor,
            volume, value, trade_count
        FROM daily_prices
        WHERE symbol_id = ?
        """
        params = [sym_id]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date ASC;"

        df = pd.read_sql_query(query, self.conn, params=params)
        return df


# نام مستعار جهت سازگاری با داشبورد و سایر بخش‌ها
BRSDatabase = DatabaseManager
