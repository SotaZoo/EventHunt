# E2E Agent Notes

Before editing browser tests, read [`.ai/AGENTS.md`](../.ai/AGENTS.md), then load:

- `.ai/testing.md`

Browser workflow tests live in `e2e/`. Features go in `e2e/features/`,
step definitions in `e2e/support/steps.ts`. The test base URL comes from
`E2E_BASE_URL` (defaults to `http://127.0.0.1:8000`).
