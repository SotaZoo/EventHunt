"""Discord webhook payload format + POST.

``format_embed`` is a pure function that renders one event into the
canonical scout-bot push card (spec §2). The shape is locked: the bot is
metadata-adding, not editorializing — the title is verbatim from Luma and
optional metadata lines are omitted (not faked) when absent.

POST below uses httpx. ``post_event`` appends ``?wait=true`` so Discord
returns the created message as JSON (we need ``id`` for the capacity
ticker PATCH and ``/scout retract``). ``patch_event`` edits an existing
message via the ``/webhooks/{...}/messages/{id}`` endpoint.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from api.services.event_presentation import fit_score_icon

if TYPE_CHECKING:
    from api.integrations.llm import NotablePerson

BRAND_COLOR = 0x58B9FF  # soft blue, picked once
TITLE_MAX = 256
DESC_MAX = 4096
TITLE_PREFIX = "⚡"  # lightning bolt on the title line per spec §2
NOTABLE_PERSON_CONFIDENCE_THRESHOLD = 0.85
"""Renderer drops notable-people entries below this self-reported confidence.

Storage keeps every row so a later sweep can re-tune this threshold; only
the push-card render filters. See ``NotablePerson`` docstring.
"""


def _friend_signal_line(names: list[str]) -> str | None:
    """Render the friend-signal line per spec §2.

    Zero names → ``None`` (caller omits the line entirely). One name keeps
    the name. Two or more collapses to a count to protect friend privacy
    in the channel.
    """
    if not names:
        return None
    if len(names) == 1:
        return f"{names[0]} is going"
    return f"{len(names)} friends going"


def _collapse_food(food: str | None) -> str | None:
    """Collapse the tristate ``food_provided`` to the rendered token.

    Only ``"yes"`` carries push-signal; ``"no"`` and ``"unknown"`` both
    render as ``"unknown"``. ``None`` means the line is omitted. The
    rendered string never reads ``food: no``.
    """
    if food is None:
        return None
    if food == "yes":
        return "yes"
    return "unknown"


def _filter_notable_people(
    people: list[NotablePerson] | None,
) -> list[NotablePerson]:
    """Drop entries below the confidence threshold before render."""
    if not people:
        return []
    return [p for p in people if p.confidence >= NOTABLE_PERSON_CONFIDENCE_THRESHOLD]


def _format_notable_people_value(people: list[NotablePerson]) -> str:
    """Render filtered entries as ``<name>, <role_clause> · <name>, <role_clause>``.

    The between-person separator is `` · `` (middle-dot with spaces) so
    person boundaries don't collide with the ``,`` that separates each
    name from its role. The previous all-comma format produced ambiguous
    output like "alice, founder, bob, engineer" where parsing requires
    counting commas.

    No "notable people: " prefix — the Discord embed field name carries
    that label so the value text stays content-only.
    """
    return " · ".join(f"{p.name}, {p.role_clause}" for p in people)


def _format_when_value(start_at: datetime, end_at: datetime | None) -> str:
    """Render the When field value: ``<weekday> <date>, <start>-<end>``."""
    weekday = start_at.strftime("%a")
    date = start_at.strftime("%b %-d")
    start_clock = start_at.strftime("%-I:%M%p").lower()
    when = f"{weekday} {date}, {start_clock}"
    if end_at is not None:
        end_clock = end_at.strftime("%-I:%M%p").lower()
        when = f"{when}-{end_clock}"
    return when


def _format_capacity_value(capacity_remaining: int) -> str:
    """Render the Capacity field value: ``<N> spots``.

    Belt-and-suspenders: a scraper edge case (e.g. -1 sentinel for
    "unknown") must never render as "-1 spots".
    """
    spots = max(0, capacity_remaining)
    return f"{spots} spots"


def _format_event_type_label(event_type: str) -> str:
    """Render event_type as a human label.

    The LLM may emit snake_case tokens (``demo_night``, ``tech_talk``).
    Convert to "Demo night", "Tech talk" — sentence case, single word
    or short phrase, never raw underscores. Unknown shapes pass through
    title-cased.
    """
    cleaned = event_type.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return event_type
    # Sentence case (first letter caps, rest lower) preserves acronyms
    # if the LLM is careful (e.g. "AI demo") but most outputs are
    # already lowercase tokens, so this is safe.
    return cleaned[0].upper() + cleaned[1:].lower()


def _fit_score_prefix(fit_score: float) -> str:
    """Emoji prefix that scales with fit_score so the quality signal is
    visible at a glance, not buried in the footer's grey text.

    Bands match the production threshold geometry:
      ≥0.90 → 🔥  (clear pick; the scout-bot's high-bar default)
      ≥0.80 → ⭐  (solid)
      ≥0.70 → ✨  (decent; /today /week surface)
      below → 🔅  (de-emphasized; surfaced for tuning, not a pick)
    """
    return fit_score_icon(fit_score)


def _capacity_color(capacity_remaining: int | None, cancelled: bool) -> int:
    """Pick the embed side-bar color for at-a-glance availability scan.

    - cancelled → grey (the row is dead)
    - <=10 spots → red (urgent)
    - <=25 spots → amber (filling up)
    - else → BRAND_COLOR (default blue)

    Returns the integer Discord uses for the ``color`` field.
    """
    if cancelled:
        return 0x4F545C  # neutral grey for retracted/cancelled
    if capacity_remaining is None:
        return BRAND_COLOR
    if capacity_remaining <= 10:
        return 0xED4245  # red — urgent
    if capacity_remaining <= 25:
        return 0xFAA61A  # amber — filling up
    return BRAND_COLOR


CANCELLED_APOLOGY_LINE = "❌ This event was cancelled by the host. Sorry for the noise."
"""Banner prepended when the capacity poller observes a cancellation.

Spec §4: cancellation PATCH strikes through the title and prepends an
apology line so the channel sees the row was retracted by the host (not
by the bot). The string is locked across renders so tests can assert it.
"""

CAPACITY_WARNING_THRESHOLDS = (50, 25, 10)
"""Ordered crossing thresholds for the capacity ticker (spec §4).

Highest-first so the poller fires 50 → 25 → 10 in order when capacity
drops multiple bands between polls.
"""


def _capacity_warning_line(spots: int) -> str:
    """Render the ticker line per spec §4 ("⚠️ now ~X spots left.")."""
    return f"⚠️ now ~{max(0, spots)} spots left."


def format_embed(
    *,
    title: str,
    url: str,
    start_at: datetime,
    end_at: datetime | None = None,
    neighborhood: str | None = None,
    capacity_remaining: int | None = None,
    why_line: str | None = None,
    notable_people: list[NotablePerson] | None = None,
    event_type: str | None = None,
    food_provided: str | None = None,
    agenda: str | None = None,
    friends_going: list[str] | None = None,
    cover_image_url: str | None = None,
    source: str,
    fit_score: float,
    capacity_warning_threshold: int | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Build a Discord embed for one event in the canonical scout-bot format.

    Pure function. All inputs come from the caller; no DB access. See
    spec §2 for the locked layout. The title is verbatim (only truncated
    to Discord's 256-char hard limit). Optional metadata lines are omitted
    (not faked) when absent.

    ``capacity_warning_threshold`` (50 | 25 | 10 | None) prepends the
    capacity-ticker line ("⚠️ now ~X spots left.") to the description per
    spec §4. The PATCH path in ``api/tasks/capacity_poller.py`` re-renders
    the entire embed with this flag set; the rest of the card (title, time,
    friend-signal) stays intact across the edit. Threshold value is used
    only for line wording — actual spot count comes from
    ``capacity_remaining``.

    ``cancelled=True`` strikes through the title (Discord markdown ``~~..~~``)
    and prepends ``CANCELLED_APOLOGY_LINE`` to the description per spec §4.
    The ⚡ prefix is kept inside the strikethrough so the visual continuity
    with the original push is obvious.
    """
    # Layout is a list of one-item lines in the description. Each line
    # carries its own emoji bullet so the card scans top-to-bottom like
    # a checklist. No Discord fields — those forced inline-packing
    # behavior (3 per row) we couldn't override, and the field-name
    # labels were oversized for short values like "yes" or "Demo".
    lines: list[str] = []

    # Banners first (urgency stuff): cancellation > capacity warning > why.
    # The two visible-warning banners are mutually exclusive in practice
    # but the order here (cancellation first) keeps the apology
    # most-visible if both flags ever get set in the same render.
    if cancelled:
        lines.append(CANCELLED_APOLOGY_LINE)
    if capacity_warning_threshold is not None and capacity_remaining is not None:
        lines.append(_capacity_warning_line(capacity_remaining))
    if why_line:
        lines.append(why_line)

    # Structured one-item-per-line block. Items with self-explanatory
    # emoji (date, place, person, handshake) drop the label; items
    # where the value alone is ambiguous (yes / Demo / 0.82) keep a
    # short word.
    lines.append(f"📅 {_format_when_value(start_at, end_at)}")
    if neighborhood:
        lines.append(f"📍 {neighborhood}")

    filtered_people = _filter_notable_people(notable_people)
    if filtered_people:
        lines.append(f"👤 {_format_notable_people_value(filtered_people)}")

    friend_line = _friend_signal_line(friends_going or [])
    if friend_line:
        lines.append(f"🤝 {friend_line}")

    food_token = _collapse_food(food_provided)
    if food_token is not None:
        lines.append(f"🍕 Food: {food_token}")
    if event_type:
        lines.append(f"💬 Format: {_format_event_type_label(event_type)}")

    # Fit gets its own line so the quality signal is in the body
    # text, not buried in the dim grey footer. Emoji band scales
    # with the score for at-a-glance reading.
    lines.append(f"{_fit_score_prefix(fit_score)} Fit: {fit_score:.2f}")

    if agenda:
        # Agenda last; it's the longest and most expendable. Don't
        # pre-truncate — let the overflow check below drop the whole
        # line if the cumulative description would blow the 4096-char
        # cap. Truncating mid-agenda would produce a half-sentence,
        # which reads worse than the line not appearing at all.
        lines.append(f"🗒️ Agenda: {agenda}")

    description = "\n".join(lines)
    if len(description) > DESC_MAX:
        # Drop agenda first (matches the legacy renderer's contract),
        # then hard-truncate as a last resort.
        if agenda and lines[-1].startswith("🗒️"):
            lines.pop()
            description = "\n".join(lines)
        if len(description) > DESC_MAX:
            description = description[:DESC_MAX]

    # Cancellation strikes through the verbatim title. The ⚡ prefix sits
    # inside the strikethrough so the visual continuity with the original
    # push is obvious — the row looks like the same card, retracted.
    if cancelled:
        rendered_title = f"~~{TITLE_PREFIX} {title}~~"[:TITLE_MAX]
    else:
        rendered_title = f"{TITLE_PREFIX} {title}"[:TITLE_MAX]

    # Footer carries only the source attribution. Fit lives in the body.
    embed: dict[str, Any] = {
        "title": rendered_title,
        "url": url,
        "description": description,
        "footer": {"text": source},
        "color": _capacity_color(capacity_remaining, cancelled),
    }
    if cover_image_url:
        embed["image"] = {"url": cover_image_url}
    return embed


class PostStatus(enum.Enum):
    OK = "ok"
    RETRYABLE = "retryable"
    FATAL = "fatal"


@dataclass
class PostResult:
    status: PostStatus
    retry_after: float | None = None  # seconds, only set on 429
    detail: str | None = None
    message_id: str | None = None
    """Discord-issued message id captured from the ``?wait=true`` response.

    Set only on ``PostStatus.OK`` when Discord returned a JSON body with
    ``id``. Used downstream by the capacity ticker PATCH (Task 5) and
    ``/scout retract`` (Task 4). Stays ``None`` for retryable / fatal
    results — those never see a Discord message id.
    """


def _with_wait_true(url: str) -> str:
    """Return ``url`` with ``wait=true`` appended to the query string.

    Preserves any existing query params (some webhook configs may already
    carry ``thread_id`` etc.). If ``wait`` is already set, it's overwritten
    to ``true`` — the entire point of ``post_event`` is to capture the
    response id, so silently honoring a caller-supplied ``wait=false``
    would defeat the contract.
    """
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["wait"] = "true"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment)
    )


