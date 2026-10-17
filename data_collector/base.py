# -*- coding: utf-8 -*-
"""
لایه انتزاعی و استاندارد تامین‌کنندگان داده (Base Data Provider Interface)

هسته BRS به هیچ منبع خاصی وابسته نیست. هر منبع داده جدید (وب‌سرویس TSETMC، FinPy، کارگزاری و ...)
فقط باید این قرارداد را پیاده‌سازی کند. شناسه نماد در همه متدها «ins_code» (شناسه پایدار TSETMC) است.

قرارداد ستون‌های تاریخچه روزانه (همگی خام، تعدیل‌نشده و به ریال):
    date             YYYY-MM-DD
    open_raw         اولین قیمت معامله
    high_raw         بیشترین قیمت
    low_raw          کمترین قیمت
    close_raw        آخرین قیمت معامله
    close_final_raw  قیمت پایانی رسمی
    ref_price_raw    قیمت مرجع روز (قیمت پایانی دیروز، تعدیل‌شده در روز اقدام شرکتی)
    volume, value, trade_count
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import pandas as pd

DAILY_COLUMNS = [
    "date", "open_raw", "high_raw", "low_raw", "close_raw", "close_final_raw", "ref_price_raw",
    "volume", "value", "trade_count",
]

CLIENT_TYPE_HISTORY_COLUMNS = [
    "date",
    "buy_i_volume", "buy_n_volume", "buy_i_value", "buy_n_value", "buy_i_count", "buy_n_count",
    "sell_i_volume", "sell_n_volume", "sell_i_value", "sell_n_value", "sell_i_count", "sell_n_count",
]


class BaseDataProvider(ABC):
    """کلاس پایه انتزاعی برای تامین‌کنندگان داده بازار بورس."""

    name: str = "base"

    @abstractmethod
    def test_connection(self) -> bool:
        """بررسی برقراری ارتباط با منبع داده."""

    @abstractmethod
    def search_symbol(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        """یافتن متادیتای یک نماد (شامل ins_code) از روی نام فارسی."""

    @abstractmethod
    def get_stock_universe(self, known: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """
        فهرست سهام داخل دامنه پروژه همراه با متادیتا:
        [{'symbol_fa', 'name_fa', 'ins_code', 'tse_code', 'market', 'board_code',
          'instrument_type_code', 'flow', 'sector_code', 'shares_outstanding', 'base_volume'}]
        known: متادیتای نمادهای از قبل شناخته‌شده به تفکیک ins_code (برای کاهش درخواست‌ها).
        خروجی خالی یعنی خطا؛ فهرست ناقص نباید برگردانده شود.
        """

    @abstractmethod
    def get_symbol_history(self, ins_code: str, last_n: int = 0) -> Optional[pd.DataFrame]:
        """تاریخچه روزانه با ستون‌های DAILY_COLUMNS (last_n=0 یعنی کل تاریخچه). در خطا: None."""

    @abstractmethod
    def get_market_snapshots(self) -> Optional[List[Dict[str, Any]]]:
        """اسنپ‌شات لحظه‌ای کل بازار سهام (قیمت‌ها، دامنه نوسان، ۵ مظنه، حقیقی/حقوقی). در خطا: None."""

    def get_client_types_history(self, ins_code: str, dates: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
        """
        تاریخچه روزانه معاملات حقیقی/حقوقی با ستون‌های CLIENT_TYPE_HISTORY_COLUMNS.
        dates=None یعنی کل تاریخچه؛ لیست تاریخ‌ها یعنی فقط همان روزها (لیست خالی → دیتافریم خالی).
        """
        raise NotImplementedError

    def get_intraday_trades(self, ins_code: str) -> Optional[pd.DataFrame]:
        """ریزمعاملات روز جاری (زمان، قیمت، حجم)."""
        raise NotImplementedError

    def get_trade_session(self, ins_code: str, trade_date: str) -> Optional[Dict[str, Any]]:
        """پاسخ خام ریزمعاملات یک تاریخ مشخص با نشانی و زمان دریافت؛ کامل بودن هنوز اثبات نشده."""
        raise NotImplementedError

    def get_market_observation(self) -> Optional[Dict[str, Any]]:
        """دیده‌بان با هویت ابزار و خام محفوظ؛ تاریخ نامعلوم از زمان دریافت ساخته نمی‌شود."""
        raise NotImplementedError

    def get_closing_observation(self, ins_code: str) -> Optional[Dict[str, Any]]:
        """آخرین کندل هر ابزار، با تاریخ مستقل خود منبع و پاسخ محفوظ."""
        raise NotImplementedError

    def get_market_overview(self, flow: int) -> Optional[Dict[str, Any]]:
        """شاخص‌های رسمی یک بازار؛ دامنه آن با جمع سهام عادی یکسان فرض نمی‌شود."""
        raise NotImplementedError

    def get_sector_names(self) -> Optional[Dict[str, str]]:
        """نام صنایع؛ نبود نام، مجوز حدس صنعت نیست."""
        raise NotImplementedError
