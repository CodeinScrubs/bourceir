# -*- coding: utf-8 -*-
"""
مدل‌های داده‌ای برای سامانه بکتستینگ بورس ایران
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


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
    average_entry_price: float = 0.0   # بهای تمام‌شده هر سهم شامل کارمزد خرید
    total_cost: float = 0.0
    realized_pnl: float = 0.0
    opened_at: Optional[str] = None    # تاریخ اولین خرید از آخرین باری که پوزیشن صفر بوده
    trades: List[Trade] = field(default_factory=list)


@dataclass
class ClosedTrade:
    """معامله رفت و برگشت کامل (Round-Trip) برای محاسبه دقیق Win Rate."""
    symbol_fa: str
    entry_date: str
    exit_date: str
    quantity: int
    entry_price: float                 # میانگین قیمت ورود (بدون کارمزد)
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
    gross_profit: float = 0.0          # مجموع سود معاملات برنده (ریال، خالص از کارمزد)
    gross_loss: float = 0.0            # مجموع زیان معاملات بازنده (ریال، خالص از کارمزد)
    net_pnl: float = 0.0               # تغییر ارزش کل پرتفوی = ارزش نهایی − سرمایه اولیه (ریال)
    profit_factor: Optional[float] = None  # نسبت سود به زیان (بدون معامله زیان‌ده: تعریف‌نشده، None)
    expectancy_pct: float = 0.0        # امید ریاضی: میانگین بازده خالص هر معامله (درصد)
    max_drawdown_pct: float = 0.0      # حداکثر افت سرمایه (درصد)
    total_commissions_paid: float = 0.0
    sharpe_ratio: float = 0.0          # نسبت شارپ سالانه (مبنای ۲۴۰ روز کاری)
    initial_capital: float = 0.0
    final_equity: float = 0.0          # ارزش نهایی پرتفوی به قیمت روز (Mark-to-Market)
    total_return_pct: float = 0.0
    blocked_orders: int = 0            # سفارش‌هایی که به دلیل صف قفل اجرا نشدند
    corporate_action_events: int = 0   # دفعاتی که پوزیشن باز از روز اقدام شرکتی عبور کرد
    corporate_action_receivable: float = 0.0  # ارزش کسر سهم پس از تعدیل اقدام شرکتی (ریال، جزو ارزش نهایی)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
