"""Golden-file + behavior tests for the canonical scout-bot push card.

The push format is locked (spec §2). These tests encode the layout so
future contributors can read the canonical shape from fixtures and from
the omission rules below.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from api.integrations.discord.webhook import (
    DESC_MAX,
    NOTABLE_PERSON_CONFIDENCE_THRESHOLD,
    format_embed,
)
from api.integrations.llm import NotablePerson

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "embed_golden.json"

# Common datetime used across tests: Fri May 1, 2026 10:00pm UTC.
START = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)
END = datetime(2026, 5, 1, 23, 30, tzinfo=UTC)


def _desc_line(embed: dict, emoji_prefix: str) -> str | None:
    """Return the description line that begins with ``emoji_prefix``.

    The renderer puts each item on its own line led by an emoji bullet
    (📅 for date, 📍 for place, 🍕 Food: …, etc.). Tests find the line
    by its emoji prefix. Returns ``None`` when no such line exists —
    lets ``assert _desc_line(embed, "🍕") is None`` pin omission rules.
    """
    for line in embed.get("description", "").splitlines():
        if line.startswith(emoji_prefix):
            return line
    return None


def _full_kwargs() -> dict:
    """Fully populated args matching the golden fixture."""
    return dict(
        title="Cerebral Valley AI Hackathon",
        url="https://lu.ma/cv-hack",
        start_at=START,
        end_at=END,
        neighborhood="SoMa",
        capacity_remaining=42,
        why_line="Same organizer as the RAG meetup you marked +1.",
        notable_people=[
            NotablePerson(
                name="Jensen Huang", role_clause="NVIDIA CEO", confidence=0.95
            ),
            NotablePerson(
                name="Andrej Karpathy",
                role_clause="founder of Eureka Labs",
                confidence=0.90,
            ),
        ],
        event_type="hackathon",
        food_provided="yes",
        agenda="Pitches at 6pm, judging at 9pm.",
        friends_going=["Sarah", "Bob", "Alice"],
        cover_image_url="https://luma.com/cover.png",
        source="luma",
        fit_score=0.86,
    )


def test_format_embed_full_canonical_matches_golden():
    embed = format_embed(**_full_kwargs())
    golden = json.loads(GOLDEN.read_text())
    assert embed == golden


def test_format_embed_minimal_renders_only_when_and_fit_lines():
    embed = format_embed(
        title="A meetup",
        url="https://lu.ma/x",
        start_at=START,
        source="luma",
        fit_score=0.72,
    )
    # Only When and Fit lines (both always present); no optional ones.
    assert _desc_line(embed, "📅") == "📅 Fri May 1, 10:00pm"
    assert _desc_line(embed, "✨") == "✨ Fit: 0.72"  # 0.72 → ✨ band
    for emoji in ("📍", "👤", "🤝", "🍕", "💬", "🗒️", "⚠️"):
        assert _desc_line(embed, emoji) is None
    # No fields[] on the embed — everything lives in description now.
    assert "fields" not in embed or embed["fields"] == []
    # Title gets the lightning bolt prefix.
    assert embed["title"] == "⚡ A meetup"
    assert "image" not in embed


def test_format_embed_friend_signal_single_name_uses_name():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        friends_going=["Sarah"],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🤝") == "🤝 Sarah is going"


def test_format_embed_friend_signal_three_uses_count():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        friends_going=["A", "B", "C"],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🤝") == "🤝 3 friends going"
    # Never name names when 2+.
    for name in ("A is going", "B is going", "C is going"):
        assert name not in embed["description"]


def test_format_embed_friend_signal_two_uses_count():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        friends_going=["A", "B"],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🤝") == "🤝 2 friends going"


def test_format_embed_friend_signal_empty_omits_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        friends_going=[],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🤝") is None


def test_format_embed_notable_people_filters_below_threshold():
    # One above, one below the 0.85 threshold.
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        notable_people=[
            NotablePerson(name="High Conf", role_clause="CEO of X", confidence=0.95),
            NotablePerson(name="Low Conf", role_clause="rumored attendee",
                          confidence=0.50),
        ],
        source="luma", fit_score=0.7,
    )
    line = _desc_line(embed, "👤")
    assert line is not None
    assert "High Conf, CEO of X" in line
    assert "Low Conf" not in line
    assert "rumored" not in line


def test_format_embed_notable_people_exact_threshold_kept():
    # 0.85 is inclusive — the bar is "drop below 0.85".
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        notable_people=[
            NotablePerson(name="On The Line", role_clause="founder",
                          confidence=NOTABLE_PERSON_CONFIDENCE_THRESHOLD),
        ],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "👤") == "👤 On The Line, founder"


def test_format_embed_notable_people_all_filtered_omits_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        notable_people=[
            NotablePerson(name="Maybe", role_clause="speaker", confidence=0.6),
        ],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "👤") is None


def test_format_embed_notable_people_uses_middledot_separator():
    """Between-person separator is `` · `` so the comma between name and
    role doesn't collide with the comma between people.

    Old format produced "alice, founder, bob, engineer" — ambiguous.
    New format: "alice, founder · bob, engineer".
    """
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        notable_people=[
            NotablePerson(name="alice", role_clause="founder", confidence=0.95),
            NotablePerson(name="bob", role_clause="engineer", confidence=0.95),
        ],
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "👤") == "👤 alice, founder · bob, engineer"


def test_format_embed_food_no_collapses_to_unknown():
    # spec §2: only `yes` carries push-signal; `no` collapses to `unknown`.
    # The rendered string never reads `no`.
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        food_provided="no",
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🍕") == "🍕 Food: unknown"
    assert "Food: no" not in embed["description"]


def test_format_embed_food_yes_renders_yes():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        food_provided="yes",
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🍕") == "🍕 Food: yes"


def test_format_embed_food_unknown_renders_unknown():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        food_provided="unknown",
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🍕") == "🍕 Food: unknown"


def test_format_embed_food_none_omits_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        food_provided=None,
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🍕") is None


def test_format_embed_event_type_none_omits_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        event_type=None,
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "💬") is None


def test_format_embed_event_type_snake_case_renders_sentence_case():
    """``demo_night`` etc. get rendered as ``Demo night`` (no underscore)."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        event_type="demo_night",
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "💬") == "💬 Format: Demo night"