def _result_from_response(resp: httpx.Response) -> PostResult:
    if 200 <= resp.status_code < 300:
        message_id: str | None = None
        # Discord returns a JSON body only when ``?wait=true`` was set;
        # ``post_event`` always sets it. ``patch_event`` also receives a
        # full message body. Tolerate missing/non-JSON bodies (e.g., 204
        # from a forgotten ``?wait=true``) by leaving ``message_id`` None
        # so the caller can decide whether to fail loud.
        try:
            body = resp.json()
        except (ValueError, AttributeError):
            body = None
        if isinstance(body, dict):
            raw_id = body.get("id")
            if raw_id is not None:
                message_id = str(raw_id)
        return PostResult(PostStatus.OK, message_id=message_id)
    if resp.status_code == 429:
        try:
            ra = float(resp.headers.get("Retry-After", "1"))
        except ValueError:
            ra = 1.0
        return PostResult(PostStatus.RETRYABLE, retry_after=ra)
    if 500 <= resp.status_code < 600:
        return PostResult(PostStatus.RETRYABLE, detail=f"5xx: {resp.status_code}")
    # 4xx other than 429 → fatal (404 = bad webhook URL; 400 = bad payload)
    return PostResult(PostStatus.FATAL, detail=f"{resp.status_code}: {resp.text[:200]}")


