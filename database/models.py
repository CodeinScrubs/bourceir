# -*- coding: utf-8 -*-
"""
تعریف اسکیمای جداول پایگاه داده SQLite برای BRS Data Engine
شامل جداول نمادها، قیمت‌های روزانه، اسنپ‌شات‌های لحظه‌ای بازار و لاگ‌های دریافت داده
"""

# جدول نمادها: نگهداری اطلاعات شناسایی نمادهای بورسی و فرابورسی
CREATE_SYMBOLS_TABLE = """
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tse_code TEXT UNIQUE NOT NULL,      -- شناسه یکتا یا کد TSETMC نماد
    symbol_fa TEXT NOT NULL,           -- نماد فارسی (مانند خودرو، فولاد)
    name_fa TEXT,                      -- نام کامل شرکت به فارسی
    market TEXT,                       -- بازار معاملاتی: بورس (bourse) یا فرابورس (farabourse)
    instrument_type TEXT DEFAULT 'stock', -- نوع ابزار مالی (پیش‌فرض: سهام عادی)
    is_active BOOLEAN DEFAULT 1,       -- وضعیت فعال بودن نماد
    created_at TIMESTAMP,              -- زمان ثبت نماد در سیستم
    updated_at TIMESTAMP               -- زمان آخرین به‌روزرسانی اطلاعات نماد
);
"""

# جدول قیمت‌های روزانه: نگهداری تاریخچه معاملات کندل روزانه نمادها با جلوگیری از داده‌های تکراری
CREATE_DAILY_PRICES_TABLE = """
CREATE TABLE IF NOT EXISTS daily_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,        -- شناسه نماد (کلید خارجی به جدول symbols)
    date TEXT NOT NULL,                -- تاریخ معامله با فرمت YYYY-MM-DD
    open REAL,                         -- قیمت بازگشایی
    high REAL,                         -- بالاترین قیمت روز
    low REAL,                          -- پایین‌ترین قیمت روز
    close REAL,                        -- آخرین قیمت معامله شده
    adj_close REAL,                    -- قیمت پایانی / تعدیل‌شده
    volume INTEGER,                    -- حجم معاملات
    value REAL,                        -- ارزش کل معاملات (ریال)
    trade_count INTEGER,               -- تعداد معاملات
    price_unit TEXT DEFAULT 'Rial',    -- واحد پولی قیمت‌ها (پیش‌فرض ریال)
    source TEXT DEFAULT 'pytse',       -- منبع داده (پیش‌فرض pytse)
    captured_at TIMESTAMP,             -- زمان ثبت اطلاعات در پایگاه داده
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, date, source)    -- جلوگیری از ثبت سوابق تکراری برای یک تاریخ و منبع
);
"""

# جدول اسنپ‌شات‌های لحظه‌ای: وضعیت صفوف خرید و فروش و رفتار حقیقی/حقوقی در طول ساعات معاملاتی
CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,        -- شناسه نماد (کلید خارجی به جدول symbols)
    captured_at TIMESTAMP NOT NULL,    -- زمان ثبت اسنپ‌شات در سامانه
    last_price REAL,                   -- آخرین قیمت معامله شده
    close_price REAL,                  -- قیمت پایانی بر اساس حجم مبنا
    open_price REAL,                   -- قیمت اولین معامله روز
    high_price REAL,                   -- بیشترین قیمت روز
    low_price REAL,                    -- کمترین قیمت روز
    volume INTEGER,                    -- حجم تجمیعی معاملات تا این لحظه
    value REAL,                        -- ارزش تجمیعی معاملات (ریال)
    trade_count INTEGER,               -- تعداد کل معاملات روز تا این لحظه
    buy_queue_volume INTEGER,          -- حجم سفارشات در بهترین صف خرید
    sell_queue_volume INTEGER,         -- حجم سفارشات در بهترین صف فروش
    real_buy_count INTEGER,            -- تعداد خریداران حقیقی
    real_sell_count INTEGER,           -- تعداد فروشندگان حقیقی
    legal_buy_count INTEGER,           -- تعداد خریداران حقوقی
    legal_sell_count INTEGER,          -- تعداد فروشندگان حقوقی
    real_buy_volume INTEGER,           -- حجم خرید حقیقی‌ها
    real_sell_volume INTEGER,          -- حجم فروش حقیقی‌ها
    legal_buy_volume INTEGER,          -- حجم خرید حقوقی‌ها
    legal_sell_volume INTEGER,         -- حجم فروش حقوقی‌ها
    price_unit TEXT DEFAULT 'Rial',    -- واحد ارزی قیمت‌ها (ریال)
    source TEXT DEFAULT 'pytse',       -- منبع دریافت داده
    provider_timestamp TIMESTAMP,      -- زمان اعلام شده توسط سرور ارائه‌دهنده داده
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, captured_at, source) -- جلوگیری از ثبت اسنپ‌شات تکراری در یک زمان
);
"""

# جدول لاگ فرآیندهای گردآوری: ثبت وضعیت اجرا، تعداد خطاهای احتمالی و سلامت داده‌ها
CREATE_COLLECTION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS collection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP,              -- زمان آغاز فرآیند گردآوری داده
    finished_at TIMESTAMP,             -- زمان اتمام فرآیند
    collection_type TEXT,              -- نوع دریافت: 'history' یا 'snapshot'
    symbols_requested INTEGER,         -- تعداد نمادهای درخواست‌شده
    symbols_succeeded INTEGER,         -- تعداد نمادهایی که با موفقیت دریافت شدند
    symbols_failed INTEGER,            -- تعداد نمادهای با خطا
    errors TEXT,                       -- متن یا خلاصه خطاهای رخ‌داده
    status TEXT                        -- وضعیت نهایی: 'success', 'partial', 'failed'
);
"""
