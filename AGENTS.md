# Event Hunt project guidelines

Start with [`.ai/AGENTS.md`](.ai/AGENTS.md) and the nearest directory
`AGENTS.md` for the files you change. See [Contributing](CONTRIBUTING.md)
for local setup and verification.

- Keep the Python backend in `api/`, the web client in `web/`, and browser
  journeys in `e2e/`.
- Add versioned SQLite and PostgreSQL migrations under `db/migrate/`.
- Put developer, operator, and design documentation in `doc/`.
- Use synthetic fixtures in tests. Keep credentials, local databases,
  backups, and user records outside source control.
