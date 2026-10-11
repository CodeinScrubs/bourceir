# -*- coding: utf-8 -*-
"""
تعریف اسکیمای جداول پایگاه داده SQLite برای BRS Data Engine
شامل جداول نمادها، قیمت‌های روزانه، اسنپ‌شات‌های لحظه‌ای بازار و لاگ‌های دریافت داده
"""

# جدول نمادها: نگهداری اطلاعات شناسایی، چرخه حیات و متادیتای نمادها
CREATE_SYMBOLS_TABLE = """
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tse_code TEXT UNIQUE NOT NULL,      -- شناسه یکتا یا کد TSETMC نماد
    symbol_fa TEXT NOT NULL,           -- نماد فارسی (مانند خودرو، فولاد)
    name_fa TEXT,                      -- نام کامل شرکت به فارسی
    market TEXT,                       -- بازار معاملاتی: بورس (bourse) یا فرابورس (farabourse)
    instrument_type TEXT DEFAULT 'stock', -- نوع ابزار مالی (پیش‌فرض: سهام عادی)
    is_active BOOLEAN DEFAULT 1,       -- وضعیت فعال بودن نماد
    status TEXT DEFAULT 'active',      -- وضعیت چرخه حیات: 'active', 'halted', 'delisted', 'transferred'
    listed_date TEXT,                  -- تاریخ پذیرش در بورس
    delisted_date TEXT,                -- تاریخ لغو پذیرش / خروج از بازار
    base_volume INTEGER DEFAULT 1,     -- حجم مبنا
    created_at TIMESTAMP,              -- زمان ثبت نماد در سیستم
    updated_at TIMESTAMP               -- زمان آخرین به‌روزرسانی اطلاعات نماد
);
"""

# جدول قیمت‌های روزانه: نگهداری تاریخچه معاملات کندل روزانه (Point-in-Time تفکیک داده خام و تعدیل‌شده)
CREATE_DAILY_PRICES_TABLE = """
CREATE TABLE IF NOT EXISTS daily_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,        -- شناسه نماد (کلید خارجی به جدول symbols)
    date TEXT NOT NULL,                -- تاریخ معامله با فرمت YYYY-MM-DD
    open REAL,                         -- قیمت بازگشایی (برای سازگاری)
    high REAL,                         -- بالاترین قیمت روز
    low REAL,                          -- پایین‌ترین قیمت روز
    close REAL,                        -- آخرین قیمت معامله شده
    adj_close REAL,                    -- قیمت پایانی تعدیل‌شده
    volume INTEGER,                    -- حجم معاملات
    value REAL,                        -- ارزش کل معاملات (ریال)
    trade_count INTEGER,               -- تعداد معاملات
    price_unit TEXT DEFAULT 'Rial',    -- واحد پولی قیمت‌ها (پیش‌فرض ریال)
    source TEXT DEFAULT 'pytse',       -- منبع داده (پیش‌فرض pytse)
    captured_at TIMESTAMP,             -- زمان ثبت اطلاعات در پایگاه داده
    open_raw REAL,                     -- قیمت بازگشایی خام و تعدیل‌نشده (Point-in-Time)
    high_raw REAL,                     -- بالاترین قیمت خام و تعدیل‌نشده
    low_raw REAL,                      -- پایین‌ترین قیمت خام و تعدیل‌نشده
    close_raw REAL,                    -- آخرین قیمت خام و تعدیل‌نشده
    adjustment_factor REAL DEFAULT 1.0,-- ضریب تعدیل در تاریخ معامله
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, date, source)    -- جلوگیری از ثبت سوابق تکراری برای یک تاریخ و منبع
);
"""

