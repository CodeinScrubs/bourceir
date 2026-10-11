# -*- coding: utf-8 -*-
"""
ماژول مدیریت جامعه نمادها (Market Universe Manager)
وظیفه شناسایی، پالایش و نگهداری فهرست تمام سهام عادی بازار بورس و فرابورس ایران
با حذف صندوق‌ها (ETF)، اوراق بدهی (اخزا، سلف، گام)، حق‌تقدم‌ها و ابزارهای مشتقه.
"""

import logging
from typing import List, Dict, Any, Optional
import pandas as pd

from config import settings
from database.db import DatabaseManager

logger = logging.getLogger(__name__)


class UniverseManager:
    """مدیریت دایره نمادهای تحت پوشش بازار سرمایه ایران."""

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()

    @staticmethod
    def is_common_stock(symbol_fa: str, isin_code: Optional[str] = None) -> bool:
        """
        تشخیص اینکه آیا یک نماد، سهم عادی بورس یا فرابورس است یا خیر.
        - حذف حق‌تقدم‌ها (حروف پایانی «ح»)
        - حذف تسهیلات مسکن (تسه، تملی)
        - حذف اوراق بدهی و اسناد خزانه (اخزا، اراد، گام، سلف)
        - حذف صندوق‌های سرمایه‌گذاری (دارا، پالایش، اهرم، توان، فراز، کاریس، سرو، ...)
        - حذف اختیار معامله‌ها (ض، ط)
        """
        sym = symbol_fa.strip()

        # حق تقدم‌ها معمولاً به «ح» ختم می‌شوند (مانند خزامیاح، وبملتح)
        if len(sym) > 3 and sym.endswith("ح"):
            return False

        # اوراق بدهی و دولتی
        excluded_prefixes = ["اخزا", "اراد", "اشاد", "اجاد", "سلف", "گام", "تسه", "تملی", "صکوک"]
        for prefix in excluded_prefixes:
            if sym.startswith(prefix):
                return False

        # اختیار معامله (Options)
        if sym.startswith("ض") or sym.startswith("ط"):
            return False

        # صندوق‌ها (برخی پیشوندها یا کلیدواژه‌ها)
        excluded_symbols = [
            "دارا یکم", "پالایش", "اهرم", "توان", "فراز", "کاریس", "سرو",
            "جهش", "موج", "شتاب", "عیار", "طلا", "گوهر", "کهربا", "زرفام",
            "تابان", "افق", "اعتماد", "امین", "یاقوت", "فیروزه"
        ]
        if sym in excluded_symbols:
            return False

        # فیلتر کد بین‌المللی ISIN در صورت وجود
        if isin_code:
            code = isin_code.strip()
            # سهام عادی معمولاً با IRO1 (بورس) یا IRO3 / IRO4 (فرابورس) شروع می‌شوند
            # اوراق مشتقه با IRO9 یا IRK هستند
            if not code.startswith("IRO"):
                return False

        return True

    def sync_universe_from_provider(self, symbols_data: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        همگام‌سازی فهرست نمادهای بازار با پایگاه داده.
        """
        added = 0
        skipped = 0

        for item in symbols_data:
            symbol_fa = item.get("symbol_fa", "").strip()
            tse_code = item.get("tse_code", "")
            name_fa = item.get("name_fa", symbol_fa)
            market = item.get("market", "bourse")

            if not symbol_fa or not tse_code:
                skipped += 1
                continue

            if not self.is_common_stock(symbol_fa, isin_code=tse_code):
                skipped += 1
                continue

            sym_id = self.db.insert_symbol(
                tse_code=tse_code,
                symbol_fa=symbol_fa,
                name_fa=name_fa,
                market=market,
                instrument_type="stock"
            )
            if sym_id:
                added += 1

        logger.info("همگام‌سازی نمادها انجام شد: %d نماد سهام عادی ثبت/به‌روز شد، %d نماد غیراستاندارد فیلتر شد.", added, skipped)
        return {"added_or_updated": added, "skipped": skipped}

    def get_tracked_symbols(self) -> List[str]:
        """دریافت لیست نمادهای فعال سهام در دیتابیس."""
        symbols = self.db.get_all_symbols()
        return [s["symbol_fa"] for s in symbols]
