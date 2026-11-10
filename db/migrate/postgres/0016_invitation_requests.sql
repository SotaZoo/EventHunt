CREATE TABLE invitation_guard (singleton INTEGER PRIMARY KEY CHECK (singleton = 1));
INSERT INTO invitation_guard VALUES (1);
CREATE TABLE invitation_requests (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    invite_id BIGINT REFERENCES alpha_invites(id)
);
CREATE TABLE invitation_mail (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL REFERENCES invitation_requests(id),
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
ALTER TABLE invitation_guard ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitation_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitation_mail ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitation_limits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON invitation_guard, invitation_requests, invitation_mail, invitation_limits FROM anon, authenticated, event_scout_worker;
REVOKE ALL ON SEQUENCE invitation_requests_id_seq, invitation_mail_id_seq FROM anon, authenticated, event_scout_worker;
