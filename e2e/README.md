# E2E

Browser workflow tests for Event Hunt private-access boundaries and discovery.

## Prerequisites

- Node 20+ and `npm`
- Playwright browsers installed (`npx playwright install chromium`)
- The API server running at `http://127.0.0.1:8000` (or set `E2E_BASE_URL`)

## Running

```bash
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix e2e ci --ignore-scripts --no-audit --no-fund
npx --prefix e2e playwright install chromium
uv run python scripts/verify.py --check browser
```

If the API server is not running, the scenarios fail at `Given the web app
is running`. The full local verifier, `python3 scripts/verify.py`, starts its
own fixture server before invoking `npm test`. See
[contributing](../CONTRIBUTING.md).
The seed helper applies SQLite migrations and
inserts synthetic events and an invited test user, and signs a fixture session
using the same serializer as production. The cookie stays in the temporary test
directory and is removed with the fixture server. There is no app auth bypass.
For a separately managed fixture server, set `E2E_BASE_URL` and `E2E_SESSION_FILE`
to its synthetic session file. Never point these tests at a real user's account.

## Layout

- `features/` — Gherkin feature files
- `support/` — World + step definitions
- `test-env.ts` — runtime config (base URL)
- `cucumber.config.ts` — Cucumber CLI config