# جدول اسنپ‌شات‌های لحظه‌ای: وضعیت صفوف، دفترچه سفارشات و رفتار معامله‌گران
CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,        -- شناسه نماد (کلید خارجی به جدول symbols)
    captured_at TIMESTAMP NOT NULL,    -- زمان ثبت اسنپ‌شات در سامانه محلی
    provider_timestamp TIMESTAMP,      -- زمان ثبت اعلام شده توسط سرور بورس
    last_price REAL,                   -- آخرین قیمت معامله شده
    close_price REAL,                  -- قیمت پایانی بر اساس حجم مبنا
    open_price REAL,                   -- قیمت اولین معامله روز
    high_price REAL,                   -- بیشترین قیمت روز
    low_price REAL,                    -- کمترین قیمت روز
    base_volume INTEGER DEFAULT 1,     -- حجم مبنا در لحظه اسنپ‌شات
    volume INTEGER,                    -- حجم تجمیعی معاملات تا این لحظه
    value REAL,                        -- ارزش تجمیعی معاملات (ریال)
    trade_count INTEGER,               -- تعداد کل معاملات روز تا این لحظه
    buy_queue_volume INTEGER,          -- حجم سفارشات در صف خرید
    sell_queue_volume INTEGER,         -- حجم سفارشات در صف فروش
    bid_price_1 REAL,                  -- بهترین مظنه خرید ۱
    bid_vol_1 INTEGER,                 -- حجم در مظنه خرید ۱
    ask_price_1 REAL,                  -- بهترین مظنه فروش ۱
    ask_vol_1 INTEGER,                 -- حجم در مظنه فروش ۱
    bid_price_2 REAL,                  -- مظنه خرید ۲
    bid_vol_2 INTEGER,                 -- حجم در مظنه خرید ۲
    ask_price_2 REAL,                  -- مظنه فروش ۲
    ask_vol_2 INTEGER,                 -- حجم در مظنه فروش ۲
    bid_price_3 REAL,                  -- مظنه خرید ۳
    bid_vol_3 INTEGER,                 -- حجم در مظنه خرید ۳
    ask_price_3 REAL,                  -- مظنه فروش ۳
    ask_vol_3 INTEGER,                 -- حجم در مظنه فروش ۳
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

# جدول منابع داده: مدیریت و اولویت‌بندی منابع تأمین داده
CREATE_DATA_SOURCES_TABLE = """
CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT UNIQUE NOT NULL,  -- کد منبع (مانند pytse, finpy, tsetmc_direct, broker_api)
    name_fa TEXT NOT NULL,             -- نام نمایشی منبع
    priority INTEGER DEFAULT 1,        -- اولویت استفاده
    is_active BOOLEAN DEFAULT 1,       -- فعال بودن منبع
    created_at TIMESTAMP
);
"""

# جدول اقدامات شرکتی: افزایش سرمایه، تقسیم سود نقدی (DPS) و ضرایب تعدیل قیمت
CREATE_CORPORATE_ACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,        -- شناسه نماد
    event_date TEXT NOT NULL,          -- تاریخ مجمع یا اقدام شرکتی (YYYY-MM-DD)
    event_type TEXT NOT NULL,          -- نوع: 'capital_increase', 'dividend', 'share_split'
    adjustment_factor REAL DEFAULT 1.0,-- ضریب تعدیل قیمت
    cash_dividend REAL DEFAULT 0.0,    -- سود نقدی تقسیمی (ریال)
    capital_increase_pct REAL DEFAULT 0.0, -- درصد افزایش سرمایه
    description TEXT,                  -- توضیحات
    captured_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, event_date, event_type)
);
"""

# جدول تقویم بازار بورس: تعیین روزهای کاری و تعطیلات رسمی
CREATE_MARKET_CALENDAR_TABLE = """
CREATE TABLE IF NOT EXISTS market_calendar (
    calendar_date TEXT PRIMARY KEY,    -- تاریخ به فرمت YYYY-MM-DD
    is_trading_day BOOLEAN NOT NULL,   -- آیا روز معاملاتی است؟
    holiday_reason TEXT,               -- دلیل تعطیلی (در صورت عدم معامله)
    market_phase TEXT DEFAULT 'regular'-- وضعیت روز: regular, half_day, special
);
"""

# جدول ریزمعاملات درون‌روزی: ثبت تیک‌به‌تیک معاملات برای نوسان‌گیری درون‌روزی (اسکالپ)
CREATE_INTRADAY_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS intraday_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,        -- شناسه نماد
    trade_time TEXT NOT NULL,          -- زمان معامله به فرمت HH:MM:SS
    trade_date TEXT NOT NULL,          -- تاریخ معامله YYYY-MM-DD
    price REAL NOT NULL,               -- قیمت معامله شده (ریال)
    volume INTEGER NOT NULL,           -- حجم این معامله
    buyer_type TEXT DEFAULT 'unknown', -- حقیقی/حقوقی
    seller_type TEXT DEFAULT 'unknown',-- حقیقی/حقوقی
    trade_number INTEGER,              -- شماره سریال معامله در سیستم بورس
    captured_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, trade_date, trade_number)
);
"""

