# Architecture

## System overview

| Component | Responsibility |
| --- | --- |
| [Worker](../../api/tasks/cycle.py) | Collects from configured feeds, refreshes registration availability, and scores events against the catalog’s preference profile. |
| [Catalog](database.md) | Stores listings, source evidence, availability, and scores. Use SQLite for a local catalog or PostgreSQL for a shared deployment. |
| [Web app](../../api/app.py) | Serves the calendar and filters through Starlette, Jinja templates, and the discovery assets in `web/discovery/`. |
| [MCP server](../../api/mcp/server.py) | Authorizes access and exposes the read-only `find_events` query over the ranked catalog. |

The web and MCP paths read the stored catalog. The worker calls the scorer
as part of collection, so reading an event does not require a model call.
The catalog’s fit score comes from a shared profile. A person or agent can
apply their own constraints when choosing from the results.

Event Hunt returns source links, observed availability, and timestamps.
The agent uses those links to check the organizer’s site. People define
the criteria and authorize any registration, mail, or calendar actions in
their agent client. [Agent workflow](mcp.md)

## Layout

```text
api/         Python backend (Starlette + Typer CLI)
  app.py        Starlette factory
  commands/     CLI entrypoints (Typer)
  configs/      Settings + env reads
  controllers/  HTTP/JSON route handlers
  integrations/ External services (Supabase/Google auth, Discord, LLM, scrapers)
  models/       Pydantic domain models
  repositories/ DB access split per aggregate
  schedule/     launchd helpers
  services/     Domain services (sessions, authorization, event catalog)
  tasks/        Background jobs (cycle, cleanup, backups, evaluation)
  tests/        Unit + integration tests + fixtures
web/         TypeScript discovery and account assets, plus a Next.js scaffold
e2e/         Cucumber + Playwright browser tests
db/migrate/  Versioned SQL migrations (sqlite + postgres)
deploy/      launchd templates and environment examples
doc/         Audience-partitioned docs
```

## Request flow (HTML)

1. Browser → Starlette → session middleware → controller
2. Controller → service → repository → DB
3. Controller → templated response (Jinja2)

The separate Next.js scaffold calls `/api/events`, whose authenticated success
path is still a stub returning an empty list. The hosted UI uses the HTML flow
above.

## Cycle (background)

`api.tasks.cycle.run_cycle` chains scrape → availability refresh → score → post.
Explicit public registration IDs also corroborate cross-posts when their title
and UTC start agree, even if host/venue data is missing. Luma and Betaworks
retain the embedded event ID; Meetup descriptions retain contextual registration
links. The existing CommonMark parser recognizes only complete top-level
registration assertions, not code examples or quoted links. Bare CTAs require
a standalone description; conflicting assertions in separate paragraphs do not
establish alternative identities. Unsupported HTML context or link-title
metadata is not discarded to infer registration identity. Missing Meetup
descriptions retain the last observed evidence only for the same title/start;
an explicit replacement or changed occurrence remains authoritative.
Blank/incidental links and title-only similarity never establish identity.
This is passive source evidence, not permission to fetch new domains.

Availability refresh still runs when scoring or posting is paused. Local launchd
workers can write fresh catalog data to SQLite or, through the restricted worker
role, to the canonical Supabase Postgres catalog.

## Recommendation annotations

The scoring batch stores optional highlights and registration notes with
their source evidence and observation timestamps. Web and MCP qualify
these annotations against current source content and expiry. Older scores
remain readable without annotations.

Scoring fingerprints include title, UTC start, venue, locality, attendance
format, host, and description.
Effective changes clear derived facts; a compare-and-set guard rejects late
results. Preserved partial descriptions retain their original observation age.
Evidence and its meaningful context define a fact; observation time defines
freshness only. The model selects a complete source time expression inside its
registration evidence. The existing dateutil parser normalizes it without
implicit year/date/hour/timezone defaults, and the application formats the note.
No source supplies a reliable statement-bound reference timestamp today, so
unresolved relative expressions stay unknown. Unsupported or ambiguous input
produces no claim. Scrape time, today and the event start are not substitutes.
Rechecking unchanged evidence renews freshness without changing the deadline;
changes to source context invalidate its derived facts through the same fingerprint.
Deadlines expire at their aware instant; deadline/scarcity notes also expire
after one day without a fresh source-content observation. Evidence matching
rejects unsupported spans but does not substitute for semantic model evaluation.
Web and MCP receive that effective expiry. The browser removes expired notes
while the page stays open; expiry does not trigger a new model call or reset
browsing state. A captured NULL hash uses a NULL-aware compare-and-set rather
than disabling the guard.
