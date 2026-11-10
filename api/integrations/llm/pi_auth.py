"""pi-auth credential lookup and Anthropic scoring adapter.

The pi-auth daemon handles OAuth refresh centrally. The current control-plane
source of truth is ``~/.pi/authd/state.json``; access tokens are retrieved
through ``pi-auth token anthropic/default --json`` so grant checks stay inside
pi-auth and refresh tokens never enter this process. The older
``~/.pi/auth.json`` access cache is still accepted for compatibility.

Failure modes (per design E5 / failure-modes table rows 16-18):

- 18 ``JSONDecodeError`` mid-write of ``auth.json`` - retry once after 100ms.
- expired-token / daemon-down - raise ``PiAuthError``; caller surfaces a
  one-time system-channel Discord alert.
- ``~/.pi/auth.json`` missing - use the central pi-auth token command.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Literal

from . import CURRENT_SCORING_PROMPT, EventFit
from .cli_fallback import CliFallbackError

AUTH_FILE = Path.home() / ".pi" / "auth.json"
DEFAULT_ACCOUNT_REF = "anthropic/default"
DEFAULT_PI_AUTH_TIMEOUT_SEC = 10
_RETRY_DELAY_SEC = 0.1

CredentialKind = Literal["oauth", "api_key"]


class PiAuthError(CliFallbackError):
    """Raised when pi-auth credentials are missing, malformed, or expired."""


@dataclass(frozen=True)
class AnthropicCredential:
    kind: CredentialKind
    value: str


def _read_auth_file(path: Path = AUTH_FILE) -> dict[str, Any]:
    """Read and parse ``auth.json``. Retries once on JSONDecodeError (E18 —
    daemon may be writing the file when we read it)."""
    raw = path.read_text()
    try:
        return json.loads(raw)
    except JSONDecodeError:
        time.sleep(_RETRY_DELAY_SEC)
        return json.loads(path.read_text())


def _pi_auth_path() -> str:
    if explicit := os.environ.get("PI_AUTH_CLI_PATH", "").strip():
        return explicit
    found = shutil.which("pi-auth")
    if not found:
        raise PiAuthError(
            "pi-auth CLI not on PATH. Install it or set PI_AUTH_CLI_PATH=/path/to/pi-auth."
        )
    return found


def _account_ref() -> str:
    return os.environ.get("PI_AUTH_ANTHROPIC_ACCOUNT", DEFAULT_ACCOUNT_REF).strip() or DEFAULT_ACCOUNT_REF


def _credential_from_pi_auth_cli(
    *,
    account_ref: str | None = None,
    timeout: int = DEFAULT_PI_AUTH_TIMEOUT_SEC,
) -> AnthropicCredential:
    ref = account_ref or _account_ref()
    try:
        proc = subprocess.run(
            [_pi_auth_path(), "token", ref, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired as exc:
        raise PiAuthError(f"pi-auth token timed out after {timeout}s") from exc
    except OSError as exc:
        raise PiAuthError(f"pi-auth token failed to start: {type(exc).__name__}") from exc

    if proc.returncode != 0:
        # Errors contain repair guidance but not tokens. Keep the cap anyway.
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise PiAuthError(f"pi-auth token {ref} failed: {detail or '<empty output>'}")

    try:
        payload = json.loads(proc.stdout)
    except JSONDecodeError as exc:
        raise PiAuthError("pi-auth token returned invalid JSON") from exc

    token = payload.get("access_token")
    token_type = payload.get("token_type")
    if token_type != "Bearer" or not isinstance(token, str) or not token.strip():
        raise PiAuthError("pi-auth token returned no bearer access token")
    return AnthropicCredential(kind="oauth", value=token)


def get_anthropic_credential(*, auth_file: Path | None = None) -> AnthropicCredential:
    """Return Anthropic credentials from env, legacy cache, or central pi-auth.

    Resolution order (matches pi-auth client priority):

    1. ``ANTHROPIC_OAUTH_TOKEN`` env var.
    2. ``ANTHROPIC_API_KEY`` env var (paid-API users).
    3. ``auth_file`` → ``anthropic`` entry → ``access`` (OAuth) or ``key``
       (API).
    4. ``pi-auth token anthropic/default --json``.

    Raises ``PiAuthError`` for: missing file, missing entry, expired token,
    command failure, or persistent JSONDecodeError after one retry.
    """
    if tok := os.environ.get("ANTHROPIC_OAUTH_TOKEN"):
        return AnthropicCredential(kind="oauth", value=tok)
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicCredential(kind="api_key", value=key)

    auth_file = AUTH_FILE if auth_file is None else auth_file
    if not auth_file.exists():
        if auth_file == AUTH_FILE:
            return _credential_from_pi_auth_cli()
        raise PiAuthError(
            f"{auth_file} missing. Run: pi-auth install && pi-auth login"
        )

    try:
        data = _read_auth_file(auth_file)
    except JSONDecodeError as e:
        raise PiAuthError(
            f"{auth_file} unreadable after retry: {e}. "
            f"Daemon may be mid-refresh — retry, or run: pi-auth status"
        ) from e

    cred = data.get("anthropic")
    if not cred:
        raise PiAuthError(
            f'No "anthropic" entry in {auth_file} — run: pi-auth login'
        )

    cred_type = cred.get("type")
    if cred_type == "api_key":
        key = cred.get("key")
        if not key:
            raise PiAuthError(f'{auth_file} anthropic.type=api_key but "key" missing')
        kind: CredentialKind = (
            "oauth" if cred.get("source") == "pi-auth" else "api_key"
        )
        return AnthropicCredential(kind=kind, value=key)

    if cred_type == "oauth":
        # Daemon should keep this fresh. If expired, daemon isn't running.
        expires_ms = cred.get("expires")
        access = cred.get("access")
        if not access:
            raise PiAuthError(f'{auth_file} anthropic.type=oauth but "access" missing')
        if expires_ms is None:
            raise PiAuthError(f'{auth_file} anthropic.type=oauth but "expires" missing')
        if expires_ms < time.time() * 1000:
            raise PiAuthError(
                "pi-auth token expired. Is the daemon running? Check: pi-auth status"
            )
        return AnthropicCredential(kind="oauth", value=access)

    raise PiAuthError(
        f'{auth_file} anthropic.type={cred_type!r} not understood (expected "oauth" or "api_key")'
    )


def get_anthropic_oauth_token(*, auth_file: Path | None = None) -> str:
    """Compatibility wrapper returning only the credential value."""
    return get_anthropic_credential(auth_file=auth_file).value


def _auth_headers_for_credential(credential: AnthropicCredential) -> dict[str, str]:
    from .agent import api_key_auth_headers, bearer_auth_headers

    if credential.kind == "api_key":
        return api_key_auth_headers(credential.value)
    return bearer_auth_headers(credential.value)


def score_batch_sync(*args: Any, **kwargs: Any) -> list[EventFit]:
    """Synchronous wrapper for callers that cannot await."""
    return asyncio.run(score_batch(*args, **kwargs))


async def score_batch(
    events: list[dict[str, Any]],
    *,
    taste_profile: str,
    today: str,
    prompt_version: str = CURRENT_SCORING_PROMPT,
    timeout: int = 180,
    model: str | None = None,
) -> list[EventFit]:
    """Score events through Anthropic using pi-auth-managed credentials."""
    from .agent import score_batch as agent_score_batch

    credential = get_anthropic_credential()
    return await agent_score_batch(
        events,
        taste_profile=taste_profile,
        today=today,
        prompt_version=prompt_version,
        timeout=timeout,
        model=model,
        auth_headers=_auth_headers_for_credential(credential),
    )


__all__ = [
    "AUTH_FILE",
    "DEFAULT_ACCOUNT_REF",
    "AnthropicCredential",
    "PiAuthError",
    "get_anthropic_credential",
    "get_anthropic_oauth_token",
    "score_batch",
    "score_batch_sync",
]