# جدول ارزیابی کیفیت داده‌ها: رتبه سلامت و خطاهای آماری داده هر روز
CREATE_DATA_QUALITY_TABLE = """
CREATE TABLE IF NOT EXISTS data_quality_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    completeness_score REAL DEFAULT 100.0, -- درصد کامل بودن داده
    price_anomaly_flag BOOLEAN DEFAULT 0,  -- آیا جهش غیرعادی قیمت داشته؟
    volume_anomaly_flag BOOLEAN DEFAULT 0, -- آیا حجم مشکوک/غیرعادی داشته؟
    quality_score REAL DEFAULT 100.0,      -- نمره کیفیت داده (۰ تا ۱۰۰)
    notes TEXT,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, trade_date)
);
"""

# جدول آزمایش‌ها و بکتست‌ها: ثبت پارامترها و نتایج آزمایش‌های استراتژی و یادگیری ماشین
CREATE_EXPERIMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT UNIQUE NOT NULL, -- شناسه یکتا مانند EXP-001
    created_at TIMESTAMP NOT NULL,
    strategy_name TEXT NOT NULL,        -- نام استراتژی یا مدل
    symbols_tested TEXT,                -- نمادهای مورد تست (کاماجدا شده)
    start_date TEXT,                    -- تاریخ شروع بازه تست
    end_date TEXT,                      -- تاریخ پایان بازه تست
    parameters_json TEXT,               -- پارامترهای استراتژی به صورت JSON
    metrics_json TEXT,                  -- نتایج عملکرد (Win Rate, Profit Factor, Max Drawdown)
    status TEXT DEFAULT 'completed'     -- وضعیت: 'running', 'completed', 'failed'
);
"""

# جدول انبار ویژگی‌های روزانه (Daily Feature Store): مرجع یکتای فاکتورهای تکنیکال و بازار
CREATE_FEATURES_DAILY_TABLE = """
CREATE TABLE IF NOT EXISTS features_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    return_1d REAL,
    return_5d REAL,
    volatility_20d REAL,
    atr_14d REAL,
    rsi_14d REAL,
    sma_20d REAL,
    sma_50d REAL,
    volume_ratio_20d REAL,
    buyer_power_ratio REAL,
    smart_money_flow REAL,
    created_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, date)
);
"""

# جدول انبار ویژگی‌های درون‌روزی (Intraday Feature Store): مرجع یکتای فاکتورهای نوسان‌گیری و اسکالپ
CREATE_FEATURES_INTRADAY_TABLE = """
CREATE TABLE IF NOT EXISTS features_intraday (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    buyer_power REAL,
    queue_pressure REAL,
    order_imbalance REAL,
    smart_money_inflow REAL,
    volume_acceleration REAL,
    bid_ask_spread_bps REAL,
    created_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, timestamp)
);
"""
