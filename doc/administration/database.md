# Database

SQLite is the default for the local demo. Set `DATABASE_PATH` to choose
the file. For a shared catalog, configure `DATABASE_URL` with a PostgreSQL
connection string and give each runtime role the permissions it needs.

## Migrations

Apply the migration set for the configured dialect before starting the app
or worker.

```sh
uv run --locked nyc-ai-scout migrate
```

Versioned SQL lives in [db/migrate](../../db/migrate). Keep SQLite and
PostgreSQL migrations in sync. Use a disposable database when verifying
schema or repository changes.

## Backups

Use `nyc-ai-scout backup --help` for the SQLite backup command. Use your
PostgreSQL provider's backup and restore workflow for a shared catalog.
