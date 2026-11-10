# `.ai/` — Project OS for AI agents

This directory is the agent-facing source of truth for working in this repo.

## Entry point

Always start at [`.ai/AGENTS.md`](./AGENTS.md). It routes to the relevant module.

## Modules

| File | When to load |
|------|--------------|
| [`git.md`](./git.md) | Branching, commits, PR creation |
| [`product-process.md`](./product-process.md) | Product changes and issue routing |
| [`merge-requests.md`](./merge-requests.md) | PR descriptions and review routing |
| [`code-review.md`](./code-review.md) | Reviewer + author standards |
| [`database.md`](./database.md) | Migrations, repository layer |
| [`testing.md`](./testing.md) | Tests and QA |
| [`code-style.md`](./code-style.md) | Code style and dependencies |
| [`ci-cd.md`](./ci-cd.md) | Source verification and deployment |
| [`security.md`](./security.md) | Auth, sessions, secrets |
| [`runtime.md`](./runtime.md) | Background tasks, deploy, schedule |
| [`ai.md`](./ai.md) | LLM prompts and modes |

## Repo shape (non-negotiable)

- `api/` is the backend
- `web/` is the frontend
- `e2e/` is browser workflow tests
- `db/migrate/{sqlite,postgres}/` owns migrations
- `doc/{administration,design,development}/` owns current docs
- `scratch/` is an optional ignored local workspace for plans, design notes,
  and research notes; it is not project state
- No root `src/`, no root `tests/`, no `doc/plans|checkpoints|checklists`

## Local overrides

Create `AGENTS.local.md` for personal notes. Files under `.ai/local/` are
ignored by Git.
