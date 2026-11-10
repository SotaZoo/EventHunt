"""Conservative event-occurrence identity, independent of personal state."""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

CatalogRow = dict[str, Any]
# Eventbrite's official regional sites share the same event IDs.
_EVENTBRITE_HOSTS = frozenset({
    "eventbrite.at", "eventbrite.be", "eventbrite.ca", "eventbrite.ch", "eventbrite.cl",
    "eventbrite.co", "eventbrite.co.nz", "eventbrite.co.uk", "eventbrite.com",
    "eventbrite.com.ar", "eventbrite.com.au", "eventbrite.com.br", "eventbrite.com.mx",
    "eventbrite.com.pe", "eventbrite.de", "eventbrite.dk", "eventbrite.es", "eventbrite.fi",
    "eventbrite.fr", "eventbrite.hk", "eventbrite.ie", "eventbrite.it", "eventbrite.nl",
    "eventbrite.pt", "eventbrite.se", "eventbrite.sg",
})
# A supported assertion has two named links plus one blank Meetup export anchor.
_MAX_REGISTRATION_LINKS = 3
_REGISTRATION_INTRO = re.compile(
    r"(?:important(?: note)?: )?(?:please )?(?:register|rsvp) (?:on|at|via|through|using)(?: the)?"
)
_REGISTRATION_ALTERNATIVE = re.compile(r"\(?or (?:on|at|via)")
_REGISTRATION_SUFFIX = re.compile(
    r"(?:\(or (?:on|at|via) (?:luma|event website)\)(?: )?)?"
    r"(?:is required for admission|to receive the joining link)?[.!]?"
    r"(?: \(?rsvp on meetup (?:is turned off|will not receive anything)\)?\.?)?"
)


def utc_instant(value: str | datetime | None) -> datetime | None:
    """Compare explicit timestamps consistently without inferring a local clock."""
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The existing catalog's legacy naive timestamps are UTC, not inferred NY.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def provider_identity(url: str) -> tuple[str, str]:
    """Ignore tracking only for known provider identifiers; preserve other URLs."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port is not None:
            return ("url", url)
    except ValueError:
        return ("url", url)
    path = parsed.path.rstrip("/")
    if host in _EVENTBRITE_HOSTS:
        match = re.fullmatch(r"/e/[^/]*-(\d+)", path)
        if match:
            return ("eventbrite", match[1])
    if host == "meetup.com":
        match = re.fullmatch(r"/[^/]+/events/(\d+)", path)
        if match:
            return ("meetup", match[1])
    if host in {"lu.ma", "luma.com"} and re.fullmatch(r"/[A-Za-z0-9_-]+", path):
        return ("luma", path[1:])
    if host == "partiful.com" and re.fullmatch(r"/e/[A-Za-z0-9_-]+", path):
        return ("partiful", path[3:])
    if host == "betaworks.com" and path.startswith("/event/") and len(path) > 7:
        return ("betaworks", path[7:])
    return ("url", url)


def supported_registration_url(url: str) -> bool:
    """Allow only credential-free HTTPS event URLs on known public providers."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port is None and provider_identity(url)[0] != "url")
    except ValueError:
        return False


