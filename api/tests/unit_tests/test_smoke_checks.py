from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from api.commands import deploy as deploy_module
from api.commands.deploy import (
    EnvConfig,
    SmokeFailure,
    check_browser_surface,
    check_http_health,
    check_launchd_service,
    check_scoring_breaker,
    check_sqlite_migration_drift,
)
from api.repositories.migrate import migration_dir_for_dialect
from api.tests.fixtures.web_helpers import migrated_db


def _cfg(tmp_path: Path, db_path: Path | None = None) -> EnvConfig:
    return EnvConfig(
        name="staging",
        checkout=tmp_path,
        env_file=tmp_path / ".env.staging",
        db_path=db_path or tmp_path / "events.db",
        services=("com.yang.nyc-ai-events-staging-web",),
        smoke_services=("com.yang.nyc-ai-events-staging-web",),
        web_port=8848,
        requires_origin_ancestor_check=False,
        sync_extras=("dev",),
        log_files=(),
    )


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        payload: dict[str, object] | None = None,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body.encode() if body is not None else json.dumps(payload or {}).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_check_http_health_passes_on_ok_payload(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def opener(_url: str, timeout: int):
        assert timeout == 2
        return _FakeResponse(
            status=200,
            payload={"status": "ok", "db": "ok", "migrations": "current"},
        )

    check_http_health(cfg, opener=opener)


def test_check_browser_surface_passes_for_alpha_paths(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def opener(url: str, timeout: int):
        assert timeout == 2
        path = urlparse(url).path
        if path == "/events":
            return _FakeResponse(
                status=303,
                headers={"Location": "/login"},
                body="",
            )
        if path == "/login":
            return _FakeResponse(status=200, body="Log in with Google")
        if path in {"/mine", "/hidden", "/settings"}:
            return _FakeResponse(status=302, headers={"Location": "/login"}, body="")
        raise AssertionError(f"unexpected path: {path}")

    check_browser_surface(cfg, opener=opener)


def test_check_browser_surface_fails_when_anonymous_timeline_is_public(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)

    def opener(url: str, timeout: int):
        path = urlparse(url).path
        if path == "/events":
            return _FakeResponse(
                status=200,
                body=(
                    "<title>Events - Event Hunt</title>"
                    '<a class="action-link" href="/login">Sign up / Log in</a>'
                    '<a href="/mine">Mine</a>'
                ),
            )
        if path == "/login":
            return _FakeResponse(status=200, body="Log in with Google")
        if path in {"/mine", "/hidden", "/settings"}:
            return _FakeResponse(status=302, headers={"Location": "/login"}, body="")
        raise AssertionError(f"unexpected path: {path}")

    with pytest.raises(SmokeFailure, match="/events did not redirect"):
        check_browser_surface(cfg, opener=opener)


def test_check_browser_surface_can_skip_google_requirement_for_staging(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def opener(url: str, timeout: int):
        path = urlparse(url).path
        if path == "/events":
            return _FakeResponse(
                status=303,
                headers={"Location": "/login"},
                body="",
            )
        if path == "/login":
            return _FakeResponse(status=200, body="Event Hunt Google sign-in not configured")
        if path in {"/mine", "/hidden", "/settings"}:
            return _FakeResponse(status=302, headers={"Location": "/login"}, body="")
        raise AssertionError(f"unexpected path: {path}")

    check_browser_surface(cfg, opener=opener, require_google_login=False)


def test_check_http_health_fails_on_degraded_payload(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def opener(_url: str, timeout: int):
        return _FakeResponse(
            status=503,
            payload={"status": "degraded", "db": "error", "error": "OperationalError"},
        )

    with pytest.raises(SmokeFailure, match="healthz returned 503"):
        check_http_health(cfg, opener=opener)


def test_check_http_health_fails_on_timeout(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def opener(_url: str, timeout: int):
        raise TimeoutError("timed out")

    with pytest.raises(SmokeFailure, match="timed out"):
        check_http_health(cfg, opener=opener)


def test_check_launchd_service_passes_when_running() -> None:
    def run(cmd, **_kwargs):
        assert cmd == ["launchctl", "print", "gui/501/com.yang.nyc-ai-events-staging-web"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="state = running\npid = 123\nlast exit code = 0\n",
            stderr="",
        )

    check_launchd_service("com.yang.nyc-ai-events-staging-web", uid=501, run=run)


def test_check_launchd_service_passes_before_first_exit() -> None:
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="state = running\npid = 123\nlast exit code = (never exited)\n",
            stderr="",
        )

    check_launchd_service("com.yang.nyc-ai-events-staging-web", uid=501, run=run)


def test_check_launchd_service_passes_after_kickstart_sigterm() -> None:
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="state = running\npid = 456\nlast exit code = 143\n",
            stderr="",
        )

    check_launchd_service("com.yang.nyc-ai-events-staging-web", uid=501, run=run)


def test_check_launchd_service_fails_when_pid_is_zero() -> None:
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="state = running\npid = 0\nlast exit code = 0\n",
            stderr="",
        )

    with pytest.raises(SmokeFailure, match="pid is 0"):
        check_launchd_service("com.yang.nyc-ai-events-staging-web", uid=501, run=run)


def test_check_launchd_service_allows_scheduled_job_between_runs() -> None:
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "state = not running\n"
                "last exit code = 0\n"
                "event triggers = {\n"
                "  com.yang.nyc-ai-events.1 = {}\n"
                "}\n"
            ),
            stderr="",
        )

    check_launchd_service(
        "com.yang.nyc-ai-events",
        uid=501,
        allow_not_running=True,
        run=run,
    )


