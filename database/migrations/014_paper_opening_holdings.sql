-- موجودی آغازین مجازی با منبع مستقل؛ هیچ اجرای خرید یا رکورد واقعی ساخته نمی‌شود.
CREATE TABLE IF NOT EXISTS paper_openings (
    portfolio_id TEXT PRIMARY KEY REFERENCES paper_portfolios(id),
    broker_import_id TEXT NOT NULL REFERENCES broker_imports(id),
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    source_captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_openings_source ON paper_openings(broker_import_id);
CREATE TRIGGER IF NOT EXISTS paper_openings_no_update BEFORE UPDATE ON paper_openings
BEGIN SELECT RAISE(ABORT, 'paper openings are append-only'); END;
CREATE TRIGGER IF NOT EXISTS paper_openings_no_delete BEFORE DELETE ON paper_openings
BEGIN SELECT RAISE(ABORT, 'paper openings are append-only'); END;
