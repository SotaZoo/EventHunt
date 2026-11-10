-- Durable, one-time browser transactions for Supabase OAuth + PKCE.

CREATE TABLE IF NOT EXISTS auth_transactions (
    state TEXT PRIMARY KEY CHECK (char_length(state) > 0 AND char_length(state) <= 255),
    flow_cookie_hash TEXT NOT NULL CHECK (char_length(flow_cookie_hash) = 64),
    pkce_verifier TEXT NOT NULL CHECK (char_length(pkce_verifier) > 0 AND char_length(pkce_verifier) <= 255),
    purpose TEXT NOT NULL CHECK (purpose IN ('login', 'consent')),
    authorization_id TEXT,
    subject TEXT CHECK (subject IS NULL OR (char_length(subject) > 0 AND char_length(subject) <= 255)),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_auth_transactions_expires_at
    ON auth_transactions(expires_at);

-- Server-owned configuration used by the Supabase custom access-token hook.
-- The MCP resource URL is inserted by deployment after the public URL is known.
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY CHECK (key IN ('mcp_resource_url')),
    value TEXT NOT NULL CHECK (char_length(value) > 0 AND char_length(value) <= 2048),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Supabase's Data API exposes the public schema. The application connects
-- directly to Postgres as the table owner, while browser roles get no table,
-- sequence, or default privileges and no RLS policies on application data.
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_event_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE linked_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE alpha_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE runtime_markers ENABLE ROW LEVEL SECURITY;
ALTER TABLE cycle_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE cycle_locks ENABLE ROW LEVEL SECURITY;
ALTER TABLE scout_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;

-- Supabase currently issues OAuth-server tokens with aud=authenticated even
-- when an RFC 8707 resource is supplied. For OAuth-client tokens only, bind
-- aud to this deployment's exact MCP resource. Ordinary web sessions have no
-- client_id and retain Supabase's default audience.
CREATE OR REPLACE FUNCTION public.event_scout_custom_access_token_hook(event JSONB)
RETURNS JSONB
LANGUAGE SQL
STABLE
SET search_path = ''
AS $$
    SELECT jsonb_build_object(
        'claims',
        CASE
            WHEN NULLIF(event->'claims'->>'client_id', '') IS NOT NULL THEN
                jsonb_set(
                    event->'claims',
                    '{aud}',
                    to_jsonb(COALESCE(
                        (
                            SELECT value
                            FROM public.app_config
                            WHERE key = 'mcp_resource_url'
                        ),
                        'urn:event-scout:unconfigured'
                    ))
                )
            ELSE event->'claims'
        END
    )
$$;

GRANT USAGE ON SCHEMA public TO supabase_auth_admin;
GRANT SELECT ON TABLE public.app_config TO supabase_auth_admin;
CREATE POLICY app_config_auth_hook_read
    ON public.app_config
    FOR SELECT
    TO supabase_auth_admin
    USING (key = 'mcp_resource_url');
GRANT EXECUTE
    ON FUNCTION public.event_scout_custom_access_token_hook(JSONB)
    TO supabase_auth_admin;
REVOKE EXECUTE
    ON FUNCTION public.event_scout_custom_access_token_hook(JSONB)
    FROM anon, authenticated, public;
