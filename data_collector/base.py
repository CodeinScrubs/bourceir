# -*- coding: utf-8 -*-
"""
لایه انتزاعی و استاندارد تامین‌کنندگان داده (Base Data Provider Interface)
این ماژول تضمین می‌کند که هسته BRS به هیچ کتابخانه خاصی (مثل pytse-client) وابسته نباشد
و بتوان در آینده منابع داده دیگر (مانند FinPy-TSE، وب‌سرویس مستقیم TSETMC یا وب‌سوکت کارگزاری)
را بدون دستکاری سایر بخش‌های پروژه جایگزین یا اضافه کرد.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
import pandas as pd


class BaseDataProvider(ABC):
    """کلاس پایه انتزاعی برای تامین‌کنندگان داده بازار بورس."""

    @abstractmethod
    def test_connection(self, test_symbol: str = "خودرو") -> bool:
        """بررسی برقراری ارتباط با منبع داده."""
        pass

    @abstractmethod
    def get_symbol_history(
        self,
        symbol_fa: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """
        دریافت سوابق روزانه قیمت‌ها (OHLCV).
        باید یک دیتافریم استاندارد با ستون‌های زیر بازگرداند:
        ['date', 'open', 'high', 'low', 'close', 'adj_close', 'volume', 'value', 'trade_count']
        """
        pass

    @abstractmethod
    def get_live_snapshot(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        """
        دریافت اسنپ‌شات لحظه‌ای تابلو برای یک نماد شامل:
        - قیمت آخرین معامله و پایانی
        - صف خرید و فروش (مظنه برتر و حجم صف)
        - حجم و سرانه معاملات حقیقی و حقوقی
        """
        pass

    @abstractmethod
    def get_symbols_list(self) -> List[Dict[str, Any]]:
        """
        دریافت فهرست تمام نمادهای فعال بازار به همراه متادیتا:
        [{'symbol_fa': '...', 'name_fa': '...', 'tse_code': '...', 'market': '...', 'industry': '...'}]
        """
        pass

    @abstractmethod
    def get_intraday_trades(self, symbol_fa: str, date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """
        دریافت ریزمعاملات یا کندل‌های درون‌روزی (برای استراتژی‌های نوسان‌گیری روزانه).
        """
        pass
