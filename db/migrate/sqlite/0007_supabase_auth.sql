-- Durable, one-time browser transactions for Supabase OAuth + PKCE.

BEGIN;

CREATE TABLE IF NOT EXISTS auth_transactions (
    state TEXT PRIMARY KEY CHECK (length(state) > 0 AND length(state) <= 255),
    flow_cookie_hash TEXT NOT NULL CHECK (length(flow_cookie_hash) = 64),
    pkce_verifier TEXT NOT NULL CHECK (length(pkce_verifier) > 0 AND length(pkce_verifier) <= 255),
    purpose TEXT NOT NULL CHECK (purpose IN ('login', 'consent')),
    authorization_id TEXT,
    subject TEXT CHECK (subject IS NULL OR (length(subject) > 0 AND length(subject) <= 255)),
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_auth_transactions_expires_at
    ON auth_transactions(expires_at);

COMMIT;