def test_format_embed_agenda_none_omits_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        agenda=None,
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "🗒") is None


def test_format_embed_title_verbatim_with_special_chars():
    # No rewriting — quotes, ampersands, emoji in the source title pass through.
    weird = 'Andrej & Friends: "RAG in Production" 🦙'
    embed = format_embed(
        title=weird,
        url="https://lu.ma/x", start_at=START,
        source="luma", fit_score=0.7,
    )
    # The ⚡ prefix is added; everything after is verbatim.
    assert embed["title"] == f"⚡ {weird}"


def test_format_embed_title_truncates_at_256():
    long = "X" * 400
    embed = format_embed(
        title=long, url="https://lu.ma/x", start_at=START,
        source="luma", fit_score=0.7,
    )
    assert len(embed["title"]) <= 256


def test_format_embed_uses_brand_color_when_capacity_unknown():
    """Default blue when capacity_remaining is None — no urgency signal."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        source="luma", fit_score=0.7,
    )
    assert embed["color"] == 0x58B9FF


def test_format_embed_uses_amber_color_at_25_capacity():
    """≤25 spots → amber side bar (filling up)."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=25,
        source="luma", fit_score=0.7,
    )
    assert embed["color"] == 0xFAA61A


def test_format_embed_uses_red_color_at_10_capacity():
    """≤10 spots → red side bar (urgent)."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=10,
        source="luma", fit_score=0.7,
    )
    assert embed["color"] == 0xED4245


def test_format_embed_uses_grey_color_when_cancelled():
    """Retracted/cancelled → grey side bar (the row is dead)."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=50,  # would normally be blue
        source="luma", fit_score=0.7,
        cancelled=True,
    )
    assert embed["color"] == 0x4F545C


def test_format_embed_cover_image_attached_when_provided():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        cover_image_url="https://luma.com/cover.png",
        source="luma", fit_score=0.7,
    )
    assert embed["image"] == {"url": "https://luma.com/cover.png"}
    # Title link still points at the Luma registration URL so the
    # clickable cover image opens the right destination.
    assert embed["url"] == "https://lu.ma/x"


def test_format_embed_cover_image_none_omits_image_field():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        cover_image_url=None,
        source="luma", fit_score=0.7,
    )
    assert "image" not in embed


def test_format_embed_neighborhood_none_omits_where_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        end_at=END,
        neighborhood=None,
        capacity_remaining=20,
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "📅") == "📅 Fri May 1, 10:00pm-11:30pm"
    assert _desc_line(embed, "📍") is None
    # Capacity never gets its own line; comfortable capacity (20) is
    # invisible by design — only the warning banner surfaces it.
    assert "spots" not in embed["description"]