def _text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def registration_identity(url: str | None) -> tuple[str, str] | None:
    """Recognize passive registration evidence without expanding outbound fetch permissions."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
            return None
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
    except ValueError:
        return None
    if host in {"lu.ma", "luma.com"} and re.fullmatch(r"/event/evt-[A-Za-z0-9]+", path):
        return ("luma-id", path.removeprefix("/event/"))
    if host == "aicamp.ai" and re.fullmatch(r"/event/eventdetails/[A-Za-z0-9_-]+", path):
        return ("aicamp", path.removeprefix("/event/eventdetails/"))
    if (host == "voxel51.com" and not parsed.query
            and re.fullmatch(r"/events/[A-Za-z0-9_-]+", path)):
        return ("voxel51", path.removeprefix("/events/"))
    identity = provider_identity(url)
    return identity if identity[0] != "url" else None


def _meetup_registration_evidence(description: str) -> tuple[list[str], bool]:
    """Find registration assertions and whether event details corroborate title aliases."""
    assertions = []
    has_event_details = False
    blocks = MarkdownIt("commonmark").parse(description)
    if any(block.type == "html_block" for block in blocks):
        # CommonMark intentionally does not model nesting inside raw HTML.
        return [], False
    contextual_cta = False
    for index, block in enumerate(blocks):
        # Introductory CTAs are supported only before section/example context.
        # A heading or colon introduction may scope this as an old-link example.
        if (block.type in {"heading_open", "blockquote_open", "fence", "code_block"}
                or (block.type == "inline" and block.content.rstrip(" \t\n*_").endswith(":"))):
            contextual_cta = True
        if block.type != "inline" or block.level != 1 or blocks[index - 1].type != "paragraph_open":
            continue
        children = block.children or []
        if any(child.type not in {"text", "softbreak", "hardbreak", "em_open", "em_close",
                                   "strong_open", "strong_close", "link_open", "link_close"}
               for child in children):
            continue
        if any(child.type == "link_open" and child.attrGet("title") for child in children):
            # Do not discard extra link context when establishing identity.
            continue
        parts = []
        links = []
        position = 0
        for child in children:
            if child.type == "link_open":
                link_start, href = position, child.attrGet("href") or ""
            elif child.type == "link_close":
                links.append((link_start, position, href))
            else:
                content = " " if child.type in {"softbreak", "hardbreak"} else child.content
                parts.append(content)
                position += len(content)
        if len(links) > _MAX_REGISTRATION_LINKS:
            continue
        paragraph = "".join(parts)
        if (not links and _text(paragraph)
                and not re.match(r"(?:please )?(?:seats are limited|(?:pre-)?registration|register|rsvp|tickets)\b",
                                 _text(paragraph))):
            has_event_details = True
        standalone_paragraph = description.strip() == block.content.strip()
        registration_end = None
        urls = []
        for start, end, href in links:
            label = _text(paragraph[start:end])
            between = _text(paragraph[registration_end:start]) if registration_end is not None else None
            if not label:
                # Some Meetup exports append a second, empty anchor. It adds
                # no identity, but does not break an adjacent explicit "or on".
                if between == "":
                    registration_end = end
                continue
            clause = _text(paragraph[:start])
            positive_intro = bool(_REGISTRATION_INTRO.fullmatch(clause))
            direct_cta = (label in {"register", "register for event", "rsvp"}
                          and clause in {"", "please", "tickets:", "registration:"}
                          and standalone_paragraph)
            alternative = between is not None and bool(_REGISTRATION_ALTERNATIVE.fullmatch(between))
            ending = _text(paragraph[end:])
            if alternative:
                ending = _text(ending.removeprefix(")"))
            joining_link = "to receive the joining link" in ending
            registration_clause = (label in {"event website", "luma", "register", "register for event", "rsvp"}
                                   and (positive_intro or direct_cta or alternative)
                                   and (not joining_link or not contextual_cta)
                                   and _REGISTRATION_SUFFIX.fullmatch(ending))
            # This full affirmative assertion differs from a bare Register link,
            # whose surrounding document might identify it as an old example.
            affirmative_cta = not contextual_cta and (
                (label.rstrip(".") == "register to reserve your spot"
                 and not clause and ending in {"", "seats are limited."})
                or (label.rstrip(".") == "pre-registration is mandatory"
                    and clause in {"", "seats are limited,"} and not ending)
            )
            if (registration_clause or affirmative_cta) and registration_identity(href) is not None:
                urls.append(href)
                registration_end = end
        if urls:
            assertions.append((urls, not contextual_cta))
    if not assertions:
        return [], False
    identities = {registration_identity(url) for url in assertions[0][0]}
    if any({registration_identity(url) for url in urls} != identities for urls, _ in assertions[1:]):
        # Separate paragraphs do not establish that different URLs are aliases.
        return [], False
    return assertions[0][0], has_event_details and all(unscoped for _, unscoped in assertions)


def _registration_evidence(row: CatalogRow) -> tuple[set[tuple[str, str]], str]:
    urls = [row["url"], row.get("registration_url")]
    matching_description = ""
    if row.get("source") == "meetup":
        description = row.get("description") or ""
        registration_urls, has_event_details = _meetup_registration_evidence(description)
        urls.extend(registration_urls)
        if has_event_details:
            # Preserve case, punctuation and URLs; only normalize whitespace.
            matching_description = " ".join(description.split())
    return {identity for url in urls if (identity := registration_identity(url)) is not None}, matching_description


def occurrence_groups(rows: Iterable[CatalogRow]) -> list[list[CatalogRow]]:
    """Union provider aliases, explicit registration matches, or corroborated facts."""
    records = list(rows)
    parents = list(range(len(records)))
    keys: dict[tuple, int] = {}

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, row in enumerate(records):
        instant = utc_instant(row.get("start_at"))
        if instant is None:
            continue
        identity_keys = [("provider", provider_identity(row["url"]), instant)]
        title, host, venue = (_text(row.get(field)) for field in ("title", "host", "venue"))
        registration_ids, description = _registration_evidence(row)
        if title:
            identity_keys.extend(("registration", identity, instant, title)
                                 for identity in registration_ids)
        locality = _text(row.get("neighborhood"))
        if (description and locality and row.get("attendance_mode") in {"in-person", "mixed"}
                and venue not in {"", locality, "tba", "tbd", "to be announced", "to be determined",
                                  "online", "virtual", "zoom", "google meet", "microsoft teams"}):
            # Title aliases need identical full details and a concrete place;
            # a shared conference link alone cannot join parallel sessions.
            identity_keys.extend(("registration-place", identity, instant, venue, locality, description)
                                 for identity in registration_ids)
        if title and venue and host and host not in {"luma", "meetup", "partiful", "eventbrite"}:
            identity_keys.append(("facts", instant, title, host, venue, locality))
        for key in identity_keys:
            if key in keys:
                parents[root(index)] = root(keys[key])
            else:
                keys[key] = index
    groups: dict[int, list[CatalogRow]] = {}
    for index, row in enumerate(records):
        groups.setdefault(root(index), []).append(row)
    return list(groups.values())
