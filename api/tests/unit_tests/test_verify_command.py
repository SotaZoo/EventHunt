"""Black-box checks for the repository verification entry point."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import verify

ROOT = Path(__file__).resolve().parents[3]
VERIFY = ROOT / "scripts" / "verify.py"


def _invoke(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_docs_check_writes_complete_success_receipt(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"

    result = _invoke(VERIFY, "--check", "docs", "--receipt", str(receipt))

    assert result.returncode == 0, result.stderr
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "passed"
    assert payload["checks"] == [{"name": "docs", "status": "passed"}]


def test_missing_required_file_fails_closed(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "verify.py"
    script.parent.mkdir()
    shutil.copy2(VERIFY, script)
    receipt = tmp_path / "receipt.json"

    result = _invoke(script, "--check", "docs", "--receipt", str(receipt))

    assert result.returncode != 0
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "failed"
    assert payload["checks"] == [{"name": "docs", "status": "failed"}]
    assert "Missing required files" in result.stderr


def test_invalid_source_sha_is_rejected(tmp_path: Path) -> None:
    result = _invoke(
        VERIFY,
        "--check",
        "docs",
        "--source-sha",
        "not-a-full-sha",
        "--receipt",
        str(tmp_path / "receipt.json"),
    )

    assert result.returncode != 0
    assert not (tmp_path / "receipt.json").exists()


def test_fixture_commands_do_not_inherit_account_credentials(monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_AGENT_API_TOKEN", "not-a-test-token")
    monkeypatch.setenv("SUPABASE_URL", "https://private.invalid")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://private.invalid/hook")
    monkeypatch.setenv("PYTHONPATH", "/private/other-project")

    env = verify._base_env()

    assert "PATH" in env
    assert "NYC_AI_EVENTS_AGENT_API_TOKEN" not in env
    assert "SUPABASE_URL" not in env
    assert "DISCORD_WEBHOOK_URL" not in env
    assert env["PYTHONPATH"] == str(verify.ROOT)


def test_offline_prepare_never_builds_project_before_checks(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(verify, "_command", lambda args, **_kwargs: commands.append(args))

    verify._prepare(offline=True)

    assert commands[0] == [
        "uv", "sync", "--extra", "dev", "--locked", "--python", "3.11",
        "--offline", "--no-install-project", "--no-build",
    ]
    assert all("--offline" in command for command in commands[:-1])
    assert commands[-1] == ["npm", "--prefix", "web", "run", "build:discovery"]


def test_offline_python_checks_use_prepared_environment(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(verify, "_command", lambda args, **_kwargs: commands.append(args))

    verify._lint(offline=True)
    verify._backend(offline=True)

    assert commands[0][:4] == ["uv", "run", "--offline", "--no-sync"]
    assert commands[1][:4] == ["uv", "run", "--offline", "--no-sync"]
    assert commands[2] == ["npm", "--prefix", "web", "run", "build:discovery"]
    assert commands[3] == ["uv", "build", "--no-build-isolation", "--offline"]
