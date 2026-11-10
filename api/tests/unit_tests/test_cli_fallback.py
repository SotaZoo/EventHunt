"""Tests for ``api.integrations.llm.cli_fallback``.

The subprocess itself is mocked — we never invoke the real ``claude`` binary
in CI. Tests exercise prompt rendering, envelope parsing, hallucinated-id
filtering, retry on transient failure, and the rate-limit / login-required
error signals.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from api.integrations.llm import EventFit
from api.integrations.llm.cli_fallback import (
    CliFallbackError,
    _extract_json_array,
    _parse_fits,
    prompt_path_for_version,
    render_prompt,
    score_batch_sync,
)


def _envelope(result_text: str) -> str:
    return json.dumps({"subtype": "success", "is_error": False, "result": result_text})


def _make_completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


VALID_FITS_JSON = json.dumps(
    [
        {"event_id": 1, "fit_score": 0.9, "reasoning": "match", "urgency": "high",
         "urgency_reason": "in 2 days"},
        {"event_id": 2, "fit_score": 0.2, "reasoning": "off-topic", "urgency": "low",
         "urgency_reason": None},
    ]
)


def test_render_prompt_substitutes_placeholders(tmp_path) -> None:
    template = tmp_path / "v.md"
    template.write_text("today={today}\nprofile={taste_profile}\nevents={events_json}")
    out = render_prompt(
        [{"id": 1, "title": "x"}],
        taste_profile="loves agents",
        today="2026-04-25",
        template_path=template,
    )
    assert "today=2026-04-25" in out
    assert "loves agents" in out
    assert '"id": 1' in out
    assert '"title": "x"' in out


def test_extract_json_array_strips_fences() -> None:
    text = '```json\n[{"a": 1}]\n```'
    assert _extract_json_array(text) == '[{"a": 1}]'


def test_extract_json_array_finds_array_in_prose() -> None:
    text = "Here is the result:\n[{\"event_id\": 1}]\nDone."
    assert _extract_json_array(text) == '[{"event_id": 1}]'


def test_extract_json_array_raises_when_missing() -> None:
    with pytest.raises(CliFallbackError, match="no JSON array"):
        _extract_json_array("just prose, no array")


def test_parse_fits_drops_hallucinated_ids() -> None:
    text = json.dumps(
        [
            {"event_id": 1, "fit_score": 0.9, "reasoning": "ok", "urgency": "low"},
            {"event_id": 99, "fit_score": 0.1, "reasoning": "hallucinated", "urgency": "low"},
        ]
    )
    fits = _parse_fits(text, valid_ids={1, 2})
    assert [f.event_id for f in fits] == [1]


def test_parse_fits_validation_error_surfaces() -> None:
    bad = json.dumps([{"event_id": 1, "fit_score": 1.5, "reasoning": "x", "urgency": "low"}])
    with pytest.raises(CliFallbackError, match="validation"):
        _parse_fits(bad, valid_ids={1})


def test_parse_fits_v1_payload_still_parses() -> None:
    """v1-shaped JSON (no notable_people / event_type / food_provided)
    must still parse cleanly after the v2 normalizer was added — the
    extraction fields default to permissive empty / None values."""
    fits = _parse_fits(VALID_FITS_JSON, valid_ids={1, 2})
    assert [f.event_id for f in fits] == [1, 2]
    assert fits[0].notable_people == []
    assert fits[0].event_type is None
    assert fits[0].food_provided is None


def test_parse_fits_raises_on_undecodable_json() -> None:
    """The decode step happens before the normalizer; bad JSON must surface
    as CliFallbackError, not propagate as a JSONDecodeError."""
    with pytest.raises(CliFallbackError, match=r"decode|validation|JSON"):
        _parse_fits("[not json]", valid_ids={1})


def test_score_batch_sync_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, *, input, capture_output, text, timeout, check, cwd):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["cwd"] = cwd
        return _make_completed(_envelope(VALID_FITS_JSON))

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fits = score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-04-25",
    )
    assert isinstance(fits, list)
    assert all(isinstance(f, EventFit) for f in fits)
    assert [f.event_id for f in fits] == [1, 2]
    # E17: prompt sent via stdin, never argv. The cmd has no event titles.
    assert captured["input"] is not None and "A" in captured["input"]
    assert "A" not in " ".join(captured["cmd"])
    assert captured["cwd"] == "/tmp"


def test_score_batch_sync_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"n": 0}

    def fake_run(cmd, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return _make_completed("not json")
        return _make_completed(_envelope(VALID_FITS_JSON))

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fits = score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="p",
        today="2026-04-25",
        max_retries=1,
    )
    assert state["n"] == 2
    assert len(fits) == 2


def test_score_batch_sync_rate_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        return _make_completed("", returncode=1, stderr="Error: rate limit reached")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CliFallbackError, match="rate limit"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_login_required(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        return _make_completed("", returncode=1, stderr="Please run /login first")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CliFallbackError, match="not logged in"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


# Real-world CLI failure shape (Claude Code 2.1.x): structured error on stdout
# with is_error=true, exit code 1, *empty* stderr. The previous adapter only
# inspected stderr on non-zero exit and lost the entire diagnostic.
# Triggered the 2026-05-14 silent breaker trip — 30+ hours of no-op cycles.


def _error_envelope(result_text: str) -> str:
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": result_text,
    })


def test_score_batch_sync_login_on_stdout_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _error_envelope("Not logged in · Please run /login")

    def fake_run(cmd, **kw):
        return _make_completed(stdout, returncode=1, stderr="")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CliFallbackError, match="not logged in"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_rate_limit_on_stdout_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _error_envelope("Rate limit exceeded · resets at 19:00 UTC")

    def fake_run(cmd, **kw):
        return _make_completed(stdout, returncode=1, stderr="")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CliFallbackError, match="rate limit"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_exit1_empty_streams_surfaces_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If both stdout and stderr are truly empty (real crash), we still raise
    # with the exit code so the failure isn't silent. Used to be the *only*
    # observed signature for every CLI failure — now it's the rare last
    # resort after the stdout-envelope path has run.
    def fake_run(cmd, **kw):
        return _make_completed("", returncode=1, stderr="")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CliFallbackError, match=r"claude CLI exit 1"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_empty_failure_logs_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Real-crash branch (exit!=0, empty stdout+stderr) must emit a WARNING
    with the claude CLI path/version/prompt-length so the next mystery
    failure has forensics. The prompt body MUST NOT appear in the log —
    CLAUDE.md forbids logging friend data or LLM payloads."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        # First call: the real score subprocess (empty failure).
        if cmd[1:2] == ["-p"]:
            return _make_completed("", returncode=1, stderr="")
        # Second call: the --version probe inside the diagnostics helper.
        return _make_completed("claude-cli 2.1.4\n", returncode=0, stderr="")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    secret_title = "Founders Dinner with [PRIVATE NAME]"
    with (
        caplog.at_level("WARNING", logger="api.integrations.llm.cli_fallback"),
        pytest.raises(CliFallbackError, match=r"claude CLI exit 1"),
    ):
        score_batch_sync(
            [{"id": 1, "title": secret_title}],
            taste_profile="loves agents",
            today="2026-04-25",
            max_retries=0,
        )

    diag_records = [r for r in caplog.records if "empty-failure diagnostics" in r.getMessage()]
    assert diag_records, "expected an empty-failure diagnostics WARNING"
    msg = diag_records[0].getMessage()
    assert "claude-cli 2.1.4" in msg or "version=" in msg
    assert "prompt_chars=" in msg
    # CLAUDE.md: never log friend data / LLM payloads.
    assert secret_title not in msg
    assert "loves agents" not in msg
    # And the --version probe actually ran.
    assert any(c[1:2] == ["--version"] for c in calls)


def test_score_batch_sync_envelope_error_does_not_trigger_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """If the CLI gave us a structured ``is_error: true`` envelope on stdout,
    we already have the error message — no need to probe --version."""
    stdout = json.dumps({"is_error": True, "result": "Subscription required"})

    def fake_run(cmd, **kw):
        return _make_completed(stdout, returncode=1, stderr="")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with (
        caplog.at_level("WARNING", logger="api.integrations.llm.cli_fallback"),
        pytest.raises(CliFallbackError, match="Subscription required"),
    ):
        score_batch_sync(
            [{"id": 1, "title": "A"}], taste_profile="p", today="2026-04-25",
            max_retries=0,
        )

    assert not [r for r in caplog.records if "empty-failure diagnostics" in r.getMessage()]


def test_score_batch_sync_generic_error_on_stdout_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Any other is_error message (billing, API surge, internal) flows through
    # the same path and gets surfaced verbatim instead of being collapsed to ''.
    stdout = _error_envelope("Subscription required · upgrade to continue")

    def fake_run(cmd, **kw):
        return _make_completed(stdout, returncode=1, stderr="")

    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CliFallbackError, match="Subscription required"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_empty_input() -> None:
    assert score_batch_sync([], taste_profile="p", today="2026-04-25") == []


def test_score_batch_sync_duplicate_ids_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.integrations.llm.cli_fallback._claude_path", lambda: "/bin/echo")
    with pytest.raises(CliFallbackError, match="unique integer"):
        score_batch_sync(
            [{"id": 1, "title": "A"}, {"id": 1, "title": "B"}],
            taste_profile="p",
            today="2026-04-25",
        )


def test_prompt_path_v1_resolves() -> None:
    """v1.md ships with the package — must exist for the M0 gate to run."""
    p = prompt_path_for_version("v1")
    assert p.name == "v1.md"
    assert p.exists()


def test_prompt_path_v2_resolves() -> None:
    """v2.md is the scout-bot MVP prompt with the extraction-extension
    fields. The cycle pipeline defaults to it; if it goes missing the
    cycle would silently fall back to a path that raises."""
    p = prompt_path_for_version("v2")
    assert p.name == "v2.md"
    assert p.exists()
    body = p.read_text()
    # All three new fields must be documented in the contract.
    assert "notable_people" in body
    assert "event_type" in body
    assert "food_provided" in body


def test_prompt_path_unknown_version_raises() -> None:
    with pytest.raises(CliFallbackError, match="not found"):
        prompt_path_for_version("v999")


def test_prompt_path_rejects_path_traversal() -> None:
    """`--prompt-version ../etc/passwd` must not escape prompts/scoring/."""
    with pytest.raises(CliFallbackError, match="unsafe"):
        prompt_path_for_version("../etc/passwd")
    with pytest.raises(CliFallbackError, match="unsafe"):
        prompt_path_for_version("a/b")
