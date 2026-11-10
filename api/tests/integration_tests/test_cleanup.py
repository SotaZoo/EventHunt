from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from api.commands.main import app
from api.repositories.event_states import set_user_event_status
from api.repositories.events import connect
from api.repositories.migrate import apply_pending
from api.repositories.users import create_user

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"
runner = CliRunner()


def test_cleanup_deletes_old_posted(tmp_path: Path, monkeypatch):
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    c = connect(db)
    apply_pending(c, MIG_DIR)
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    c.execute(
        """INSERT INTO events (source, url, title, start_at, posted_at,
                               first_seen, last_seen, fit_input_hash, scoring_status)
           VALUES ('luma', 'https://lu.ma/old', 'old', ?, ?, ?, ?, 'h1', 'scored'),
                  ('luma', 'https://lu.ma/fresh', 'fresh', ?, ?, ?, ?, 'h2', 'scored'),
                  ('luma', 'https://lu.ma/unposted', 'unp', ?, NULL, ?, ?, 'h3', 'pending')
        """,
        (old, old, old, old,
         fresh, fresh, fresh, fresh,
         old, old, old),
    )
    c.close()
    result = runner.invoke(app, ["cleanup"])
    assert result.exit_code == 0
    c = connect(db)
    urls = {r["url"] for r in c.execute("SELECT url FROM events").fetchall()}
    assert urls == {"https://lu.ma/fresh", "https://lu.ma/unposted"}


def test_cleanup_preserves_events_with_user_state(tmp_path: Path, monkeypatch):
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    c = connect(db)
    apply_pending(c, MIG_DIR)
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    cur = c.execute(
        """INSERT INTO events (source, url, title, start_at, posted_at,
                               first_seen, last_seen, fit_input_hash, scoring_status)
           VALUES ('luma', 'https://lu.ma/saved-old', 'saved old', ?, ?, ?, ?, 'h1', 'scored'),
                  ('luma', 'https://lu.ma/old', 'old', ?, ?, ?, ?, 'h2', 'scored')
        """,
        (old, old, old, old, old, old, old, old),
    )
    saved_event_id = cur.lastrowid - 1
    user = create_user(
        c,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    set_user_event_status(c, user["id"], saved_event_id, "saved")
    c.close()

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    c = connect(db)
    urls = {r["url"] for r in c.execute("SELECT url FROM events").fetchall()}
    assert urls == {"https://lu.ma/saved-old"}