def test_format_embed_end_at_none_renders_start_only():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        end_at=None,
        neighborhood="SoMa",
        capacity_remaining=20,
        source="luma", fit_score=0.7,
    )
    assert _desc_line(embed, "📅") == "📅 Fri May 1, 10:00pm"
    assert _desc_line(embed, "📍") == "📍 SoMa"


def test_format_embed_capacity_silent_when_comfortable():
    """Capacity has no standalone line. Comfortable capacity is invisible
    by design; only the urgency banner (set by the capacity poller via
    ``capacity_warning_threshold``) ever surfaces raw spot counts.
    Friends who want exact spots click through to Luma.
    """
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=42,  # comfortable, no warning threshold set
        source="luma", fit_score=0.7,
    )
    assert "spots" not in embed["description"]
    assert "⚠️" not in embed["description"]


def test_format_embed_capacity_banner_clamps_negative_to_zero():
    """The capacity warning ticker still clamps -1 sentinel to 0 even
    though there's no longer a Capacity field. The banner is now the
    only path raw spot counts ever surface.
    """
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=-1,
        capacity_warning_threshold=10,
        source="luma", fit_score=0.7,
    )
    assert "⚠️ now ~0 spots left." in embed["description"]
    assert "-1" not in embed["description"]


def test_format_embed_why_line_none_omitted():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        why_line=None,
        source="luma", fit_score=0.7,
    )
    # Minimum description: 📅 When line + ✨/⭐/🔥 Fit line.
    # That's exactly 2 lines, so exactly 1 newline.
    assert embed["description"].count("\n") == 1


def test_format_embed_why_line_empty_string_omitted():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        why_line="",
        source="luma", fit_score=0.7,
    )
    assert embed["description"].count("\n") == 1


def test_format_embed_footer_is_source_only():
    """Footer carries only the source attribution; fit lives in the body."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        source="luma", fit_score=0.86,
    )
    assert embed["footer"] == {"text": "luma"}


def test_format_embed_fit_line_uses_band_emoji():
    """Bands match production thresholds: 🔥 ≥0.90 / ⭐ ≥0.80 / ✨ ≥0.70 / 🔅 below.

    Fit lives in the description body as its own line so the quality
    signal is the eye-catcher, not the dim grey footer.
    """
    cases = [
        (0.95, "🔥", "🔥 Fit: 0.95"),
        (0.90, "🔥", "🔥 Fit: 0.90"),
        (0.85, "⭐", "⭐ Fit: 0.85"),
        (0.80, "⭐", "⭐ Fit: 0.80"),
        (0.75, "✨", "✨ Fit: 0.75"),
        (0.70, "✨", "✨ Fit: 0.70"),
        (0.50, "🔅", "🔅 Fit: 0.50"),
        (0.10, "🔅", "🔅 Fit: 0.10"),
    ]
    for score, expected_emoji, expected_line in cases:
        embed = format_embed(
            title="x", url="https://lu.ma/x", start_at=START,
            source="luma", fit_score=score,
        )
        line = _desc_line(embed, expected_emoji)
        assert line == expected_line, (
            f"fit_score={score} should produce {expected_line!r}, "
            f"got {line!r}"
        )


def test_format_embed_overflow_drops_agenda_first():
    """When the rendered description exceeds DESC_MAX, the renderer
    drops the optional agenda line first (it's the most expendable),
    keeping earlier load-bearing lines (When/Where/why-line/Fit) intact.
    """
    long_agenda = "x" * (DESC_MAX + 100)
    embed = format_embed(
        title="x",
        url="https://lu.ma/x",
        start_at=START,
        end_at=END,
        neighborhood="SoMa",
        capacity_remaining=20,
        why_line="Same organizer as the RAG meetup you marked +1.",
        agenda=long_agenda,
        source="luma",
        fit_score=0.7,
    )
    desc = embed["description"]
    # Agenda was dropped — no 🗒️ line and no body of x's.
    assert _desc_line(embed, "🗒") is None
    assert "xxxxxxxx" not in desc
    # Earlier load-bearing lines are preserved.
    assert _desc_line(embed, "📅") == "📅 Fri May 1, 10:00pm-11:30pm"
    assert _desc_line(embed, "📍") == "📍 SoMa"
    assert "Same organizer as the RAG meetup you marked +1." in desc
    # Final description is within the Discord cap.
    assert len(desc) <= DESC_MAX


def test_format_embed_oversize_description_hard_truncates():
    """Description (urgency banner + why_line only) must stay under DESC_MAX.

    Defense-in-depth: an oversized why_line slipping past the EventFit
    boundary still has to fit Discord's 4096-char description cap.
    """
    long_why = "y" * (DESC_MAX + 500)
    embed = format_embed(
        title="x",
        url="https://lu.ma/x",
        start_at=START,
        why_line=long_why,
        agenda=None,
        source="luma",
        fit_score=0.7,
    )
    assert len(embed["description"]) == DESC_MAX


def test_format_embed_capacity_warning_lands_in_description_first_line():
    """Spec §4 PATCH: ``⚠️ now ~X spots left.`` is the first line of the
    description so the urgency signal is the eye-catcher. The When/Where
    lines still follow, and Fit closes the body.
    """
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        neighborhood="SoMa",
        capacity_remaining=42,
        capacity_warning_threshold=50,
        source="luma", fit_score=0.86,
    )
    lines = embed["description"].splitlines()
    # First line is the urgency banner.
    assert lines[0] == "⚠️ now ~42 spots left."
    # Structured info still lands on its own lines.
    assert _desc_line(embed, "📅") == "📅 Fri May 1, 10:00pm"
    assert _desc_line(embed, "📍") == "📍 SoMa"
    assert _desc_line(embed, "⭐") == "⭐ Fit: 0.86"
    # Title is unchanged (not strikethrough).
    assert embed["title"] == "⚡ x"


def test_format_embed_capacity_warning_uses_current_capacity_not_threshold():
    """Wording is locked to actual remaining spots, not the band number."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=8,
        capacity_warning_threshold=10,  # 10-band crossed
        source="luma", fit_score=0.86,
    )
    # The ticker line shows 8 (actual remaining), not 10 (threshold band).
    assert "~8 spots left" in embed["description"]
    assert "~10 spots left" not in embed["description"]


