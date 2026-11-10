"""Subprocess wrapper around ``claude -p`` (the Claude CLI's print mode).

This is the fallback path when pi-auth isn't installed (or
``CLAUDE_OAUTH_MODE=cli`` is set). The adapter:

- passes the prompt via **stdin**, never argv (E17 — argv is prompt-injectable
  via event titles),
- parses the CLI's JSON envelope (``{subtype, is_error, result, ...}``) and
  validates the inner ``result`` is a JSON list of ``EventFit``,
- filters out hallucinated ``event_id`` values (E15 — only echo events from the
  input batch),
- exposes ``score_batch_sync`` (the workhorse) and ``score_batch`` (an async
  shim that runs the sync path under ``asyncio.to_thread`` per E4).

Concurrency: the design caps CLI fallback at 1 concurrent call (rate-limit
window is account-wide). That's enforced by the caller, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from api.models.event_annotations import EventHighlight, EventUrgencyNote

from . import CURRENT_SCORING_PROMPT, EventFit

_EVENT_TYPE_VALUES = {"talk", "workshop", "hackathon", "demo", "networking", "other"}
_FOOD_VALUES = {"yes", "no", "unknown"}
_NOTABLE_PERSON_KEYS = {"name", "role_clause", "confidence"}

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 180
# Haiku is the right tool for fit-scoring: structured classification + short
# reasoning + 7 fields per event. Sonnet was previously the default, but it
# does extended thinking and streams output ~5-10x slower, which on real
# Luma batches (long descriptions, 7 fields per event) pushed 20-event
# scoring runs over the 180s subprocess timeout. Haiku finishes the same
# task in well under 20s and costs ~10x less. Override per call via
# ``model=`` kwarg if a specific eval needs the larger model.
DEFAULT_MODEL = "haiku"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts" / "scoring"
PROMPT_PATH = PROMPTS_DIR / "v1.md"


def prompt_path_for_version(version: str) -> Path:
    """Resolve ``prompts/scoring/<version>.md``. Raises if it doesn't exist —
    silently falling back to v1 would let `nyc-ai eval --prompt-version v9`
    write `results-v9.json` while actually scoring with v1."""
    if not version.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise CliFallbackError(f"prompt version {version!r} contains unsafe path chars")
    path = PROMPTS_DIR / f"{version}.md"
    if not path.exists():
        raise CliFallbackError(f"prompt template not found: {path}")
    return path


class CliFallbackError(RuntimeError):
    """Raised when the claude CLI subprocess fails or returns garbage."""


def _claude_path() -> str:
    if explicit := os.environ.get("CLAUDE_CLI_PATH"):
        return explicit
    found = shutil.which("claude")
    if not found:
        raise CliFallbackError(
            "claude CLI not on PATH. Install it (https://docs.claude.com/claude-code) "
            "or set CLAUDE_CLI_PATH=/path/to/claude."
        )
    return found


_TEMPLATE_CACHE: dict[Path, str] = {}


def _log_empty_failure_diagnostics(*, prompt_length: int) -> None:
    """Log claude CLI version + resolved binary path + prompt length at
    WARNING. Called only when the subprocess returned non-zero AND both
    stdout and stderr are empty — the rare "real crash" branch. We log
    this so the next 30-hour silent breaker mystery has actionable
    forensics. The prompt body itself is NEVER logged (CLAUDE.md:
    no friend data / no LLM payloads in logs)."""
    try:
        resolved = _claude_path()
    except CliFallbackError:
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
        "claude CLI empty-failure diagnostics: path=%s resolved=%s version=%s prompt_chars=%d",
        resolved, real_path, version, prompt_length,
    )


def render_prompt(
    events: list[dict[str, Any]],
    *,
    taste_profile: str,
    today: str,
    template_path: Path = PROMPT_PATH,
) -> str:
    """Substitute ``{today}``, ``{taste_profile}``, ``{events_json}`` into the
    frozen prompt template. Uses ``str.replace`` not ``str.format`` because the
    template contains literal JSON braces.

    The template is cached per path so a 6-batch eval doesn't hit disk 6 times
    for the same ~6KB file. Tests passing a custom ``template_path`` get their
    own cache entry; reuse across calls is automatic.
    """
    template = _TEMPLATE_CACHE.get(template_path)
    if template is None:
        template = template_path.read_text()
        _TEMPLATE_CACHE[template_path] = template
    events_json = json.dumps(events, ensure_ascii=False, indent=2)
    return (
        template.replace("{today}", today)
        .replace("{taste_profile}", taste_profile)
        .replace("{events_json}", events_json)
    )


def _run_claude_subprocess(
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    model: str = DEFAULT_MODEL,
) -> str:
    """Spawn ``claude -p --output-format json --model <model>``, send prompt
    via stdin, return the inner ``result`` string from the envelope.

    Raises ``CliFallbackError`` on timeout, non-zero exit, malformed envelope,
    or rate-limit signals."""
    cmd = [
        _claude_path(),
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,  # E17: stdin not argv
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd="/tmp",  # avoid project-scoped CLAUDE.md / hook leakage
        )
    except subprocess.TimeoutExpired as e:
        raise CliFallbackError(f"claude CLI timeout after {timeout}s: {e}") from e

    stderr = (proc.stderr or "")[:400]

    # The CLI emits operational errors (rate-limit, "Not logged in", billing,
    # API surge) on STDOUT as a structured envelope with ``is_error: true``,
    # paired with exit code 1 and *empty* stderr. Parse stdout first so the
    # real message is preserved; previously we read only stderr on non-zero
    # exit and the actual signal was discarded — which is how three transient
    # rate-limit hits on 2026-05-14 tripped the breaker invisibly.
    envelope: dict[str, Any] | None = None
    if proc.stdout:
        try:
            decoded = json.loads(proc.stdout)
            if isinstance(decoded, dict):
                envelope = decoded
        except JSONDecodeError:
            envelope = None

    envelope_err = ""
    if envelope is not None and envelope.get("is_error"):
        result = envelope.get("result")
        if isinstance(result, str):
            envelope_err = result[:400]

    signal = (envelope_err or stderr).lower()
    if "rate limit" in signal or "rate_limit" in signal:
        # Failure mode #19 — different signal from API 429. Surface, don't retry.
        raise CliFallbackError(
            f"claude CLI rate limit hit (5h window): {envelope_err or stderr!r}. Wait and retry."
        )
    if "login" in signal or "logged in" in signal:
        raise CliFallbackError(
            f"claude CLI not logged in: {envelope_err or stderr!r}. Run: claude /login"
        )
    if proc.returncode != 0 or envelope_err:
        detail = envelope_err or stderr or "<empty stdout/stderr>"
        # Truly-empty failure (real CLI crash, not a structured error
        # envelope): capture diagnostic context so the next mystery hit
        # isn't blind. Never log the prompt body — CLAUDE.md forbids
        # leaking friend data / LLM payloads. Prompt length only.
        if not envelope_err and not stderr.strip():
            _log_empty_failure_diagnostics(prompt_length=len(prompt))
        raise CliFallbackError(f"claude CLI exit {proc.returncode}: {detail!r}")

    if envelope is None:
        raise CliFallbackError(
            f"claude stdout not valid JSON envelope: {(proc.stdout or '')[:400]!r}"
        )
    if envelope.get("subtype") != "success":
        raise CliFallbackError(f"claude envelope error: {str(envelope)[:400]}")
    text = envelope.get("result")
    if not isinstance(text, str):
        raise CliFallbackError(f"claude envelope has no 'result' string: {envelope}")
    return text


def _extract_json_array(text: str) -> str:
    """Find the first top-level JSON array in ``text``. Strips ```json fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = cleaned.rstrip("`").rstrip()
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if not match:
        raise CliFallbackError(f"no JSON array in model output: {cleaned[:400]!r}")
    return match.group(0)


