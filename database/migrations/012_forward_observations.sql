-- Local prospective observations, separate from reported paper/actual executions.
CREATE TABLE IF NOT EXISTS forward_trials (
    id TEXT PRIMARY KEY,
    registered_at TEXT NOT NULL,
    protocol_json TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forward_events (
    id INTEGER PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES forward_trials(id),
    phase TEXT NOT NULL CHECK (phase IN ('assessment', 'followup')),
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    UNIQUE(trial_id, phase)
);
CREATE INDEX IF NOT EXISTS idx_forward_events_trial ON forward_events(trial_id, id);
CREATE TRIGGER IF NOT EXISTS forward_trial_no_update BEFORE UPDATE ON forward_trials
BEGIN SELECT RAISE(ABORT, 'forward trials are append-only'); END;
CREATE TRIGGER IF NOT EXISTS forward_trial_no_delete BEFORE DELETE ON forward_trials
BEGIN SELECT RAISE(ABORT, 'forward trials are append-only'); END;
CREATE TRIGGER IF NOT EXISTS forward_event_no_update BEFORE UPDATE ON forward_events
BEGIN SELECT RAISE(ABORT, 'forward events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS forward_event_no_delete BEFORE DELETE ON forward_events
BEGIN SELECT RAISE(ABORT, 'forward events are append-only'); END;