def test_format_embed_capacity_warning_clamps_negative_to_zero():
    """A scraper edge case (-1 sentinel) must never render as ``~-1 spots``."""
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=-1,
        capacity_warning_threshold=10,
        source="luma", fit_score=0.86,
    )
    assert "~0 spots left" in embed["description"]
    assert "~-1 spots left" not in embed["description"]


def test_format_embed_capacity_warning_none_omits_ticker_line():
    embed = format_embed(
        title="x", url="https://lu.ma/x", start_at=START,
        capacity_remaining=42,
        capacity_warning_threshold=None,
        source="luma", fit_score=0.7,
    )
    assert "⚠️" not in embed["description"]
    assert "spots left" not in embed["description"]


def test_format_embed_cancelled_strikes_through_title_and_prepends_apology():
    """Spec §4 cancellation branch: title wrapped in ``~~`` + apology banner."""
    embed = format_embed(
        title="Cancelled Meetup",
        url="https://lu.ma/x", start_at=START,
        source="luma", fit_score=0.86,
        cancelled=True,
    )
    # Strikethrough wraps the FULL title including the lightning bolt
    # so the visual continuity with the original push is obvious.
    assert embed["title"] == "~~⚡ Cancelled Meetup~~"
    # Apology banner is the first description line.
    first_line = embed["description"].splitlines()[0]
    assert first_line.startswith("❌")
    assert "cancelled" in first_line.lower()


def test_format_embed_cancelled_defaults_to_false():
    """Backward-compat: omitting ``cancelled`` keeps the title untouched."""
    embed = format_embed(
        title="Live Meetup",
        url="https://lu.ma/x", start_at=START,
        source="luma", fit_score=0.86,
    )
    assert embed["title"] == "⚡ Live Meetup"
    assert "❌" not in embed["description"]


def test_format_embed_overflow_does_not_drop_why_line_starting_with_agenda_prefix():
    """The agenda-drop must use index, not string prefix. A ``why_line`` that
    happens to start with ``"agenda: "`` (e.g., an LLM quoting a label back)
    must survive the overflow handler — only the genuine agenda line is
    dropped.
    """
    long_agenda = "z" * (DESC_MAX + 100)
    embed = format_embed(
        title="x",
        url="https://lu.ma/x",
        start_at=START,
        why_line="agenda: looks like a fit per your taste profile",
        agenda=long_agenda,
        source="luma",
        fit_score=0.7,
    )
    desc = embed["description"]
    # The look-alike why_line survives.
    assert "agenda: looks like a fit per your taste profile" in desc
    # The actual agenda body is gone.
    assert "zzzzzzzz" not in desc
    assert len(desc) <= DESC_MAX
