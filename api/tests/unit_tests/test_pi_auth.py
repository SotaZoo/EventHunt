"""Tests for ``api.integrations.llm.pi_auth``."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from api.integrations.llm import pi_auth as pi_auth_module
from api.integrations.llm.cli_fallback import CliFallbackError
from api.integrations.llm.pi_auth import (
    AnthropicCredential,
    PiAuthError,
    get_anthropic_credential,
    get_anthropic_oauth_token,
)


def test_pi_auth_error_uses_cycle_failure_boundary() -> None:
    assert issubclass(PiAuthError, CliFallbackError)


def _write_auth(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(payload))
    return p


def test_env_oauth_token_takes_priority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "env-tok")
    auth = _write_auth(tmp_path, {"anthropic": {"type": "oauth", "access": "file-tok",
                                                 "expires": int(time.time() * 1000) + 60_000}})
    assert get_anthropic_oauth_token(auth_file=auth) == "env-tok"
    assert get_anthropic_credential(auth_file=auth) == AnthropicCredential(
        kind="oauth", value="env-tok"
    )


def test_env_api_key_takes_priority_when_no_oauth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    auth = _write_auth(tmp_path, {"anthropic": {"type": "api_key", "key": "file-key"}})
    assert get_anthropic_oauth_token(auth_file=auth) == "env-key"
    assert get_anthropic_credential(auth_file=auth) == AnthropicCredential(
        kind="api_key", value="env-key"
    )


def test_oauth_from_auth_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = _write_auth(
        tmp_path,
        {"anthropic": {"type": "oauth", "access": "tok-123",
                       "expires": int(time.time() * 1000) + 60_000}},
    )
    assert get_anthropic_oauth_token(auth_file=auth) == "tok-123"
    assert get_anthropic_credential(auth_file=auth).kind == "oauth"


def test_api_key_from_auth_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = _write_auth(tmp_path, {"anthropic": {"type": "api_key", "key": "sk-abc"}})
    assert get_anthropic_oauth_token(auth_file=auth) == "sk-abc"
    assert get_anthropic_credential(auth_file=auth).kind == "api_key"


def test_materialized_pi_auth_file_is_bearer_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = _write_auth(
        tmp_path,
        {
            "anthropic": {
                "type": "api_key",
                "key": "access-token",
                "source": "pi-auth",
            }
        },
    )
    assert get_anthropic_credential(auth_file=auth) == AnthropicCredential(
        kind="oauth", value="access-token"
    )


def test_default_missing_file_reads_central_pi_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PI_AUTH_ANTHROPIC_ACCOUNT", "anthropic/default")
    monkeypatch.setattr(pi_auth_module, "AUTH_FILE", tmp_path / "missing-auth.json")
    monkeypatch.setattr("api.integrations.llm.pi_auth._pi_auth_path", lambda: "/bin/pi-auth")
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "provider": "anthropic",
                    "account": "default",
                    "token_type": "Bearer",
                    "access_token": "central-token",
                    "expires_at": "2026-05-20T06:25:16.327Z",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert get_anthropic_credential() == AnthropicCredential(
        kind="oauth", value="central-token"
    )
    assert captured["cmd"] == [
        "/bin/pi-auth", "token", "anthropic/default", "--json",
    ]
    assert captured["cwd"] == "/tmp"


def test_central_pi_auth_failure_raises_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(pi_auth_module, "AUTH_FILE", tmp_path / "missing-auth.json")
    monkeypatch.setattr("api.integrations.llm.pi_auth._pi_auth_path", lambda: "/bin/pi-auth")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="No grant for event-scout to anthropic/default.",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PiAuthError, match="No grant"):
        get_anthropic_credential()


def test_missing_file_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    missing = tmp_path / "nope.json"
    with pytest.raises(PiAuthError, match="missing"):
        get_anthropic_oauth_token(auth_file=missing)


def test_no_anthropic_entry_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = _write_auth(tmp_path, {"openai": {"type": "api_key", "key": "x"}})
    with pytest.raises(PiAuthError, match="anthropic"):
        get_anthropic_oauth_token(auth_file=auth)


def test_expired_oauth_token_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = _write_auth(
        tmp_path,
        {"anthropic": {"type": "oauth", "access": "stale",
                       "expires": int(time.time() * 1000) - 1_000}},
    )
    with pytest.raises(PiAuthError, match="expired"):
        get_anthropic_oauth_token(auth_file=auth)


def test_jsondecodeerror_retry_eventually_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """E18 — partial JSON during pi-auth refresh; retry once after 100ms."""
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = tmp_path / "auth.json"
    valid = json.dumps(
        {"anthropic": {"type": "oauth", "access": "ok",
                       "expires": int(time.time() * 1000) + 60_000}}
    )
    state = {"calls": 0}
    real_read = Path.read_text

    def flaky_read(self: Path) -> str:
        if self == auth:
            state["calls"] += 1
            if state["calls"] == 1:
                return "{not json"
            return valid
        return real_read(self)

    auth.write_text(valid)  # exists for the .exists() check
    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert get_anthropic_oauth_token(auth_file=auth) == "ok"
    assert state["calls"] == 2  # one failure + one retry


def test_jsondecodeerror_persistent_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = tmp_path / "auth.json"
    auth.write_text("{still broken")
    with pytest.raises(PiAuthError, match="unreadable"):
        get_anthropic_oauth_token(auth_file=auth)


def test_unknown_credential_type_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth = _write_auth(tmp_path, {"anthropic": {"type": "magic_beans"}})
    with pytest.raises(PiAuthError, match="not understood"):
        get_anthropic_oauth_token(auth_file=auth)
