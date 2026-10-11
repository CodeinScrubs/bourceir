-- BRS Migration 001: Initial Schema
-- Base tables for symbols, daily prices, snapshots, and collection log

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tse_code TEXT UNIQUE NOT NULL,
    symbol_fa TEXT NOT NULL,
    name_fa TEXT,
    market TEXT,
    instrument_type TEXT DEFAULT 'stock',
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume INTEGER,
    value REAL,
    trade_count INTEGER,
    price_unit TEXT DEFAULT 'Rial',
    source TEXT DEFAULT 'pytse',
    captured_at TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, date, source)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    captured_at TIMESTAMP NOT NULL,
    last_price REAL,
    close_price REAL,
    open_price REAL,
    high_price REAL,
    low_price REAL,
    volume INTEGER,
    value REAL,
    trade_count INTEGER,
    buy_queue_volume INTEGER,
    sell_queue_volume INTEGER,
    real_buy_count INTEGER,
    real_sell_count INTEGER,
    legal_buy_count INTEGER,
    legal_sell_count INTEGER,
    real_buy_volume INTEGER,
    real_sell_volume INTEGER,
    legal_buy_volume INTEGER,
    legal_sell_volume INTEGER,
    price_unit TEXT DEFAULT 'Rial',
    source TEXT DEFAULT 'pytse',
    provider_timestamp TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    UNIQUE(symbol_id, captured_at, source)
);

CREATE TABLE IF NOT EXISTS collection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    collection_type TEXT,
    symbols_requested INTEGER,
    symbols_succeeded INTEGER,
    symbols_failed INTEGER,
    errors TEXT,
    status TEXT
);
