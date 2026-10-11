-- BRS Migration 003: Point-in-Time Data Pipeline & Feature Store (BRS v0.4.2)
-- 1. Add feature store tables: features_daily and features_intraday
-- 2. Raw columns (open_raw, high_raw, low_raw, close_raw, adjustment_factor) are added via column migrations

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
