# Security (Administration)

## Secrets

All secrets are env-var driven. Never commit:

- `.env*` files
- OAuth client IDs/secrets and Supabase keys/tokens
- `NYC_AI_EVENTS_SESSION_SECRET`
- Webhook URLs
- `data/` (database, backups, logs)

## Hosted authentication

The hosted friend pilot uses Supabase Auth with Google. Restrict the Google
OAuth client to Supabase's exact provider callback and restrict Supabase's
redirect allowlist to Event Hunt's exact `/auth/supabase/callback` URL. Keep
the direct Google integration only as a separate local rollback path.

## Cookies

The Event Hunt session is HMAC-signed with itsdangerous. Supabase access and
refresh tokens are separate HttpOnly cookies. In friend mode all three are
`Secure` + `SameSite=Lax`; logout clears all three.

## User data

Invite codes are hashed before storage. Friend email, Event Hunt user, and
linked Supabase subject remain private database data. The CLI revocation flow
deactivates the accepted user so both future browser admission and MCP subject
verification fail.

## Public launch risks

Before opening the alpha to the public:

- Replace the internal Render URL with a reviewed production host and rollout.
- Separate migrations from container startup before running multiple web
  instances.
- Add structured audit logs for sign-in / sign-out / status mutations.
- Threat-model the Discord integration (server tokens, embed-source forging).
