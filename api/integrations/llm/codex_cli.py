"""Subprocess wrapper around ``codex exec`` for event scoring.

This is the local OpenAI/Codex replacement for the Claude CLI scorer. It keeps
the same prompt rendering and ``EventFit`` parser contract as the existing
fallback adapter:

- passes the prompt via stdin, never argv,
- asks Codex for a final JSON array and parses that final message,
- filters hallucinated ``event_id`` values in the shared parser,
- defaults to the cost-sensitive GPT-5.6 Codex CLI model validated here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from . import CURRENT_SCORING_PROMPT, EventFit
from .cli_fallback import (
    DEFAULT_TIMEOUT_SEC,
    CliFallbackError,
    _parse_fits,
    prompt_path_for_version,
    render_prompt,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "low"
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}


class CodexCliError(CliFallbackError):
    """Raised when the Codex CLI subprocess fails or returns invalid output."""


def _codex_path() -> str:
    if explicit := os.environ.get("CODEX_CLI_PATH"):
        return explicit
    found = shutil.which("codex")
    if not found:
        raise CodexCliError(
            "codex CLI not on PATH. Install it (https://developers.openai.com/codex/cli) "
            "or set CODEX_CLI_PATH=/path/to/codex."
        )
    return found


def _log_empty_failure_diagnostics(*, prompt_length: int) -> None:
    """Log Codex CLI version + path + prompt length for empty crash cases.

    Never logs the prompt body; event titles and taste profiles may contain
    private friend data.
    """
    try:
        resolved = _codex_path()
    except CodexCliError:
        resolved = "<not on PATH>"
    real_path = resolved
    with contextlib.suppress(OSError):
        real_path = str(Path(resolved).resolve())
    version = "<unknown>"
    try:
        ver_proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd="/tmp",
        )
        version = (ver_proc.stdout or ver_proc.stderr or "").strip()[:200] or "<empty>"
    except (subprocess.TimeoutExpired, OSError) as exc:
        version = f"<probe failed: {type(exc).__name__}>"
    log.warning(
        "codex CLI empty-failure diagnostics: path=%s resolved=%s version=%s prompt_chars=%d",
        resolved, real_path, version, prompt_length,
    )


def _run_codex_subprocess(
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> str:
    """Spawn ``codex exec`` and return the final assistant message.

    ``codex exec`` prints progress to stderr and the final agent message to
    stdout. We keep the run read-only and ephemeral because scoring should not
    inspect or mutate the repo; the prompt already contains every event field
    the model should see.
    """
    with tempfile.TemporaryDirectory(prefix="nyc-ai-codex-") as tmp_dir:
        final_message_path = Path(tmp_dir) / "final-message.txt"
        cmd = [
            _codex_path(),
            "--ask-for-approval",
            "never",
            "exec",
            # Scoring needs account auth, not the user's MCP/plugin config.
            "--ignore-user-config",
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            str(final_message_path),
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired as e:
            raise CodexCliError(f"codex CLI timeout after {timeout}s: {e}") from e

        final_message = (
            final_message_path.read_text() if final_message_path.exists() else ""
        )

    stdout = proc.stdout or ""
    stderr = (proc.stderr or "")[:400]
    signal = f"{stdout[:400]}\n{stderr}\n{final_message[:400]}".lower()
    if proc.returncode != 0 and ("rate limit" in signal or "rate_limit" in signal or "usage limit" in signal):
        raise CodexCliError(f"codex CLI rate/usage limit hit: {(stderr or stdout[:400])!r}")
    if proc.returncode != 0 and ("not logged in" in signal or "login" in signal or "authentication" in signal):
        raise CodexCliError(
            f"codex CLI not logged in: {(stderr or stdout[:400])!r}. Run: codex login"
        )
    if proc.returncode != 0:
        detail = stderr or stdout[:400] or final_message[:400] or "<empty stdout/stderr>"
        if not stdout.strip() and not stderr.strip() and not final_message.strip():
            _log_empty_failure_diagnostics(prompt_length=len(prompt))
        raise CodexCliError(f"codex CLI exit {proc.returncode}: {detail!r}")
    text = final_message.strip() or stdout.strip()
    if not text:
        raise CodexCliError("codex CLI returned empty final message")
    return text


def _parse_codex_fits(text: str, *, valid_ids: set[int]) -> list[EventFit]:
    try:
        return _parse_fits(text, valid_ids=valid_ids)
    except CliFallbackError as first_error:
        last_error: CliFallbackError = first_error
        decoder = json.JSONDecoder()
        for start in range(len(text) - 1, -1, -1):
            if text[start] != "[":
                continue
            try:
                decoded, end = decoder.raw_decode(text[start:])
            except JSONDecodeError:
                continue
            if not isinstance(decoded, list):
                continue
            candidate = text[start:start + end]
            try:
                fits = _parse_fits(candidate, valid_ids=valid_ids)
            except CliFallbackError as exc:
                last_error = exc
                continue
            if {fit.event_id for fit in fits} == valid_ids:
                return fits
        raise CodexCliError(
            f"no complete EventFit JSON array in codex output: {last_error}"
        ) from last_error


def score_batch_sync(
    events: list[dict[str, Any]],
    *,
    taste_profile: str,
    today: str,
    prompt_version: str = CURRENT_SCORING_PROMPT,
    max_retries: int = 1,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> list[EventFit]:
    """Score ``events`` synchronously via the Codex CLI subprocess."""
    if not events:
        return []
    valid_ids = {e["id"] for e in events}
    if len(valid_ids) != len(events) or any(not isinstance(i, int) for i in valid_ids):
        raise CodexCliError("every event needs a unique integer 'id' field")

    prompt = render_prompt(
        events,
        taste_profile=taste_profile,
        today=today,
        template_path=prompt_path_for_version(prompt_version),
    )
    selected_model = os.environ.get("CODEX_MODEL", model).strip() or model
    selected_effort = os.environ.get("CODEX_REASONING_EFFORT", reasoning_effort).strip() or reasoning_effort
    if selected_effort not in REASONING_EFFORTS:
        raise CodexCliError(
            f"CODEX_REASONING_EFFORT={selected_effort!r} is not one of "
            f"{' | '.join(sorted(REASONING_EFFORTS))}"
        )

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            text = _run_codex_subprocess(
                prompt,
                timeout=timeout,
                model=selected_model,
                reasoning_effort=selected_effort,
            )
            return _parse_codex_fits(text, valid_ids=valid_ids)
        except CliFallbackError as e:
            log.warning(
                "codex score_batch_sync attempt %d failed: kind=%s",
                attempt + 1, type(e).__name__,
            )
            log.debug("codex score_batch_sync attempt %d detail", attempt + 1, exc_info=e)
            last_err = e
    assert last_err is not None
    raise last_err


async def score_batch(
    events: list[dict[str, Any]],
    *,
    taste_profile: str,
    today: str,
    prompt_version: str = CURRENT_SCORING_PROMPT,
    max_retries: int = 1,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> list[EventFit]:
    """Async shim — pushes the blocking subprocess off the event loop."""
    return await asyncio.to_thread(
        score_batch_sync,
        events,
        taste_profile=taste_profile,
        today=today,
        prompt_version=prompt_version,
        max_retries=max_retries,
        timeout=timeout,
        model=model,
        reasoning_effort=reasoning_effort,
    )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "CodexCliError",
    "score_batch",
    "score_batch_sync",
]
