# Database & Migrations

## Layout

- SQLite migrations: `db/migrate/sqlite/NNNN_*.sql`
- Postgres migrations: `db/migrate/postgres/NNNN_*.sql`
- Numbering is global per dialect; never reuse or renumber after merge.

## Authoring

- Each migration file is one transaction. Don't mix schema and data changes in one file.
- Postgres parity: every SQLite schema change has a Postgres counterpart with matching number.
- `schema_migrations.filename` records the applied filename verbatim.

## Local workflow

```bash
uv run nyc-ai-scout migrate
```

## Code

- Database access lives in `api/repositories/` (split per aggregate).
- Connection helpers: `api.repositories.database`.
- Never hand-roll connections in controllers/services — go through the repository layer.
