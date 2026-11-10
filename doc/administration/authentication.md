# Authentication

The [local demo](../../README.md#quick-start) signs in with a synthetic
invite code. A shared deployment uses its own HTTPS URL, session secret,
and configured OAuth provider.

## Supabase Google setup

Configure Google as an identity provider in your Supabase project and
set these variables for the app:

- `NYC_AI_EVENTS_FRIEND_MODE=1`
- `NYC_AI_EVENTS_PUBLIC_BASE_URL`
- `NYC_AI_EVENTS_SESSION_SECRET`
- `NYC_AI_EVENTS_SUPABASE_URL`
- `NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY`

Configure the public base URL and OAuth redirects for that installation.
Use a random session secret of at least 32 characters. Supabase OAuth
can also authorize the [read-only MCP endpoint](../development/mcp.md).

## Sign-in policy

Set `EVENT_HUNT_OPEN_GOOGLE_SIGNUP=1` to admit new verified Google accounts
with Supabase Auth. For invite-only setup, see
`nyc-ai-scout user invite-google --help`. Account
deactivation continues to apply in either mode.

The legacy direct Google OAuth path uses
`NYC_AI_EVENTS_GOOGLE_CLIENT_ID` and `NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET`.
The MCP server accepts either configured Supabase OAuth or its static
bearer credential.
