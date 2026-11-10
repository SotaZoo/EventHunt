-- db/migrate/sqlite/0004_google_alpha_auth.sql
-- Add Google identity links and friend allowlist for the private alpha.

BEGIN;

DROP INDEX IF EXISTS idx_users_invite_code_hash;
DROP INDEX IF EXISTS idx_users_is_active;
DROP INDEX IF EXISTS idx_user_event_states_event;
DROP INDEX IF EXISTS idx_user_event_states_status;
DROP INDEX IF EXISTS idx_user_event_states_rating;

ALTER TABLE users RENAME TO users_old;

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE CHECK (length(email) > 0 AND length(email) <= 254),
    display_name TEXT NOT NULL CHECK (length(display_name) > 0 AND length(display_name) <= 80),
    invite_code_hash TEXT UNIQUE CHECK (invite_code_hash IS NULL OR length(invite_code_hash) = 64),
    interest_flags_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO users (
    id,
    email,
    display_name,
    invite_code_hash,
    interest_flags_json,
    is_active,
    created_at,
    updated_at
)
SELECT
    id,
    email,
    display_name,
    invite_code_hash,
    interest_flags_json,
    is_active,
    created_at,
    updated_at
FROM users_old;

CREATE TABLE user_event_states_new (
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
DROP TABLE users_old;
ALTER TABLE user_event_states_new RENAME TO user_event_states;

CREATE INDEX IF NOT EXISTS idx_users_invite_code_hash ON users(invite_code_hash);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_user_event_states_event ON user_event_states(event_id);
CREATE INDEX IF NOT EXISTS idx_user_event_states_status ON user_event_states(status);
CREATE INDEX IF NOT EXISTS idx_user_event_states_rating ON user_event_states(rating);

CREATE TABLE IF NOT EXISTS linked_identities (
    provider TEXT NOT NULL CHECK (length(provider) > 0 AND length(provider) <= 32),
    subject TEXT NOT NULL CHECK (length(subject) > 0 AND length(subject) <= 255),
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL CHECK (length(email) > 0 AND length(email) <= 254),
    email_verified INTEGER NOT NULL CHECK (email_verified IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, subject)
);

CREATE INDEX IF NOT EXISTS idx_linked_identities_user ON linked_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_linked_identities_email ON linked_identities(email);

CREATE TABLE IF NOT EXISTS alpha_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL CHECK (length(email) > 0 AND length(email) <= 254),
    normalized_email TEXT NOT NULL CHECK (length(normalized_email) > 0 AND length(normalized_email) <= 254),
    invited_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    accepted_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    accepted_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alpha_invites_active_email
    ON alpha_invites(normalized_email)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alpha_invites_accepted_user
    ON alpha_invites(accepted_user_id);

COMMIT;
