# API Agent Notes

Before editing backend code, read [`.ai/AGENTS.md`](../.ai/AGENTS.md), then load:

- `.ai/code-style.md`
- `.ai/testing.md`
- `.ai/database.md` when repositories or migrations are touched
- `.ai/security.md` when auth, sessions, OAuth, user records, or secrets are touched
- `.ai/runtime.md` when tasks, schedule, deploy assumptions, or jobs are touched

Backend code lives in `api/`. Do not introduce root `src/`, root `core/`, or
root `worker/`. Tests live in `api/tests/{unit_tests,integration_tests,fixtures}`.
