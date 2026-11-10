# Operations

## Source feeds

Select adapters with `nyc-ai-scout scrape --source` or
`nyc-ai-scout cycle --sources`. The catalog accepts dated listings from
any location, including online events.

Install Chromium for the Python collectors after syncing dependencies.

```sh
uv run --locked playwright install chromium
```

On Linux, install the
[Playwright system dependencies](https://playwright.dev/python/docs/browsers#install-system-dependencies)
for Chromium as well.

These settings replace the bundled listing URLs with public feeds from
the same provider. The plural variables accept whitespace-separated URLs.

| Adapter | Setting |
| --- | --- |
| Luma | `EVENT_HUNT_LUMA_LIST_URLS` |
| Eventbrite | `EVENT_HUNT_EVENTBRITE_LIST_URLS` |
| Meetup | `EVENT_HUNT_MEETUP_LIST_URLS` |
| Partiful | `EVENT_HUNT_PARTIFUL_LIST_URL` |

Betaworks and Cerebral Valley read their organization's event pages. The
bundled web calendar groups dates in `America/New_York`. The MCP tool
takes timezone-aware query bounds and returns UTC timestamps.

## Ranking and refresh

Copy [the example profile](../../data/taste-profile.example.md) to
`data/taste-profile.md` or set `TASTE_PROFILE_PATH`. The default prompt
scores the configured profile. Include location or attendance preferences
in that profile if they are relevant to the catalog.

Configure `LLM_SCORING_MODE` and the selected provider before running a
live cycle. The CLI cycle also expects a `DISCORD_WEBHOOK_URL` for its
posting integration. Use `nyc-ai-scout cycle --help` for available options.
The [local demo](../../README.md#quick-start) uses saved synthetic scores.

Availability refresh runs independently when scoring pauses. See
[Architecture](../development/architecture.md) for the cycle and
`nyc-ai-scout requeue-scoring-failures --help` for recovery options.

## Health

`GET /healthz` reports database and migration readiness. Verify discovery
and, when configured, [MCP access](../development/mcp.md) after a release.