def _normalize_extraction_fields(raw: Any) -> Any:
    """Coerce v2 extraction fields (``notable_people``, ``event_type``,
    ``food_provided``) into shapes Pydantic will accept.

    The strict path is Pydantic on the whole array. But the v2 prompt added
    free-text-shaped fields that the model can mangle (a confidence as a
    string, an unknown event_type, a single missing key inside one of N
    notable_people entries). Hard-failing the whole batch on one of those is
    out of proportion to the cost of dropping the offending sub-entry — so
    this pre-pass:

    - clips ``event_type`` outside the 6-bucket taxonomy to ``"other"``,
    - clips ``food_provided`` outside ``{yes, no, unknown}`` to ``"unknown"``,
    - drops individual ``notable_people`` entries with missing keys or
      non-coercible ``confidence``, keeping the rest of the row intact,
    - leaves the v1 fields (``event_id``, ``fit_score``, ``urgency``, …)
      strictly typed so a malformed score still fails loudly.

    Operates in-place on the parsed list-of-dicts; pass it raw JSON only.
    """
    if not isinstance(raw, list):
        return raw
    for row in raw:
        if not isinstance(row, dict):
            continue

        # Optional v3 card facts fail independently; a malformed annotation
        # must not turn an otherwise valid rank into a failed scoring batch.
        for key, model in (("highlight", EventHighlight), ("urgency_note", EventUrgencyNote)):
            if row.get(key) is not None:
                try:
                    row[key] = model.model_validate(row[key]).model_dump(mode="json")
                except ValidationError:
                    row[key] = None

        et = row.get("event_type")
        if et is not None:
            row["event_type"] = et if et in _EVENT_TYPE_VALUES else "other"

        fp = row.get("food_provided")
        if fp is not None:
            row["food_provided"] = fp if fp in _FOOD_VALUES else "unknown"

        people = row.get("notable_people")
        if people is None:
            continue
        if not isinstance(people, list):
            row["notable_people"] = []
            continue

        cleaned: list[dict[str, Any]] = []
        for entry in people:
            if not isinstance(entry, dict):
                continue
            if not _NOTABLE_PERSON_KEYS.issubset(entry.keys()):
                continue
            try:
                conf = float(entry["confidence"])
            except (TypeError, ValueError):
                continue
            if not (0.0 <= conf <= 1.0):
                continue
            name = entry["name"]
            role = entry["role_clause"]
            if not isinstance(name, str) or not isinstance(role, str):
                continue
            if not name.strip() or not role.strip():
                continue
            cleaned.append(
                {
                    "name": name.strip(),
                    "role_clause": role.strip(),
                    "confidence": conf,
                }
            )
        row["notable_people"] = cleaned
    return raw


