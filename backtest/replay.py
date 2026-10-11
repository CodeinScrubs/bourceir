# -*- coding: utf-8 -*-
"""
موتور بازپخش داده‌های تاریخی بازار (Market Data Replay Engine)
============================================================
این ماژول سابقه اسنپ‌شات‌ها و داده‌های یک روز کاری گذشته را دریافت کرده و آن را
به صورت شبیه‌سازی‌شده (گام‌به‌گام یا با سرعت افزایشی) بازپخش می‌کند، گویی بازار
هم‌اکنون در حال معامله زنده است.
این قابلیت امکان ارزیابی استراتژی‌ها، شاخص‌های تابلوخوانی و عملکرد داشبورد را
بدون نیاز به باز بودن فیزیکی بازار فراهم می‌سازد.
"""

import time
import logging
from typing import List, Dict, Any, Optional, Callable, Iterator
from datetime import datetime
import pandas as pd

from database.db import DatabaseManager

logger = logging.getLogger(__name__)


class MarketReplayEngine:
    """موتور بازپخش جریان داده‌های بازار (Market Replay)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db = DatabaseManager(db_path=db_path) if db_path else DatabaseManager()

    def get_available_replay_dates(self) -> List[str]:
        """دریافت لیست تاریخ‌هایی که اسنپ‌شات برای بازپخش دارند."""
        query = """
        SELECT DISTINCT DATE(captured_at) as replay_date
        FROM snapshots
        ORDER BY replay_date DESC;
        """
        try:
            cursor = self.db.conn.cursor()
            cursor.execute(query)
            return [row["replay_date"] for row in cursor.fetchall() if row["replay_date"]]
        except Exception as e:
            logger.error("خطا در دریافت تاریخ‌های بازپخش: %s", e)
            return []

    def load_snapshots_for_replay(
        self,
        date_str: Optional[str] = None,
        symbols: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        بارگذاری کلیه اسنپ‌شات‌های یک روز مشخص به ترتیب زمانی دقیق (Point-in-Time).
        """
        query = """
        SELECT
            sn.*,
            s.symbol_fa
        FROM snapshots sn
        JOIN symbols s ON sn.symbol_id = s.id
        WHERE 1=1
        """
        params = []
        if date_str:
            query += " AND DATE(sn.captured_at) = ?"
            params.append(date_str)
        if symbols:
            placeholders = ",".join(["?"] * len(symbols))
            query += f" AND s.symbol_fa IN ({placeholders})"
            params.extend(symbols)

        query += " ORDER BY sn.captured_at ASC, sn.id ASC;"

        df = pd.read_sql_query(query, self.db.conn, params=params)
        return df

    def replay_generator(
        self,
        date_str: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        speed_multiplier: float = 0.0  # 0.0 = بدون تاخیر (Instant)، > 0 = شبیه‌سازی زمانی با ضریب سرعت
    ) -> Iterator[Dict[str, Any]]:
        """
        تولیدکننده (Generator) جریان گام‌به‌گام تیک‌های بازار برای اتصال به استراتژی‌ها یا داشبورد.
        """
        df = self.load_snapshots_for_replay(date_str=date_str, symbols=symbols)
        if df.empty:
            logger.warning("هیچ اسنپ‌شاتی برای تاریخ %s یافت نشد.", date_str)
            return

        last_time: Optional[datetime] = None

        for _, row in df.iterrows():
            record = row.to_dict()
            captured_at_str = record.get("captured_at")

            # شبیه‌سازی فاصله زمانی بین تیک‌ها در صورت تنظیم ضریب سرعت
            if speed_multiplier > 0 and captured_at_str and last_time:
                try:
                    curr_time = pd.to_datetime(captured_at_str).to_pydatetime()
                    diff_sec = (curr_time - last_time).total_seconds()
                    if 0 < diff_sec < 120:  # تاخیرهای معقول
                        sleep_time = diff_sec / speed_multiplier
                        time.sleep(min(sleep_time, 2.0))
                    last_time = curr_time
                except Exception:
                    pass
            elif captured_at_str and not last_time:
                try:
                    last_time = pd.to_datetime(captured_at_str).to_pydatetime()
                except Exception:
                    pass

            yield record

    def run_replay(
        self,
        callback: Callable[[str, Dict[str, Any]], None],
        date_str: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        speed_multiplier: float = 0.0
    ) -> int:
        """
        اجرای فرآیند بازپخش و ارسال هر رکورد به تابع کال‌بک (Callback).
        خروجی: تعداد اسنپ‌شات‌های بازپخش‌شده.
        """
        count = 0
        for snapshot in self.replay_generator(date_str, symbols, speed_multiplier):
            symbol_fa = snapshot.get("symbol_fa", "نامشخص")
            callback(symbol_fa, snapshot)
            count += 1
        return count
