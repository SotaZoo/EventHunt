-- db/migrate/sqlite/0003_add_applied_status.sql
-- Add the reset-era "applied" status to per-user event state.

BEGIN;

CREATE TABLE IF NOT EXISTS user_event_states_new (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN (
            'new',
            'saved',
            'dismissed',
            'interested',
            'registering',
            'applied',
            'pending_approval',
            'confirmed',
            'rejected'
        )),
    rating INTEGER CHECK (rating IS NULL OR rating IN (-1, 0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    status_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    rating_updated_at TEXT,
    PRIMARY KEY (user_id, event_id)
);

INSERT INTO user_event_states_new (
    user_id,
    event_id,
    status,
    rating,
    created_at,
    updated_at,
    status_updated_at,
    rating_updated_at
)
SELECT
    user_id,
    event_id,
    status,
    rating,
    created_at,
    updated_at,
    status_updated_at,
    rating_updated_at
FROM user_event_states;

DROP TABLE user_event_states;
ALTER TABLE user_event_states_new RENAME TO user_event_states;

CREATE INDEX IF NOT EXISTS idx_user_event_states_event ON user_event_states(event_id);
CREATE INDEX IF NOT EXISTS idx_user_event_states_status ON user_event_states(status);
CREATE INDEX IF NOT EXISTS idx_user_event_states_rating ON user_event_states(rating);

COMMIT;
