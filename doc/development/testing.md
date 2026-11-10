# Testing

## Layout

- Backend unit: `api/tests/unit_tests/`
- Backend integration: `api/tests/integration_tests/`
- Backend fixtures: `api/tests/fixtures/`
- Frontend: `web/__tests__/`
- Browser e2e: `e2e/features/`

## Commands

```bash
uv run pytest api/tests
npm --prefix web test -- --runInBand
npm --prefix e2e test
```

Run `uv run python scripts/verify.py --check browser` for browser QA. It seeds
a real synthetic catalog, starts the production routes with the test-only
Supabase provider factory, runs Cucumber, then stops the server and deletes the
temporary database. Provider destinations are loopback fixtures; no real
Google account or deployed MCP is exercised. Signed-token HTTP integration
tests separately verify real MCP requests, removal and reconnection.

The standalone `npm --prefix e2e test` command expects the same fixture server
at `E2E_BASE_URL` and its generated session file at `E2E_SESSION_FILE`.

## Conventions

- TDD: write a failing test first.
- Integration tests use real SQLite (no mocks for storage).
- Frontend tests assert behavior, not markup.
- E2E features cover golden paths only — unit/integration cover the
  exhaustive matrix.
