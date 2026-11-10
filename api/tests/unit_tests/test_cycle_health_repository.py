from api.repositories import runtime_markers
from api.tasks.cleanup import CycleHealth, load_health, save_health


class PostgresConnection:
    dialect = "postgres"


def test_postgres_cycle_health_uses_runtime_markers_repository(monkeypatch, tmp_path):
    conn = PostgresConnection()
    saved: list[dict[str, object]] = []
    monkeypatch.setattr(
        runtime_markers,
        "load_cycle_health_row",
        lambda _conn: {
            "consecutive_all_failed": 1,
            "last_error": "upstream",
            "last_failed_at": None,
            "last_success_at": "2026-09-22T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        runtime_markers,
        "save_cycle_health_row",
        lambda _conn, value: saved.append(value),
    )

    health = load_health(tmp_path, conn=conn)
    save_health(tmp_path, CycleHealth(consecutive_all_failed=2), conn=conn)

    assert health.last_error == "upstream"
    assert saved == [
        {
            "consecutive_all_failed": 2,
            "last_error": None,
            "last_failed_at": None,
            "last_success_at": None,
        }
    ]
