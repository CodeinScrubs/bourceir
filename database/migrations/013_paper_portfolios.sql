-- پورتفوهای مجازی مستقل؛ هیچ ردیف حساب واقعی یا داده بازار تغییر نمی‌کند.
CREATE TABLE IF NOT EXISTS paper_portfolios (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    capital_rial INTEGER NOT NULL CHECK (capital_rial > 0),
    max_loss_rial INTEGER NOT NULL CHECK (max_loss_rial > 0 AND max_loss_rial < capital_rial),
    mode TEXT NOT NULL CHECK (mode IN ('manual', 'observed')),
    protocol_json TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_ideas (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES paper_portfolios(id),
    symbol_id INTEGER NOT NULL REFERENCES symbols(id),
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_ideas_portfolio ON paper_ideas(portfolio_id, created_at);
CREATE TABLE IF NOT EXISTS paper_events (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    portfolio_id TEXT NOT NULL REFERENCES paper_portfolios(id),
    idea_id TEXT REFERENCES paper_ideas(id),
    kind TEXT NOT NULL CHECK (kind IN ('trade', 'valuation')),
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    CHECK ((kind = 'trade' AND idea_id IS NOT NULL) OR (kind = 'valuation' AND idea_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_paper_events_portfolio ON paper_events(portfolio_id, id);
CREATE TRIGGER IF NOT EXISTS paper_portfolios_no_update BEFORE UPDATE ON paper_portfolios
BEGIN SELECT RAISE(ABORT, 'paper portfolios are append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_portfolios_no_delete BEFORE DELETE ON paper_portfolios
BEGIN SELECT RAISE(ABORT, 'paper portfolios are append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_ideas_no_update BEFORE UPDATE ON paper_ideas
BEGIN SELECT RAISE(ABORT, 'paper ideas are append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_ideas_no_delete BEFORE DELETE ON paper_ideas
BEGIN SELECT RAISE(ABORT, 'paper ideas are append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_events_no_update BEFORE UPDATE ON paper_events
BEGIN SELECT RAISE(ABORT, 'paper events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_events_no_delete BEFORE DELETE ON paper_events
BEGIN SELECT RAISE(ABORT, 'paper events are append-only'); END;
