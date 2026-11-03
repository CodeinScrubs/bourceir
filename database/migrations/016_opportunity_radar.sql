-- Append-only public-market attention ledger. No orders, account balances or fills.
CREATE TABLE IF NOT EXISTS radar_runs (
    id TEXT PRIMARY KEY, registered_at TEXT NOT NULL,
    protocol_json TEXT NOT NULL, protocol_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS radar_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES radar_runs(id), recorded_at TEXT NOT NULL,
    report_json TEXT NOT NULL, report_sha256 TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL, chain_sha256 TEXT NOT NULL,
    episodes_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_cycles_run ON radar_cycles(run_id,id);
CREATE TABLE IF NOT EXISTS radar_episodes (
    run_id TEXT NOT NULL REFERENCES radar_runs(id), episode_key TEXT NOT NULL,
    payload_json TEXT NOT NULL, PRIMARY KEY(run_id,episode_key)
);
CREATE TABLE IF NOT EXISTS radar_notice_versions (
    run_id TEXT NOT NULL REFERENCES radar_runs(id), notice_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,notice_key)
);
CREATE TABLE IF NOT EXISTS radar_attention_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES radar_runs(id), cycle_id INTEGER NOT NULL REFERENCES radar_cycles(id),
    recorded_at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_events_run ON radar_attention_events(run_id,id);
CREATE TRIGGER IF NOT EXISTS radar_runs_no_update BEFORE UPDATE ON radar_runs BEGIN SELECT RAISE(ABORT,'radar runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_runs_no_delete BEFORE DELETE ON radar_runs BEGIN SELECT RAISE(ABORT,'radar runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_cycles_no_update BEFORE UPDATE ON radar_cycles BEGIN SELECT RAISE(ABORT,'radar cycles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_cycles_no_delete BEFORE DELETE ON radar_cycles BEGIN SELECT RAISE(ABORT,'radar cycles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_notices_no_update BEFORE UPDATE ON radar_notice_versions BEGIN SELECT RAISE(ABORT,'radar notice versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_notices_no_delete BEFORE DELETE ON radar_notice_versions BEGIN SELECT RAISE(ABORT,'radar notice versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_events_no_update BEFORE UPDATE ON radar_attention_events BEGIN SELECT RAISE(ABORT,'radar attention events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_events_no_delete BEFORE DELETE ON radar_attention_events BEGIN SELECT RAISE(ABORT,'radar attention events are immutable'); END;
