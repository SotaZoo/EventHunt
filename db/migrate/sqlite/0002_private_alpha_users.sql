-- db/migrate/sqlite/0002_private_alpha_users.sql
-- Private-alpha user accounts and per-user event state.

BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE CHECK (length(email) > 0 AND length(email) <= 254),
    display_name TEXT NOT NULL CHECK (length(display_name) > 0 AND length(display_name) <= 80),
    invite_code_hash TEXT NOT NULL UNIQUE,
    interest_flags_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_event_states (
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

CREATE INDEX IF NOT EXISTS idx_users_invite_code_hash ON users(invite_code_hash);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_user_event_states_event ON user_event_states(event_id);
CREATE INDEX IF NOT EXISTS idx_user_event_states_status ON user_event_states(status);
CREATE INDEX IF NOT EXISTS idx_user_event_states_rating ON user_event_states(rating);

COMMIT;
