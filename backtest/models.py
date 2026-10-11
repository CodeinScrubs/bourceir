# -*- coding: utf-8 -*-
"""
مدل‌های داده‌ای برای سامانه بکتستینگ بورس ایران
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class Order:
    """مدل سفارش معاملاتی."""
    symbol_fa: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: float                       # قیمت به ریال
    date: str                          # تاریخ یا زمان ثبت سفارش
    order_id: Optional[str] = None


@dataclass
class Trade:
    """مدل معامله انجام‌شده با احتساب کارمزد بورس."""
    symbol_fa: str
    side: OrderSide
    quantity: int
    price: float                       # قیمت معامله (ریال)
    gross_value: float                 # ارزش ناخالص (ریال)
    commission: float                  # کارمزد و مالیات کسرشده (ریال)
    net_value: float                   # ارزش خالص پس از کارمزد (ریال)
    date: str
    trade_id: Optional[str] = None


@dataclass
class Position:
    """مدل نگهداری سهم (پوزیشن باز یا بسته)."""
    symbol_fa: str
    quantity: int = 0
    average_entry_price: float = 0.0
    total_cost: float = 0.0
    realized_pnl: float = 0.0
    trades: List[Trade] = field(default_factory=list)


@dataclass
class ClosedTrade:
    """مدل معامله بسته شده کامل (Round-Trip Trade) جهت محاسبه دقیق و بدون خطای Win Rate."""
    symbol_fa: str
    entry_date: str
    exit_date: str
    quantity: int
    entry_price: float                 # میانگین قیمت ورود
    exit_price: float                  # قیمت خروج
    gross_pnl: float                   # سود ناخالص (ریال)
    commissions: float                 # مجموع کارمزدهای خرید و فروش (ریال)
    net_pnl: float                     # سود یا زیان خالص نهایی (ریال)
    return_pct: float                  # درصد بازدهی خالص معامله
    is_winner: bool                    # آیا معامله سودده بود؟


@dataclass
class PerformanceMetrics:
    """شاخص‌های آماری و عملکرد پرتفوی در بکتست."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0              # درصد معاملات برنده (۰ تا ۱۰۰)
    gross_profit: float = 0.0          # سود ناخالص کل (ریال)
    gross_loss: float = 0.0            # زیان ناخالص کل (ریال)
    net_pnl: float = 0.0               # سود یا زیان خالص بعد از کارمزد (ریال)
    profit_factor: float = 0.0         # نسبت سود به زیان
    max_drawdown_pct: float = 0.0      # حداکثر افت سرمایه (درصد)
    total_commissions_paid: float = 0.0# مجموع کارمزد و مالیات پرداختی (ریال)
    sharpe_ratio: float = 0.0          # نسبت شارپ سالانه
    initial_capital: float = 0.0       # سرمایه اولیه
    final_equity: float = 0.0          # ارزش نهایی پرتفوی
    total_return_pct: float = 0.0      # درصد کل بازدهی سرمایه
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
