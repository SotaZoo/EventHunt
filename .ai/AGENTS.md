# AI Agent Router

Start here for every task.

1. Read the user request and identify touched surfaces.
2. Load only the relevant modules below.
3. Load the nearest directory `AGENTS.md` for every file you edit.
4. Follow `CONTRIBUTING.md` and `.github/PULL_REQUEST_TEMPLATE.md` for review evidence.

## Modules

- Git and branch safety: `.ai/git.md`
- Product process and Scrum workflow: `.ai/product-process.md`
- Planning and PR flow: `.ai/merge-requests.md`
- Review standards: `.ai/code-review.md`
- Database and migrations: `.ai/database.md`
- Tests and QA: `.ai/testing.md`
- Code style and dependencies: `.ai/code-style.md`
- CI/CD: `.ai/ci-cd.md`
- Security, auth, and secrets: `.ai/security.md`
- Runtime and operations: `.ai/runtime.md`
- LLM and prompt behavior: `.ai/ai.md`

## Repo shape (non-negotiable)

- Backend: `api/` only (never `src/`, `core/`, `worker/`)
- Frontend: `web/` only
- Browser e2e: `e2e/` only
- Tests: `api/tests/{unit_tests,integration_tests,fixtures}` only (no root `tests/`)
- Migrations: `db/migrate/{sqlite,postgres}/` only
- Current docs: `doc/{administration,design,development}/` only
- Local scratch: `scratch/` only (untracked; never canonical project state)
- No in-app `/admin`. Operator/admin work stays CLI/runtime-only.

## Product workflow

Use GitHub issues and pull requests for product changes and review.
See [product changes](product-process.md) for the expected evidence.
