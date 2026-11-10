-- Cloud-safe runtime state and Discord posting claims.

BEGIN;

ALTER TABLE events ADD COLUMN posting_claimed_at TEXT;
ALTER TABLE events ADD COLUMN posting_claim_token TEXT;

CREATE INDEX IF NOT EXISTS idx_events_posting_claim ON events(posting_claimed_at);

CREATE TABLE IF NOT EXISTS runtime_markers (
    name TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cycle_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_all_failed INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_all_failed >= 0),
    last_error TEXT,
    last_failed_at TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cycle_locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    locked_until TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

COMMIT;
