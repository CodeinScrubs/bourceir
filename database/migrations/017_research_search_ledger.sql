-- Local search accounting. This records computation, never trading permission.
CREATE TABLE IF NOT EXISTS search_contracts (
    id TEXT PRIMARY KEY, registered_at TEXT NOT NULL, spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_trials (
    id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES search_contracts(id),
    display_name TEXT NOT NULL, registered_at TEXT NOT NULL, policy_json TEXT NOT NULL,
    UNIQUE(contract_id, policy_json)
);
CREATE TABLE IF NOT EXISTS search_attempts (
    id TEXT PRIMARY KEY, trial_id TEXT NOT NULL REFERENCES search_trials(id),
    run_id TEXT NOT NULL, registered_at TEXT NOT NULL, repeat_authorized INTEGER NOT NULL CHECK(repeat_authorized IN (0,1)),
    UNIQUE(run_id, trial_id)
);
CREATE INDEX IF NOT EXISTS idx_search_attempt_trial ON search_attempts(trial_id);
CREATE INDEX IF NOT EXISTS idx_search_attempt_run ON search_attempts(run_id);
CREATE TABLE IF NOT EXISTS search_terminal_events (
    attempt_id TEXT PRIMARY KEY REFERENCES search_attempts(id), recorded_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete','failed','interrupted')),
    evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS search_contracts_no_update BEFORE UPDATE ON search_contracts BEGIN SELECT RAISE(ABORT,'search contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_contracts_no_delete BEFORE DELETE ON search_contracts BEGIN SELECT RAISE(ABORT,'search contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_trials_no_update BEFORE UPDATE ON search_trials BEGIN SELECT RAISE(ABORT,'search trials are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_trials_no_delete BEFORE DELETE ON search_trials BEGIN SELECT RAISE(ABORT,'search trials are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_attempts_no_update BEFORE UPDATE ON search_attempts BEGIN SELECT RAISE(ABORT,'search attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_attempts_no_delete BEFORE DELETE ON search_attempts BEGIN SELECT RAISE(ABORT,'search attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_terminals_no_update BEFORE UPDATE ON search_terminal_events BEGIN SELECT RAISE(ABORT,'search events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS search_terminals_no_delete BEFORE DELETE ON search_terminal_events BEGIN SELECT RAISE(ABORT,'search events are immutable'); END;
