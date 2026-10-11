-- BRS Migration 002: Quant Foundation v0.4
-- Add corporate actions, calendar, data sources, intraday trades, data quality, and experiments
-- Upgrade symbols with lifecycle and base_volume
-- Upgrade daily_prices with point-in-time raw prices
-- Upgrade snapshots with order book depth and queue dynamics

CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT UNIQUE NOT NULL,
    name_fa TEXT NOT NULL,
    priority INTEGER DEFAULT 1,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    adjustment_factor REAL DEFAULT 1.0,
    cash_dividend REAL DEFAULT 0.0,
    capital_increase_pct REAL DEFAULT 0.0,
    description TEXT,
    captured_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, event_date, event_type)
);

CREATE TABLE IF NOT EXISTS market_calendar (
    calendar_date TEXT PRIMARY KEY,
    is_trading_day BOOLEAN NOT NULL,
    holiday_reason TEXT,
    market_phase TEXT DEFAULT 'regular'
);

CREATE TABLE IF NOT EXISTS intraday_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    trade_time TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    price REAL NOT NULL,
    volume INTEGER NOT NULL,
    buyer_type TEXT DEFAULT 'unknown',
    seller_type TEXT DEFAULT 'unknown',
    trade_number INTEGER,
    captured_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, trade_date, trade_number)
);

CREATE TABLE IF NOT EXISTS data_quality_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    completeness_score REAL DEFAULT 100.0,
    price_anomaly_flag BOOLEAN DEFAULT 0,
    volume_anomaly_flag BOOLEAN DEFAULT 0,
    quality_score REAL DEFAULT 100.0,
    notes TEXT,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, trade_date)
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP NOT NULL,
    strategy_name TEXT NOT NULL,
    symbols_tested TEXT,
    start_date TEXT,
    end_date TEXT,
    dataset_version TEXT DEFAULT 'v1.0',
    features_version TEXT DEFAULT 'v1.0',
    model_version TEXT DEFAULT 'v1.0',
    parameters_json TEXT,
    metrics_json TEXT,
    status TEXT DEFAULT 'completed'
);