def test_check_launchd_service_rejects_scheduled_job_bad_last_exit() -> None:
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "state = not running\n"
                "last exit code = 2\n"
                "event triggers = {\n"
                "  com.yang.nyc-ai-events.1 = {}\n"
                "}\n"
            ),
            stderr="",
        )

    with pytest.raises(SmokeFailure, match="state = not running"):
        check_launchd_service(
            "com.yang.nyc-ai-events",
            uid=501,
            allow_not_running=True,
            run=run,
        )


def test_check_launchd_service_fails_on_nonzero_last_exit() -> None:
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="state = running\npid = 123\nlast exit code = 2\n",
            stderr="",
        )

    with pytest.raises(SmokeFailure, match="last exit code = 2"):
        check_launchd_service("com.yang.nyc-ai-events-staging-web", uid=501, run=run)


def test_check_sqlite_migration_drift_passes_for_migrated_db(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)

    check_sqlite_migration_drift(_cfg(tmp_path, db))


def test_check_sqlite_migration_drift_fails_when_schema_is_behind(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    latest = sorted(path.name for path in migration_dir_for_dialect("sqlite").glob("*.sql"))[-1]
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE filename = ?", (latest,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SmokeFailure, match="migration drift"):
        check_sqlite_migration_drift(_cfg(tmp_path, db))


def test_check_scoring_breaker_passes_when_marker_absent(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()

    check_scoring_breaker(_cfg(tmp_path))


def test_check_scoring_breaker_fails_when_marker_exists(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".scoring_disabled").write_text("all failed")

    with pytest.raises(SmokeFailure, match="scoring breaker engaged"):
        check_scoring_breaker(_cfg(tmp_path))


def _production_cfg_with_release(tmp_path: Path, version: str = "0.3.3") -> EnvConfig:
    cfg = EnvConfig(
        name="production",
        checkout=tmp_path,
        env_file=tmp_path / ".env.friends-alpha",
        db_path=tmp_path / "data/events.db",
        services=("com.yang.nyc-ai-events-web",),
        smoke_services=("com.yang.nyc-ai-events-web",),
        web_port=8847,
        requires_origin_ancestor_check=True,
        sync_extras=(),
        log_files=(),
    )
    metadata_path = tmp_path / "data/current-release.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(json.dumps({"name": "nyc-ai-events", "version": version}))
    return cfg


def test_check_release_identity_passes_for_matching_wheel_install(tmp_path: Path) -> None:
    cfg = _production_cfg_with_release(tmp_path)

    def run(cmd, **_kwargs):
        assert cmd[0] == str(cfg.checkout / ".venv/bin/python")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"version": "0.3.3", "editable": False}) + "\n",
            stderr="",
        )

    deploy_module.check_release_identity(cfg, run=run)


def test_check_release_identity_rejects_version_mismatch(tmp_path: Path) -> None:
    cfg = _production_cfg_with_release(tmp_path)

    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"version": "0.2.0", "editable": False}) + "\n",
            stderr="",
        )

    with pytest.raises(SmokeFailure, match=r"expected 0\.3\.3, found 0\.2\.0"):
        deploy_module.check_release_identity(cfg, run=run)


def test_check_release_identity_rejects_editable_install(tmp_path: Path) -> None:
    cfg = _production_cfg_with_release(tmp_path)

    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"version": "0.3.3", "editable": True}) + "\n",
            stderr="",
        )

    with pytest.raises(SmokeFailure, match="editable installation"):
        deploy_module.check_release_identity(cfg, run=run)


def test_check_release_identity_rejects_invalid_output_fields(tmp_path: Path) -> None:
    cfg = _production_cfg_with_release(tmp_path)

    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")

    with pytest.raises(SmokeFailure, match="invalid fields"):
        deploy_module.check_release_identity(cfg, run=run)


def test_check_release_identity_reports_missing_runtime_python(tmp_path: Path) -> None:
    cfg = _production_cfg_with_release(tmp_path)

    def run(cmd, **_kwargs):
        raise FileNotFoundError(cmd[0])

    with pytest.raises(SmokeFailure, match="could not start"):
        deploy_module.check_release_identity(cfg, run=run)


def test_check_release_identity_rejects_invalid_release_metadata(tmp_path: Path) -> None:
    cfg = _production_cfg_with_release(tmp_path)
    (cfg.checkout / "data/current-release.json").write_text("[]")

    with pytest.raises(SmokeFailure, match="invalid package identity"):
        deploy_module.check_release_identity(cfg)
