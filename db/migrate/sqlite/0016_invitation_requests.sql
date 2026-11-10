CREATE TABLE invitation_guard (singleton INTEGER PRIMARY KEY CHECK (singleton = 1));
INSERT INTO invitation_guard VALUES (1);
CREATE TABLE invitation_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    invite_id INTEGER REFERENCES alpha_invites(id)
);
CREATE TABLE invitation_mail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES invitation_requests(id),
    kind TEXT NOT NULL CHECK (kind IN ('notification', 'invitation')),
    message_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','unknown','canceled')),
    attempted_at TEXT,
    attempt_token TEXT,
    error_code TEXT,
    UNIQUE (request_id, kind)
);
CREATE TABLE invitation_limits (
    key TEXT PRIMARY KEY,
    expires_at BIGINT NOT NULL,
    count INTEGER NOT NULL
);
