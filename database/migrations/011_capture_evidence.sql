-- Immutable local run protocols and observations; never backfill old snapshots as prospective polls.
CREATE TABLE IF NOT EXISTS capture_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL,
    protocol_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES capture_runs(id),
    cycle INTEGER NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('poll_started', 'poll_finished', 'market_closed', 'run_stopped')),
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    CHECK ((event = 'run_stopped' AND cycle = 0) OR (event <> 'run_stopped' AND cycle > 0)),
    UNIQUE(run_id, cycle, event)
);
CREATE INDEX IF NOT EXISTS idx_capture_event_time ON capture_events(recorded_at, run_id);
CREATE TRIGGER IF NOT EXISTS capture_run_no_update BEFORE UPDATE ON capture_runs
BEGIN SELECT RAISE(ABORT, 'capture protocols are append-only'); END;
CREATE TRIGGER IF NOT EXISTS capture_run_no_delete BEFORE DELETE ON capture_runs
BEGIN SELECT RAISE(ABORT, 'capture protocols are append-only'); END;
CREATE TRIGGER IF NOT EXISTS capture_event_no_update BEFORE UPDATE ON capture_events
BEGIN SELECT RAISE(ABORT, 'capture events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS capture_event_no_delete BEFORE DELETE ON capture_events
BEGIN SELECT RAISE(ABORT, 'capture events are append-only'); END;
