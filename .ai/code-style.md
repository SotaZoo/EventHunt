# Code style

Apply these rules to Python, templates, scripts, and tests.

## Python

- Keep modules cohesive. Avoid turning `api/commands/main.py`, `api/app.py`, or
  `api/repositories/_storage.py` into catch-all files.
- Keep route handlers and CLI commands thin. Push state-changing behavior into
  focused functions or service modules.
- Keep SQL and database behavior inside `api/repositories/` or migration files.
- Prefer explicit types and simple data structures over clever dynamic behavior.
- Use project helpers before introducing a new abstraction or dependency.
- Avoid shelling out when a Python API is available.
- When subprocesses are necessary, pass arguments as arrays and never interpolate
  untrusted input into shell strings.
- Keep exceptions specific when the caller needs different recovery behavior.
- Let unexpected runtime failures surface to logs instead of returning silent
  success.
- Avoid broad refactors while changing behavior.

## Web templates

- Keep templates display-oriented.
- Do not hide authorization, persistence, or business logic in templates.
- Escape or sanitize user-controlled content before rendering.
- Prefer labels, links, buttons, and form controls with semantic HTML.
- Use accessible names for controls.
- Keep empty, error, and denied states visible.

## Dependencies

- Add dependencies only when they remove meaningful complexity.
- Update lock files with dependency changes.
- Document why a new service, dependency, or external API is needed.
- Review dependency licenses and secret-handling implications before merging.
