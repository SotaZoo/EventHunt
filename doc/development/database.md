# Database (Development)

## Migrations

Add a numbered file to BOTH dialect dirs:

```text
db/migrate/sqlite/NNNN_<description>.sql
db/migrate/postgres/NNNN_<description>.sql
```

Apply locally:

```bash
uv run nyc-ai-scout migrate
```

Test with `api/tests/integration_tests/test_migrate.py` and the per-migration
file (e.g. `test_migration_0001.py`).

## Repository layer

Per-aggregate modules under `api/repositories/`. Add new aggregates as new
modules; do not bloat the existing ones. Connection helpers in
`api/repositories/database.py`.

## Test data

Use `api.tests.fixtures.web_helpers.migrated_db()` for an empty migrated
SQLite DB in a tmp dir. `add_user`, `add_scored_event` add representative
rows.

## Annotation migration

`0013_event_annotations.sql` adds nullable `highlight`, `urgency_note`,
`scored_at` and `source_content_checked_at` to events in both dialects. The first
two contain typed JSON with source evidence; scoring and source observation are
separate times. Old rows stay null, not guessed from legacy urgency. Existing
table-level worker grants cover the columns; admission/auth grants are unchanged.
Apply this additive migration before the new worker; rollback packages, not
columns. Effective scoring-input changes clear annotations alongside rank.
