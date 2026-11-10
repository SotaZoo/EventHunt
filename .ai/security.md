# Security

## Surfaces

- Hosted auth: Supabase Auth with Google via
  `api/integrations/supabase/auth.py`
- Legacy local auth fallback: direct Google OAuth via
  `api/integrations/google/oauth.py`
- Sessions: `api/services/sessions.py` (HMAC-signed app cookie plus HttpOnly
  Supabase access/refresh cookies)
- Agent auth: Supabase OAuth metadata and JWT verification in
  `api/mcp/server.py`
- Authorization: `api/services/authorization.py`
- Private alpha allowlist: `api/repositories/{users,invites,identities}.py`
  (`EVENT_HUNT_OPEN_GOOGLE_SIGNUP=1` admits new verified Google accounts;
  revocation and inactive-user checks still apply).

## Rules

- Never log secrets, OAuth tokens, session cookies, webhook URLs, or full DB rows.
- Never commit env files (`.env*`), `data/`, backups, or screenshots of friend data.
- All session cookies are `Secure` + `SameSite=Lax` in friend mode.
- No in-app `/admin`. Admin actions stay CLI-only.
- Friend-mode startup fails closed if `NYC_AI_EVENTS_PUBLIC_BASE_URL`, the
  session secret, or the selected Supabase/direct-Google auth configuration is
  incomplete.
- MCP verification outages (signing keys, identity storage, or provider
  UserInfo) deny access with HTTP 503 and no authentication challenge. Only
  invalid, revoked, or unauthorized credentials return HTTP 401. Do not cache
  successful UserInfo checks: revocation must still block the next request.

## Reporting

See `SECURITY.md` for vulnerability reporting.
