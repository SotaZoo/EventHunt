# Pull request workflow

Use this file with `.ai/git.md` and `.ai/code-review.md`.

## Description

Every pull request should include:

- Summary.
- Testing.
- Deployment impact.
- Rollback notes for runtime or database changes.
- Screenshots or short screen recordings for visible UI changes.
- Loaded `.ai` modules.

## References

Use full URLs for external references.

Use relative paths for local files, for example:

- `deploy/README.md`
- `doc/development/mcp.md`

## Review routing

Route review by risk:

- Database: migrations, storage helpers, imports, exports, cleanup, and query
  behavior.
- Security: auth, OAuth, sessions, cookies, redirects, templates, logs,
  outbound HTTP, and secrets.
- Runtime: launchd, backups, Discord posting, scrapers, and
  manual deploy steps.
- UI: templates, forms, navigation, tables, status text, and accessibility.
- Dependencies: `pyproject.toml`, `uv.lock`, Docker, and new
  external services.

## Size

Prefer focused pull requests. Split work when review requires unrelated mental
models, such as schema plus UI plus deploy automation.