def post_event(webhook_url: str, embed: dict, *, timeout: float = 10.0) -> PostResult:
    """POST one embed to a Discord webhook with ``?wait=true``.

    ``?wait=true`` is REQUIRED (spec §1) so Discord returns the created
    message as JSON; ``PostResult.message_id`` carries the captured id back
    to the caller. Without it Discord returns 204 No Content and the
    capacity ticker / retract paths have nothing to PATCH.

    See design §Failure Modes for the retryable/fatal taxonomy.
    """
    url = _with_wait_true(webhook_url)
    try:
        resp = httpx.post(url, json={"embeds": [embed]}, timeout=timeout)
    except httpx.TimeoutException as e:
        return PostResult(PostStatus.RETRYABLE, detail=f"timeout: {e}")
    except httpx.RequestError as e:
        return PostResult(PostStatus.RETRYABLE, detail=f"network: {e}")
    return _result_from_response(resp)


def patch_event(
    webhook_url: str,
    message_id: str,
    embed: dict,
    *,
    timeout: float = 10.0,
) -> PostResult:
    """PATCH an existing Discord webhook message by id.

    Targets ``{webhook_url}/messages/{message_id}`` per Discord's webhook
    edit endpoint. Used by the capacity ticker (Task 5) and ``/scout
    retract`` (Task 4) to mutate cards already in #scout — the canonical
    push card stays the source of truth, edits replace its body.

    Returns the same ``PostResult`` shape as ``post_event`` so callers can
    share the OK / RETRYABLE / FATAL handling. ``message_id`` is preserved
    on 2xx (Discord echoes the same id in the PATCH response).
    """
    parts = urlsplit(webhook_url)
    edit_path = f"{parts.path.rstrip('/')}/messages/{message_id}"
    edit_url = urlunsplit(
        (parts.scheme, parts.netloc, edit_path, parts.query, parts.fragment)
    )
    try:
        resp = httpx.patch(edit_url, json={"embeds": [embed]}, timeout=timeout)
    except httpx.TimeoutException as e:
        return PostResult(PostStatus.RETRYABLE, detail=f"timeout: {e}")
    except httpx.RequestError as e:
        return PostResult(PostStatus.RETRYABLE, detail=f"network: {e}")
    return _result_from_response(resp)
