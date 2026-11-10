"""Tests for ``api.integrations.llm.codex_cli``.

The subprocess is mocked. These tests pin the Codex command shape, stdin-only
prompt handling, model default, and common operational error mapping.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from api.integrations.llm import EventFit
from api.integrations.llm.codex_cli import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    CodexCliError,
    score_batch_sync,
)

VALID_FITS_JSON = json.dumps(
    [
        {"event_id": 1, "fit_score": 0.9, "reasoning": "match", "urgency": "high",
         "urgency_reason": "in 2 days"},
        {"event_id": 2, "fit_score": 0.2, "reasoning": "off-topic", "urgency": "low",
         "urgency_reason": None},
    ]
)


def test_successful_scoring_can_describe_authentication_without_being_a_login_error(monkeypatch):
    response = json.dumps([{
        "event_id": 1, "fit_score": .91, "reasoning": "AI authentication and agent security",
        "urgency": "low", "urgency_reason": None,
    }])
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _make_completed(response))
    fits = score_batch_sync([{"id": 1, "title": "Agent security"}],
                            taste_profile="AI engineering", today="2026-10-06", max_retries=0)
    assert fits[0].reasoning == "AI authentication and agent security"


def test_malformed_optional_annotations_do_not_discard_a_valid_score(monkeypatch):
    response = json.dumps([{
        "event_id": 1, "fit_score": .91, "reasoning": "Shared fit", "urgency": "low",
        "highlight": {"text": "", "evidence": "source"},
        "urgency_note": {"kind": "event_date", "text": "Tomorrow", "evidence": "Tomorrow"},
    }])
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _make_completed(response))
    fit = score_batch_sync([{"id": 1, "title": "Agent security"}],
                          taste_profile="AI engineering", today="2026-10-06", max_retries=0)[0]
    assert fit.fit_score == .91
    assert fit.highlight is None
    assert fit.urgency_note is None


def test_default_model_is_current_cost_sensitive_codex_model() -> None:
    assert DEFAULT_MODEL == "gpt-5.6-luna"


def _make_completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_score_batch_sync_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, *, input, capture_output, text, timeout, check, cwd):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["cwd"] = cwd
        return _make_completed(VALID_FITS_JSON)

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fits = score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-04-25",
    )

    assert isinstance(fits, list)
    assert all(isinstance(f, EventFit) for f in fits)
    assert [f.event_id for f in fits] == [1, 2]
    assert captured["cmd"][:4] == ["/bin/codex", "--ask-for-approval", "never", "exec"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == DEFAULT_MODEL
    assert captured["cmd"][captured["cmd"].index("-c") + 1] == (
        f'model_reasoning_effort="{DEFAULT_REASONING_EFFORT}"'
    )
    assert "--output-last-message" in captured["cmd"]
    assert "--skip-git-repo-check" in captured["cmd"]
    assert "--ignore-user-config" in captured["cmd"]
    assert captured["cmd"][-1] == "-"
    assert captured["input"] is not None and "A" in captured["input"]
    assert "A" not in " ".join(captured["cmd"])
    assert captured["cwd"] == "/tmp"


def test_score_batch_sync_reads_codex_output_last_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        out_path = Path(cmd[cmd.index("--output-last-message") + 1])
        out_path.write_text(VALID_FITS_JSON)
        return _make_completed("transcript that should be ignored")

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fits = score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-06-02",
        max_retries=0,
    )

    assert "--output-last-message" in captured["cmd"]
    assert [f.event_id for f in fits] == [1, 2]


def test_score_batch_sync_extracts_last_valid_eventfit_array_from_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_array = json.dumps([{"id": 1, "title": "Prompt event"}])
    transcript = (
        "codex transcript\n"
        f"user prompt included events: {prompt_array}\n"
        "assistant final answer:\n"
        f"{VALID_FITS_JSON}\n"
        "tokens used 123"
    )

    def fake_run(cmd, **_kw):
        return _make_completed(transcript)

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fits = score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-06-02",
        max_retries=0,
    )

    assert [f.event_id for f in fits] == [1, 2]


def test_score_batch_sync_rejects_transcript_without_eventfit_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, **_kw):
        return _make_completed('user events: [{"id": 1, "title": "A"}]')

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CodexCliError, match="EventFit"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="profile",
            today="2026-06-02",
            max_retries=0,
        )


def test_score_batch_sync_uses_codex_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _make_completed(VALID_FITS_JSON)

    monkeypatch.setenv("CODEX_MODEL", "gpt-5.6-terra")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-04-25",
    )

    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "gpt-5.6-terra"


def test_score_batch_sync_uses_codex_reasoning_effort_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _make_completed(VALID_FITS_JSON)

    monkeypatch.setenv("CODEX_REASONING_EFFORT", "medium")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-04-25",
    )

    assert captured["cmd"][captured["cmd"].index("-c") + 1] == (
        'model_reasoning_effort="medium"'
    )


def test_score_batch_sync_rejects_invalid_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "maximum")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")

    with pytest.raises(CodexCliError, match="CODEX_REASONING_EFFORT"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
        )


def test_score_batch_sync_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"n": 0}

    def fake_run(cmd, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return _make_completed("not json")
        return _make_completed(VALID_FITS_JSON)

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fits = score_batch_sync(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="profile",
        today="2026-04-25",
        max_retries=1,
    )

    assert state["n"] == 2
    assert len(fits) == 2


def test_score_batch_sync_login_required(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        return _make_completed("", returncode=1, stderr="Authentication required. Run codex login.")

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CodexCliError, match="not logged in"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_rate_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kw):
        return _make_completed("", returncode=1, stderr="Usage limit reached")

    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CodexCliError, match="limit"):
        score_batch_sync(
            [{"id": 1, "title": "A"}],
            taste_profile="p",
            today="2026-04-25",
            max_retries=0,
        )


def test_score_batch_sync_duplicate_ids_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    with pytest.raises(CodexCliError, match="unique integer"):
        score_batch_sync(
            [{"id": 1, "title": "A"}, {"id": 1, "title": "B"}],
            taste_profile="p",
            today="2026-04-25",
        )
