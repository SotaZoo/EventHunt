# Web interface

Render serves Starlette/Jinja in `api/`. The discovery home uses
`web/discovery/` (TypeScript, CSS and Ionic Core iOS-mode web components) and
`web/service/discovery.ts`. The separate Next.js app remains a scaffold;
building it is not a web release.

## Run the demo

Install Python 3.11, [uv](https://docs.astral.sh/uv/), Node.js with npm, and
OpenSSL. Run these commands from the repository root.

```sh
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix web run build:discovery
uv sync --extra dev --locked --python 3.11
```

Start the app with a fresh SQLite database and sample events.

```sh
env -i PATH="$PATH" sh <<'DEMO'
set -e
export DATABASE_PATH="$(mktemp -d)/events.db"
export NYC_AI_EVENTS_SESSION_SECRET="$(openssl rand -hex 32)"
uv run --locked python e2e/seed_db.py
exec uv run --locked nyc-ai-scout serve --host 127.0.0.1 --port 8847
DEMO
```

Open [the local sign-in page](http://127.0.0.1:8847/login), expand **Local
invite code**, and enter `e2e-discovery-only`. The demo uses saved scores
and needs no provider credentials.

## Served discovery home

```bash
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix web run build:discovery
uv run nyc-ai-scout serve
```

The build produces ignored `web/dist/discovery/` assets. Starlette serves them
at `/assets/discovery`; wheels include them under `api/_runtime/web`. Build
assets before building/installing the Python package. The production Dockerfile
does this in a Node build stage; the runtime needs no Node process or public CDN.
Ionic supplies the approved controls; esbuild bundles only browser assets. Both
dependencies are MIT-licensed and pinned in the Node lockfile.

The discovery and account CSS/JS entrypoint URLs include a content hash of the
served files. Build assets before application startup and deploy/restart after
updates; returning browsers receive changed assets without clearing their cache.

`/api/discovery` requires the existing app session and returns the complete,
dated, eligible upcoming catalog in New York time. For this small friend pilot,
filtering the complete set in the browser keeps the preview count, agenda and
calendar dots consistent without a misleading top-100 limit. Navigation state
is retained in browser history and per-tab session storage for source and Account
returns; event data and credentials are not stored there.
MCP's bounded ranked query contract is unchanged.

Cards show title, location/Online, optional food, fit followed by a concrete
positive highlight, and optional registration urgency last. Text wraps at the
normal supporting-text size. Every fit >= 0.90 gets a passive warm-neutral
capsule; chronology and filters do not change. Missing annotations produce no
empty rows and never fall back to full reasoning or event-proximity urgency.
The catalog includes the effective urgency expiry; an open agenda removes the
note when it expires, without reloading or resetting calendar/filter context.
Map-pin, monitor, utensils and clock paths come from Lucide 0.468.0 under the
vendored [Lucide and Feather notices](../../web/discovery/icons/LICENSE).
The discovery bundles include those notices and the Ionic Core MIT notice.

Web and MCP share occurrence cleanup before counts and limits. Confirmed
canceled, full, and registration-closed listings are hidden; joinable waitlists
remain visible with a **Waitlist** label. This is
[source availability, not personal registration](CONTEXT.md).

Attendance is nullable and source-backed; old records are not guessed or
backfilled. JSON-LD ingestion retains explicit schema.org attendance or typed
Place/VirtualLocation evidence. Sources without that evidence remain unknown
and appear under All. Existing unknown values require a new evidence-bearing
scrape; deploying the web alone does not upgrade the separately managed worker.

The additive `0010_event_attendance.sql`, `0011_registration_availability.sql`
and `0012_registration_identity.sql` migrations must run before the new worker.
The annotation worker also requires `0013_event_annotations.sql`; apply it
before promoting the current scoring prompt. Deploying just the UI does not generate stored highlights.
Old workers remain compatible with the nullable attendance and registration URL
columns and default-unknown availability. Roll back application packages if
needed; leave these additive columns in place. Rebuild and approve the local CI
dependency image/digest for the new lockfile and verifier
before relying on a release receipt. No automatic deployment is introduced.

See the [approved design tokens](../design/design-system.md) and the existing
[release gate](../administration/local-ci.md).

## Account and AI access

The profile entry opens `/settings` (Account), with MCP setup and access
management at `/settings/ai`. `web/account/` uses the discovery design tokens
and builds into the same asset directory; these are served Jinja pages, not
Next.js routes. Supabase still owns OAuth and sign-in. See [MCP](mcp.md) for the
connection and removal contract.

Feedback opens an email draft to the approved `info@sotazoo.com` inbox with
subject **Event Hunt feedback**, identifying the app in the company inbox.
`NYC_AI_EVENTS_FEEDBACK_EMAIL` can override the recipient; an empty or invalid
override hides the row.

Old `/mine` and `/hidden` bookmarks redirect to `/events` after the existing
account check. Legacy status/filter parameters are not carried into discovery.
Stored historical event states are preserved; those screens are retired.

## Separate scaffold

- Next.js 15 (App Router) + React 19
- TypeScript
- Jest + React Testing Library

## Layout

- `web/app/`: Routes and layouts
- `web/components/`: Reusable components
- `web/service/`: API client (`apiGet` from `service/api.ts`)
- `web/types/`: Shared TypeScript types
- `web/utils/`: Pure utilities
- `web/hooks/`: React hooks
- `web/__tests__/`: Component and page tests

## Local dev

```bash
cd web
npm ci
npm run dev
```

The dev server expects the backend API at `http://127.0.0.1:8000`. Set
`NEXT_PUBLIC_API_BASE_URL` to override.

## Testing

```bash
npm --prefix web test
npm --prefix web run build
```
