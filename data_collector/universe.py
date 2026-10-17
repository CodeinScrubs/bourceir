# -*- coding: utf-8 -*-
"""
ماژول مدیریت جامعه نمادها (Market Universe Manager)

دامنه پروژه BRS: فقط سهام عادی بورس و فرابورس.
تشخیص اصلی بر اساس کد نوع ابزار رسمی TSETMC (yVal) انجام می‌شود:
    300 = سهام بورس، 303 = سهام فرابورس  (309 بازار پایه و 313 بازار SME به‌صورت اختیاری)
بنابراین صندوق‌ها، اوراق بدهی، حق‌تقدم‌ها، اختیار معامله‌ها و تابلوهای بلوکی خودبه‌خود حذف می‌شوند.
"""

import logging
from datetime import timedelta
from typing import Dict, List, Optional

from config import settings
from core.text import normalize_symbol
from core.timeutils import now_tehran
from data_collector.base import BaseDataProvider
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

class UniverseManager:
    """مدیریت دایره نمادهای تحت پوشش (سهام بورس و فرابورس)."""

    def __init__(self, db: Optional[DatabaseManager] = None, provider: Optional[BaseDataProvider] = None):
        self.db = db or DatabaseManager()
        self.provider = provider

    @staticmethod
    def is_common_stock(symbol_fa: str, isin_code: Optional[str] = None) -> bool:
        """
        تشخیص تقریبی سهم عادی فقط از روی نام/ISIN (برای وقتی که کد yVal در دسترس نیست).
        روش اصلی و دقیق، فیلتر yVal در TsetmcProvider.is_in_scope است.
        """
        sym = normalize_symbol(symbol_fa)
        if not sym:
            return False
        if sym[-1].isdigit():  # تابلوی بلوکی/عمده یا اوراق
            return False
        if len(sym) > 3 and sym.endswith("ح"):  # حق تقدم
            return False
        if sym.startswith(("اخزا", "اراد", "اشاد", "اجاد", "سلف", "گام", "تسه", "تملی", "صکوک")):
            return False
        if sym.startswith(("ض", "ط")) and any(ch.isdigit() for ch in sym):  # اختیار معامله
            return False
        funds = {
            "دارا یکم", "پالایش", "اهرم", "توان", "فراز", "کاریس", "سرو", "جهش", "موج", "شتاب", "عیار",
            "طلا", "گوهر", "کهربا", "زرفام", "تابان", "افق", "اعتماد", "امین", "یاقوت", "فیروزه",
        }
        if sym in {normalize_symbol(f) for f in funds}:
            return False
        if isin_code and not isin_code.strip().startswith("IRO"):
            return False
        return True

    def sync_universe(self) -> Dict[str, int]:
        """
        دریافت فهرست کامل سهام داخل دامنه از منبع داده و ثبت/به‌روزرسانی در دیتابیس.
        حضور، مجوز به‌روزرسانی مثبت است؛ غیبت در دیده‌بان، حذف یا توقف قطعی نیست.
        حتی پاسخ هم‌اندازه پاسخ قبلی، کامل بودن عضویت را اثبات نمی‌کند.
        """
        if self.provider is None:
            raise RuntimeError("برای همگام‌سازی جامعه نمادها، provider لازم است.")
        known = {s["ins_code"]: s for s in self.db.get_all_symbols(include_inactive=True) if s.get("ins_code")}
        universe = self.provider.get_stock_universe(known=known)
        if not universe:
            logger.error("فهرست نمادها از منبع داده دریافت نشد؛ دیتابیس دست‌نخورده باقی ماند.")
            return {"received": 0, "saved": 0, "failed": 0, "deactivated": 0}
        active_before = self.db.get_universe_symbols()
        saved = failed = 0
        for meta in universe:
            if self.db.upsert_symbol(meta):
                saved += 1
            else:
                failed += 1

        received_codes = {m["ins_code"] for m in universe}
        missing = sorted(s['symbol_fa'] for s in active_before if s.get('ins_code') not in received_codes)
        if missing:
            logger.warning('%d نماد فعال محلی در پاسخ نیستند؛ وضعیت قبلی حفظ شد و علت غیبت نامعلوم است.', len(missing))
        logger.info('همگام‌سازی جامعه سهام: %d ثبت/به‌روز، %d ناموفق، %d غیبت نامعلوم.', saved, failed, len(missing))
        return dict(received=len(universe), saved=saved, failed=failed, deactivated=0,
                    missing_from_response=len(missing), missing_symbols=missing,
                    absence_reason_verified=False)

    def get_tracked_symbols(self) -> List[str]:
        """نمادهای داخل دامنه که در دیتابیس ثبت شده‌اند."""
        return [s["symbol_fa"] for s in self.db.get_universe_symbols()]

    def get_top_liquid_symbols(self, limit: int = 30, lookback_days: int = 30) -> List[str]:
        """رتبه‌بندی نقدشوندگی بر اساس میانگین ارزش معاملات روزانه در بازه اخیر."""
        cutoff = (now_tehran() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        rows = self.db.conn.execute(
            """
            SELECT s.symbol_fa, AVG(dp.value) AS avg_value
            FROM daily_prices dp JOIN symbols s ON dp.symbol_id = s.id
            WHERE s.is_active = 1 AND dp.date >= ? AND dp.source = 'tsetmc'
            GROUP BY s.id
            ORDER BY avg_value DESC
            LIMIT ?;
            """, (cutoff, limit),
        ).fetchall()
        if rows:
            return [r["symbol_fa"] for r in rows]
        logger.info("داده کافی برای رتبه‌بندی نقدشوندگی نیست؛ نمادهای پیش‌فرض برگردانده شد.")
        return list(settings.TEST_SYMBOLS)