def _parse_fits(text: str, *, valid_ids: set[int]) -> list[EventFit]:
    array_text = _extract_json_array(text)
    try:
        decoded = json.loads(array_text)
    except JSONDecodeError as e:
        raise CliFallbackError(f"EventFit decode failed: {e}") from e
    normalized = _normalize_extraction_fields(decoded)
    try:
        fits = TypeAdapter(list[EventFit]).validate_python(normalized)
    except ValidationError as e:
        raise CliFallbackError(f"EventFit validation failed: {e}") from e

    # E15: drop hallucinated ids; do not let the LLM expand the batch.
    filtered = [f for f in fits if f.event_id in valid_ids]
    dropped = len(fits) - len(filtered)
    if dropped:
        log.warning("dropped %d EventFit rows with unknown event_id", dropped)
    return filtered


def score_batch_sync(
    events: list[dict[str, Any]],
    *,
    taste_profile: str,
    today: str,
    prompt_version: str = CURRENT_SCORING_PROMPT,
    max_retries: int = 1,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    model: str = DEFAULT_MODEL,
) -> list[EventFit]:
    """Score ``events`` synchronously via the claude CLI subprocess.

    ``events`` is a list of dicts. Each MUST have an integer ``id`` field;
    everything else (title, host, description, date, venue, …) is fed to the
    LLM as-is.

    ``prompt_version`` selects a frozen template under ``prompts/scoring/``.
    Unknown versions raise — silent fallback to v1 would let
    ``--prompt-version v9`` write ``results-v9.json`` containing v1 scores.
    """
    if not events:
        return []
    valid_ids = {e["id"] for e in events}
    if len(valid_ids) != len(events) or any(not isinstance(i, int) for i in valid_ids):
        raise CliFallbackError("every event needs a unique integer 'id' field")

    template_path = prompt_path_for_version(prompt_version)
    prompt = render_prompt(
        events, taste_profile=taste_profile, today=today, template_path=template_path
    )

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            text = _run_claude_subprocess(prompt, timeout=timeout, model=model)
            return _parse_fits(text, valid_ids=valid_ids)
        except CliFallbackError as e:
            # ``e`` can carry cleaned[:400] of the raw LLM body (titles +
            # notable-person names under v2). Surface only kind + attempt at
            # warning level; route the body to debug. CLAUDE.md: never log
            # friend data / LLM payloads.
            log.warning(
                "score_batch_sync attempt %d failed: kind=%s",
                attempt + 1, type(e).__name__,
            )
            log.debug("score_batch_sync attempt %d detail", attempt + 1, exc_info=e)
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
) -> list[EventFit]:
    """Async shim — pushes the blocking subprocess off the event loop (E4)."""
    return await asyncio.to_thread(
        score_batch_sync,
        events,
        taste_profile=taste_profile,
        today=today,
        prompt_version=prompt_version,
        max_retries=max_retries,
        timeout=timeout,
        model=model,
    )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SEC",
    "PROMPT_PATH",
    "CliFallbackError",
    "render_prompt",
    "score_batch",
    "score_batch_sync",
]
