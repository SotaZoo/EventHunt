# Agent workflow

Event Hunt exposes the ranked catalog through the authenticated, read-only
`find_events(start, end, limit=5)` MCP tool. It returns a bounded shortlist
with the shared fit score, source URL, registration availability, and
freshness. These fields describe the catalog and organizer's listing.

## Connect

1. Sign in at [eventhunt.net](https://eventhunt.net).
2. Open **Account → AI integration**.
3. Copy the setup prompt under **Paste into your AI agent** into your
   OAuth-capable MCP client.

For Codex CLI, add the endpoint and sign in in the browser:

```sh
codex mcp add event-scout --url https://eventhunt.net/mcp
codex mcp login event-scout --scopes email,offline_access
```

Review the Event Hunt consent screen. Use the same Event Hunt account you
opened in the browser. [OpenAI Docs](https://learn.chatgpt.com/docs/extend/mcp)

To revoke access, open **Account → AI integration → Authorized clients**
and choose **Remove access** next to the client.

## Work with your agent

Tell the agent what you want to attend, when you are available, and
which actions you approve. For a first run, ask it to find a free AI
workshop next week and show you the choices before registering.

The agent uses Event Hunt to find candidates, then checks the organizer’s
page for current details. Choose an option or give the agent rules for
recurring runs. The agent can register within those rules and add the
event to your calendar once it verifies confirmation and has your permission.

## Schedule the workflow

Save the date window, timezone, and registration rules in one chat. In the
ChatGPT desktop app, ask for both schedules to run in that chat so the
follow-up can read the scout's registration requests.
[Schedule a task inside a chat](https://learn.chatgpt.com/docs/automations#schedule-a-task-inside-a-chat)

Check that the scheduled scout can access Event Hunt and the organizer's
site, and that the follow-up can access organizer replies and the calendar
it may update.

### Weekly scout

> Use Event Hunt to find up to five events for the next seven days. Check the
> organizer's date, timezone, and registration status. Register once for
> events that meet my saved rules, then verify the organizer's response.
> Record each request's event URL and status in this chat. Ask me about
> anything outside those rules.

### Daily follow-up

> Check organizer replies for the requests recorded in this chat. Add
> confirmed events to my calendar after checking for an existing entry.
> Leave pending requests and waitlists open. Report what changed.

Event Hunt supplies the shortlist. The agent uses the browser, mail, and
calendar tools you authorize to follow up.

## Query contract

The agent supplies inclusive, timezone-aware ISO 8601 start and end
timestamps to `find_events`. The limit is 1 through 25.
`catalog_updated_at` is the latest source observation across the catalog.
Each listing has its own `last_seen` and `availability_checked_at` timestamps
for the last observation and the last confirmed availability check.
Verify availability and any `time_warning` with the organizer before acting.

## Self-hosted MCP

Set `NYC_AI_EVENTS_PUBLIC_BASE_URL` to the deployment's public HTTPS origin.
Use that origin followed by `/mcp` as the resource URL.

For OAuth, configure [Supabase sign-in](../administration/authentication.md#supabase-google-setup).
Enable the Supabase OAuth server with the same Site URL, the authorization
path `/oauth/consent`, and an RS256 or ES256 signing key. Enable dynamic client
registration for clients that register automatically, or register a
compatible client in advance. [Supabase OAuth setup](https://supabase.com/docs/guides/auth/oauth-server/getting-started)

Request the `email` scope. Use a
[Supabase access token hook](https://supabase.com/docs/guides/auth/oauth-server/token-security#custom-access-token-hooks)
to set the MCP OAuth token audience to the exact resource URL. The app
checks the audience, scope, session, and admitted account.

For a single-user deployment, leave the Supabase settings unset and set
`NYC_AI_EVENTS_AGENT_API_TOKEN` to a private random value of at least 32
characters. Configure your client to send it as an HTTP bearer token.

For the protocol and authorization tests, run
`uv run --locked pytest api/tests/integration_tests/test_event_scout_mcp.py`.
