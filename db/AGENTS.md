# DB Agent Notes

Before changing migrations, read [`.ai/AGENTS.md`](../.ai/AGENTS.md), then load:

- `.ai/database.md`
- `.ai/testing.md`

Migrations live under `db/migrate/{sqlite,postgres}/`. Number files globally
per dialect; never reuse or renumber after merge. Every SQLite schema change
needs a Postgres counterpart with a matching number.
