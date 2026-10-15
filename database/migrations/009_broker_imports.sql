-- Read-only broker observations are separate from market data, research and manual fills.
CREATE TABLE IF NOT EXISTS broker_imports (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('portfolio', 'turnover', 'fills')),
    source_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(kind, source_sha256, parser_version)
);
CREATE TABLE IF NOT EXISTS broker_records (
    import_id TEXT NOT NULL REFERENCES broker_imports(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    record_json TEXT NOT NULL,
    PRIMARY KEY(import_id, ordinal)
);
CREATE TABLE IF NOT EXISTS broker_baselines (
    id INTEGER PRIMARY KEY,
    portfolio_import_id TEXT NOT NULL REFERENCES broker_imports(id),
    adopted_at TEXT NOT NULL,
    capital_rial INTEGER NOT NULL CHECK (capital_rial > 0),
    max_loss_rial INTEGER NOT NULL CHECK (max_loss_rial > 0 AND max_loss_rial < capital_rial)
);
CREATE TRIGGER IF NOT EXISTS broker_imports_no_update BEFORE UPDATE ON broker_imports
BEGIN SELECT RAISE(ABORT, 'broker imports are append-only'); END;
CREATE TRIGGER IF NOT EXISTS broker_imports_no_delete BEFORE DELETE ON broker_imports
BEGIN SELECT RAISE(ABORT, 'broker imports are append-only'); END;
CREATE TRIGGER IF NOT EXISTS broker_records_no_update BEFORE UPDATE ON broker_records
BEGIN SELECT RAISE(ABORT, 'broker records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS broker_records_no_delete BEFORE DELETE ON broker_records
BEGIN SELECT RAISE(ABORT, 'broker records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS broker_baselines_no_update BEFORE UPDATE ON broker_baselines
BEGIN SELECT RAISE(ABORT, 'broker baselines are append-only'); END;
CREATE TRIGGER IF NOT EXISTS broker_baselines_no_delete BEFORE DELETE ON broker_baselines
BEGIN SELECT RAISE(ABORT, 'broker baselines are append-only'); END;
