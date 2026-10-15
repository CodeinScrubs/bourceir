-- Local user settings, manual plans and explicitly separated paper/actual execution journal.
CREATE TABLE IF NOT EXISTS personal_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    capital_rial INTEGER NOT NULL CHECK (capital_rial > 0),
    max_loss_rial INTEGER NOT NULL CHECK (max_loss_rial > 0 AND max_loss_rial < capital_rial),
    actual_history_complete INTEGER NOT NULL DEFAULT 0 CHECK (actual_history_complete IN (0, 1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_plans (
    id TEXT PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id),
    created_at TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('intraday', 'swing', 'weeks_months')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'actual')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    entry_rial REAL NOT NULL CHECK (entry_rial > 0),
    stop_rial REAL NOT NULL CHECK (stop_rial > 0 AND stop_rial < entry_rial),
    target_rial REAL NOT NULL CHECK (target_rial > entry_rial),
    rationale TEXT NOT NULL,
    calculation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_executions (
    id TEXT PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'actual')),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_rial REAL NOT NULL CHECK (price_rial > 0),
    fee_rial REAL NOT NULL CHECK (fee_rial >= 0),
    fee_source TEXT NOT NULL CHECK (fee_source IN ('actual', 'config_estimate')),
    executed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    rationale TEXT NOT NULL,
    plan_id TEXT REFERENCES trade_plans(id)
);
CREATE INDEX IF NOT EXISTS idx_journal_mode_time ON journal_executions(mode, executed_at, recorded_at);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal_executions
BEGIN SELECT RAISE(ABORT, 'execution journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal_executions
BEGIN SELECT RAISE(ABORT, 'execution journal is append-only'); END;
