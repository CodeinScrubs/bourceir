-- Public instrument metadata/prices for valuing ALL reported holdings, including rights/SME/base market.
-- These observations are never part of the research universe or feature store.
CREATE TABLE IF NOT EXISTS account_market_observations (
    id INTEGER PRIMARY KEY,
    symbol_fa TEXT NOT NULL,
    ins_code TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_market_symbol ON account_market_observations(symbol_fa, observed_at);
CREATE TRIGGER IF NOT EXISTS account_market_no_update BEFORE UPDATE ON account_market_observations
BEGIN SELECT RAISE(ABORT, 'account market observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS account_market_no_delete BEFORE DELETE ON account_market_observations
BEGIN SELECT RAISE(ABORT, 'account market observations are append-only'); END;
