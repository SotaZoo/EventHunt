# Testing & QA

## Where tests live

- Backend unit: `api/tests/unit_tests/`
- Backend integration: `api/tests/integration_tests/`
- Backend fixtures: `api/tests/fixtures/`
- Frontend: `web/__tests__/`
- Browser e2e: `e2e/features/`

## Commands

```bash
uv run pytest api/tests
npm --prefix web test
npm --prefix e2e test
```

## Authoring

- Write the failing test first (TDD). Commit the failing test in a separate
  step from the fix when the change is non-trivial.
- Integration tests must hit a real SQLite DB via `migrated_db()` — no mocks
  for the storage layer.
- Web tests use React Testing Library + Jest jsdom. No snapshot tests for
  dynamic UI; assert behavior, not markup.
- E2E tests must clean up their world (`After` hook).

## Before requesting review

- Full backend suite green: `uv run pytest api/tests`
- Frontend unit suite green: `npm --prefix web test`
- E2E green when the API server is up
