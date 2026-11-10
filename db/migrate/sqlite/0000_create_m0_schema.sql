-- db/migrate/sqlite/0000_create_m0_schema.sql
-- Bootstrap the M0 events table on fresh installs.
-- No-op on existing M0 databases (all CREATE IF NOT EXISTS).
-- Migration 0001 will then pivot this to the v1 channel schema.

BEGIN;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    starts_at TEXT,
    ends_at TEXT,
    venue TEXT,
    neighborhood TEXT,
    description TEXT,
    host TEXT,
    cover_image TEXT,
    ai_relevance INTEGER,
    category TEXT,
    tags_json TEXT DEFAULT '[]',
    summary TEXT,
    is_nyc INTEGER,
    classify_status TEXT NOT NULL DEFAULT 'unclassified',
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_starts    ON events(starts_at);
CREATE INDEX IF NOT EXISTS idx_events_classify  ON events(classify_status);
CREATE INDEX IF NOT EXISTS idx_events_relevance ON events(ai_relevance);

COMMIT;
