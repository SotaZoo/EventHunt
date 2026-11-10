---
name: nyc-ai-events-coding-principles
description: Load the project AI router and the relevant module before planning, implementing, or reviewing nyc-ai-events changes.
---

# NYC AI Events Coding Principles

Always begin by loading [`.ai/AGENTS.md`](../../../.ai/AGENTS.md). It routes
you to the right module based on the surfaces you touch. Then load the nearest
directory `AGENTS.md` for every file you edit.

Repo shape (non-negotiable):

- Backend: `api/`
- Frontend: `web/`
- Browser e2e: `e2e/`
- Tests: `api/tests/{unit_tests,integration_tests,fixtures}`
- Migrations: `db/migrate/{sqlite,postgres}/`
- Docs: `doc/{administration,design,development}/`
- No in-app `/admin`. Operator/admin work stays CLI-only.
