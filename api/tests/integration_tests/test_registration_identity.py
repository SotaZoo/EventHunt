"""Explicit registration evidence joins cross-posts without guessing identity."""
from contextlib import closing
from datetime import datetime

import pytest
from freezegun import freeze_time

from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.tests.fixtures.catalog_helpers import START, agent_events, web_events
from api.tests.fixtures.web_helpers import add_user, migrated_db

WORKSHOP_DESCRIPTION = (
    "Build a robot detector with live data and a guided training session.\n\n"
    "Seats are limited, [Pre-registration is mandatory.](https://voxel51.com/events/workshop)\n\n"
    "Bring a laptop for the afternoon workshop in Workshop Hall."
)


def _listing(db, url, *, source="meetup", title="Agent Builder Lab", start=START,
             description=None, registration_url=None, state="unknown", score=.8, host=None,
             venue=None, neighborhood=None, attendance_mode=None):
    with closing(connect(db)) as conn:
        event_id = upsert_raw(conn, RawEvent(
            source=source, url=url, title=title, starts_at=datetime.fromisoformat(start),
            description=description, registration_url=registration_url,
            host=host, venue=venue, neighborhood=neighborhood,
            attendance_mode=attendance_mode,
            registration_availability=state,
            availability_evidence=f"source: {state}" if state != "unknown" else None,
        ))
        if score is not None:
            update_fit(conn, event_id, fit_score=score, reasoning="Synthetic shared fit",
                       urgency="medium", urgency_reason=None, prompt_version="v2", taste_sha="synthetic")
    return event_id


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("attendance_mode", ["in-person", "mixed"])
@pytest.mark.parametrize("extra_whitespace", [False, True])
def test_registration_and_physical_occurrence_join_pending_title_aliases(tmp_path, attendance_mode, extra_whitespace):
    db = migrated_db(tmp_path)
    add_user(db)
    description = WORKSHOP_DESCRIPTION.replace("a robot", "a  robot") if extra_whitespace else WORKSHOP_DESCRIPTION
    _listing(db, "https://www.meetup.com/builders/events/12345/", title="Physical AI Workshop",
             description=description, venue="Workshop Hall", neighborhood="New York", attendance_mode="in-person",
             score=None)
    canonical = _listing(db, "https://www.meetup.com/builders/events/67890/", title="NYC Physical AI Workshop",
                         description=WORKSHOP_DESCRIPTION, venue="Workshop Hall", neighborhood="New York",
                         attendance_mode=attendance_mode, score=.96)
    assert [event['id'] for event in web_events(db)] == [canonical]
    assert [event.id for event in agent_events(db)] == [canonical]
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("change", [
    {"description": WORKSHOP_DESCRIPTION.replace("robot detector", "parallel vision model")},
    {"description": WORKSHOP_DESCRIPTION.replace("Build", "BUILD")},
    {"description": WORKSHOP_DESCRIPTION.replace("events/workshop", "events/Workshop")},
    {"description": WORKSHOP_DESCRIPTION.replace("events/workshop", "events/other")},
    {"start": "2026-10-08T18:00:00-04:00"},
    {"venue": "Different Hall"},
    {"neighborhood": "Boston"},
    {"venue": None},
    {"venue": "TBA"},
    {"venue": "To be announced"},
    {"venue": "Online"},
    {"venue": "Zoom"},
    {"venue": "New York"},
    {"neighborhood": None},
    {"attendance_mode": None},
    {"attendance_mode": "online"},
])
def test_title_alias_needs_identical_details_and_concrete_physical_occurrence(tmp_path, change):
    db = migrated_db(tmp_path)
    add_user(db)
    details = dict(description=WORKSHOP_DESCRIPTION, venue="Workshop Hall", neighborhood="New York",
                   attendance_mode="in-person")
    _listing(db, "https://www.meetup.com/builders/events/12345/", title="Physical AI Workshop", **details)
    _listing(db, "https://www.meetup.com/builders/events/67890/", title="NYC Physical AI Workshop",
             **(details | change))
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("description", [
    "Seats are limited, [Pre-registration is mandatory.](https://voxel51.com/events/workshop)",
    "Please register soon.\n\n[Register to reserve your spot.](https://voxel51.com/events/workshop)",
    "## Old link (do not use)\n\nRegister on the [event website](https://voxel51.com/events/workshop).\n\n"
    "Build a robot detector with live data and a guided training session.",
])
def test_boilerplate_or_contextual_registration_cannot_join_different_titles(tmp_path, description):
    db = migrated_db(tmp_path)
    add_user(db)
    for number, title in ((12345, "Physical AI Workshop"), (67890, "NYC Physical AI Workshop")):
        _listing(db, f"https://www.meetup.com/builders/events/{number}/", title=title, description=description,
                 venue="Workshop Hall", neighborhood="New York", attendance_mode="in-person")
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
def test_confirmed_title_alias_cancellation_preserves_distinct_parallel_session(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    details = dict(description=WORKSHOP_DESCRIPTION, venue="Workshop Hall", neighborhood="New York",
                   attendance_mode="in-person")
    _listing(db, "https://www.meetup.com/builders/events/12345/", title="Physical AI Workshop", **details)
    _listing(db, "https://www.meetup.com/builders/events/67890/", title="NYC Physical AI Workshop",
             state="canceled", **details)
    parallel = _listing(db, "https://www.meetup.com/builders/events/98765/", title="Parallel Vision Session",
                        **(details | {"description": WORKSHOP_DESCRIPTION.replace("robot detector", "vision model")}))
    assert [event['id'] for event in web_events(db)] == [parallel]
    assert [event.id for event in agent_events(db)] == [parallel]
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3


@freeze_time("2026-10-06T12:00:00Z")
def test_affirmative_event_registration_cta_joins_cross_posted_meetup_groups(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    registration = "https://voxel51.com/events/synthetic-physical-ai-meetup"
    for number, extra in ((12345, f"[ ]({registration})"), (67890, " "), (98765, " ")):
        description = ("Three speakers present robot learning and video search.\n\n"
                       f"**[Register to reserve your spot.]({registration})**{extra}Seats are limited.\n\n"
                       "Bring your questions for the speakers.")
        _listing(db, f"https://www.meetup.com/group-{number}/events/{number}/",
                 description=description, host=f"Group {number}", venue="Synthetic Hall", neighborhood="Brooklyn")
    _listing(db, "https://luma.com/other", source="luma", title="Different event", score=.7)
    assert len(web_events(db)) == 2
    assert {event.title for event in agent_events(db, limit=2)} == {"Agent Builder Lab", "Different event"}
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("description", [
    "An afternoon workshop.\n\nSeats are limited, **[Pre-registration is mandatory.]"
    "(https://voxel51.com/events/synthetic-workshop)**\n\nBring a laptop.",
    "**Important**: Register on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714) "
    "to receive the joining link. (rsvp on meetup will NOT receive anything).\n\n"
    "A virtual workshop.\n\n* Next month: [RSVP](https://www.aicamp.ai/event/eventdetails/Other123)",
])
def test_mandatory_or_joining_link_registration_assertions_join_cross_posts(tmp_path, description):
    db = migrated_db(tmp_path)
    add_user(db)
    for number in (12345, 67890):
        _listing(db, f"https://www.meetup.com/builders/events/{number}/", description=description)
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("change", ["title", "start", "registration", "registration_case"])
def test_registration_cta_keeps_distinct_event_occurrences_separate(tmp_path, change):
    db = migrated_db(tmp_path)
    add_user(db)
    registration = "https://voxel51.com/events/physical-ai"
    for number in (12345, 67890):
        url = registration
        title, start = "Agent Builder Lab", START
        if number == 67890:
            if change == "title":
                title = "Separate workshop"
            elif change == "start":
                start = "2026-10-08T18:00:00-04:00"
            elif change == "registration":
                url = "https://voxel51.com/events/different-event"
            else:
                url = "https://voxel51.com/events/Physical-AI"
        _listing(db, f"https://www.meetup.com/builders/events/{number}/", title=title, start=start,
                 description=f"[Register to reserve your spot.]({url}) Seats are limited.")
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("url", [
    "https://voxel51.com/events",
    "https://voxel51.com/events/event?session=different",
    "https://voxel51.com/events/event?utm_source=friend",
    "https://voxel51.com/events/nested/event",
    "http://voxel51.com/events/event",
    "https://voxel51.com.evil.test/events/event",
    "https://secret@voxel51.com/events/event",
    "https://voxel51.com:443/events/event",
    "https://voxel51.com:0/events/event",
])
def test_unsupported_registration_urls_are_not_cross_post_identity(tmp_path, url):
    db = migrated_db(tmp_path)
    add_user(db)
    for number in (12345, 67890):
        _listing(db, f"https://www.meetup.com/builders/events/{number}/",
                 description=f"[Register to reserve your spot.]({url}) Seats are limited.")
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("paragraph", [
    "## Old link (do not use)\n\nImportant: Register on the [event website]({url}) "
    "to receive the joining link. (rsvp on meetup will NOT receive anything).",
    "Old link (do not use):\n\nImportant: Register on the [event website]({url}) "
    "to receive the joining link. (rsvp on meetup will NOT receive anything).",
    "## Old link (do not use)\n\nSeats are limited, [Pre-registration is mandatory.]({url})",
    "Seats are limited, [Pre-registration is mandatory.]({url}) (old link, do not use).",
    "[Pre-registration is NOT mandatory.]({url})",
    "## Old link (do not use)\n\n[Register to reserve your spot.]({url}) Seats are limited.",
    "Old link (do not use):\n\n[Register to reserve your spot.]({url}) Seats are limited.",
    "Do not [Register to reserve your spot.]({url}) Seats are limited.",
    "[Register to reserve your spot.]({url}) (old link, do not use).",
    '[Register to reserve your spot.]({url} "Old link, do not use")',
    "> [Register to reserve your spot.]({url}) Seats are limited.",
    "```\n[Register to reserve your spot.]({url}) Seats are limited.\n```",
    "[Register to reserve your spot.]({url}) [Other event](https://luma.com/other).",
    "<blockquote>\n\n[Register to reserve your spot.]({url}) Seats are limited.\n\n</blockquote>",
])
def test_nonaffirmative_or_quoted_registration_cta_cannot_hide_a_listing(tmp_path, paragraph):
    db = migrated_db(tmp_path)
    add_user(db)
    url = "https://voxel51.com/events/physical-ai"
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=paragraph.format(url=url))
    _listing(db, url, source="other", state="canceled")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_confirmed_cross_post_cancellation_hides_only_its_matching_occurrence(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    url = "https://voxel51.com/events/physical-ai"
    description = f"[Register to reserve your spot.]({url}) Seats are limited."
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://www.meetup.com/builders/events/67890/", description=description, state="canceled")
    _listing(db, "https://www.meetup.com/builders/events/98765/", description=description,
             start="2026-10-08T18:00:00-04:00", state="waitlist")
    assert len(web_events(db)) == len(agent_events(db)) == 1
    assert agent_events(db)[0].registration_availability == "waitlist"


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("intro", ["", "**Important**: ", "**Important Note:** "])
@pytest.mark.parametrize("tail", ["", " RSVP on meetup is turned off."])
def test_meetup_cross_posts_join_on_explicit_registration_link_without_venue(tmp_path, intro, tail):
    db = migrated_db(tmp_path)
    add_user(db)
    description = (intro + "Register on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714)"
                   "[ ](https://www.aicamp.ai/event/eventdetails/W2026101215) is required for admission." + tail)
    for number in (12345, 67890, 98765):
        _listing(db, f"https://www.meetup.com/builders/events/{number}/", description=description)
    _listing(db, "https://luma.com/other", source="luma", title="Different event", score=.7)
    assert len(web_events(db)) == 2
    assert {event.title for event in agent_events(db, limit=2)} == {"Agent Builder Lab", "Different event"}
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("separator", [" ", "[ ](https://www.aicamp.ai/event/eventdetails/Wrong456) "])
@pytest.mark.parametrize("tail", ["", "\n(RSVP on meetup is turned off)"])
def test_explicit_alternative_slug_joins_meetup_to_luma_without_host_guessing(tmp_path, separator, tail):
    db = migrated_db(tmp_path)
    add_user(db)
    description = ("**Important Note:** Register on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714)" + separator +
                   "(or on [luma](https://luma.com/builders)) is required for admission." + tail)
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/builders", source="luma")
    _listing(db, "https://www.meetup.com/other/events/55555/",
             description="Register on the [event website](https://www.aicamp.ai/event/eventdetails/Wrong456).")
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
def test_embedded_luma_id_joins_organizer_and_provider_listing(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    _listing(db, "https://www.betaworks.com/event/builders", source="betaworks",
             registration_url="https://lu.ma/event/evt-Example123")
    _listing(db, "https://luma.com/builders", source="luma",
             registration_url="https://luma.com/event/evt-Example123")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("description", [
    "Sponsor: [event website](https://www.aicamp.ai/event/eventdetails/W2026100714)",
    "Register for this meeting via our group. Sponsor: [event website](https://www.aicamp.ai/event/eventdetails/W2026100714)",
    "Do not register on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714); that link is stale.",
    "Please do not register on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714).",
    "Do not\nregister on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714).",
    "Registration: [Register](https://www.aicamp.ai/event/eventdetails/W2026100714) (old link, do not use).",
    "Sponsor: [Register for Event](https://www.aicamp.ai/event/eventdetails/W2026100714)",
    "Register on the [ ](https://www.aicamp.ai/event/eventdetails/W2026100714).",
    "Register on the [event website](http://www.aicamp.ai/event/eventdetails/W2026100714).",
    "Register on the [event website](https://aicamp.ai.evil.test/event/eventdetails/W2026100714).",
    "Register on the [event website](https://token@www.aicamp.ai/event/eventdetails/W2026100714).",
    "Register on the [event website](https://www.aicamp.ai:443/event/eventdetails/W2026100714).",
    "Register on the [event website](https://www.aicamp.ai/event/eventdetails/).",
])
def test_incidental_blank_or_unsafe_links_do_not_join_listings(tmp_path, description):
    db = migrated_db(tmp_path)
    add_user(db)
    for number in (12345, 67890):
        _listing(db, f"https://www.meetup.com/builders/events/{number}/", description=description)
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("label", ["Register", "Register for Event", "RSVP"])
def test_direct_registration_cta_is_evidence_without_preceding_registration_word(tmp_path, label):
    db = migrated_db(tmp_path)
    add_user(db)
    description = f"Tickets: [{label}](https://www.aicamp.ai/event/eventdetails/W2026100714)"
    for number in (12345, 67890):
        _listing(db, f"https://www.meetup.com/builders/events/{number}/", description=description)
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_unrelated_sponsor_link_cannot_propagate_cancellation_to_current_event(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    description = ("Register on the [event website](https://www.aicamp.ai/event/eventdetails/Current123). "
                   "Sponsor: [luma](https://luma.com/unrelated)")
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/unrelated", source="luma", state="canceled")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("continuation", [
    ". Sponsor: ", " (or do not register on ", "\nFor another event, (or on ",
])
def test_interrupted_or_negated_alternative_is_not_registration_evidence(tmp_path, continuation):
    db = migrated_db(tmp_path)
    add_user(db)
    description = ("Register on the [event website](https://www.aicamp.ai/event/eventdetails/Current123)"
                   + continuation + "[luma](https://luma.com/unrelated)")
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/unrelated", source="luma", state="canceled")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_registration_match_normalizes_title_and_utc_instant(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    url = "https://lu.ma/event/evt-Example123"
    _listing(db, "https://luma.com/builders", source="luma", registration_url=url)
    _listing(db, "https://www.betaworks.com/event/builders", source="betaworks",
             title=" AGENT   builder lab ", start="2026-10-07T22:00:00Z", registration_url=url)
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_same_registration_link_still_requires_title_and_occurrence_agreement(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    url = "https://lu.ma/event/evt-Example123"
    _listing(db, "https://luma.com/builders", source="luma", registration_url=url)
    _listing(db, "https://www.betaworks.com/event/another-title", source="betaworks",
             title="Different event", registration_url=url)
    _listing(db, "https://www.betaworks.com/event/next-date", source="betaworks",
             start="2026-10-08T18:00:00-04:00", registration_url=url)
    assert len(web_events(db)) == len(agent_events(db)) == 3


@freeze_time("2026-10-06T12:00:00Z")
def test_cancellation_on_explicit_cross_post_hides_same_occurrence(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    description = "Register on the [event website](https://www.aicamp.ai/event/eventdetails/W2026100714)."
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description, state="canceled")
    _listing(db, "https://www.meetup.com/builders/events/67890/", description=description, state="open")
    assert web_events(db) == []
    assert agent_events(db) == []


def test_missing_registration_metadata_does_not_erase_evidence_until_occurrence_changes(tmp_path):
    db = migrated_db(tmp_path)
    raw = RawEvent(source="luma", title="Builder Lab", url="https://luma.com/builders",
                   starts_at=datetime.fromisoformat(START), registration_url="https://lu.ma/event/evt-Example123")
    with closing(connect(db)) as conn:
        event_id = upsert_raw(conn, raw)
        upsert_raw(conn, raw.model_copy(update={"registration_url": None}))
        assert conn.execute("SELECT registration_url FROM events WHERE id = ?", (event_id,)).fetchone()[0] == raw.registration_url
        replacement = "https://lu.ma/event/evt-Replacement456"
        upsert_raw(conn, raw.model_copy(update={"registration_url": replacement}))
        upsert_raw(conn, raw.model_copy(update={"registration_url": None}))
        assert conn.execute("SELECT registration_url FROM events WHERE id = ?", (event_id,)).fetchone()[0] == replacement
        upsert_raw(conn, raw.model_copy(update={"starts_at": datetime.fromisoformat("2026-10-08T18:00:00-04:00"),
                                              "registration_url": None}))
        assert conn.execute("SELECT registration_url FROM events WHERE id = ?", (event_id,)).fetchone()[0] is None


@freeze_time("2026-10-06T12:00:00Z")
def test_venue_only_refresh_preserves_registration_identity_and_cancellation(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    url = "https://lu.ma/event/evt-Example123"
    _listing(db, "https://luma.com/builders", source="luma", registration_url=url)
    organizer_url = "https://www.betaworks.com/event/builders"
    _listing(db, organizer_url, source="betaworks", registration_url=url)
    with closing(connect(db)) as conn:
        organizer_id = upsert_raw(conn, RawEvent(
            source="betaworks", title=" Agent   Builder Lab ", url=organizer_url,
            starts_at=datetime.fromisoformat("2026-10-07T22:00:00Z"), venue="Betaworks",
        ))
        update_fit(conn, organizer_id, fit_score=.8, reasoning="Synthetic shared fit", urgency="medium",
                   urgency_reason=None, prompt_version="v2", taste_sha="synthetic")
        assert conn.execute("SELECT registration_url FROM events WHERE id = ?", (organizer_id,)).fetchone()[0] == url
    assert len(web_events(db)) == len(agent_events(db)) == 1
    _listing(db, "https://luma.com/builders", source="luma", registration_url=url, state="canceled")
    assert web_events(db) == agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("example", [
    "    [Register](https://luma.com/old)",
    "\t[Register](https://luma.com/old)",
    "```\n\n[Register](https://luma.com/old)\n\n```",
    "~~~\n\n[Register](https://luma.com/old)\n\n~~~",
    "`[Register](https://luma.com/old)`",
    "> [Register](https://luma.com/old)",
])
def test_markdown_examples_cannot_propagate_cancellation(tmp_path, example):
    db = migrated_db(tmp_path)
    add_user(db)
    description = ("Old link (do not use):\n\n" + example +
                   "\n\nRegister on [luma](https://luma.com/current).")
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/old", source="luma", state="canceled")
    _listing(db, "https://luma.com/current", source="luma")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("warning", ["Old link (do not use):", "## Old link (do not use)"])
@pytest.mark.parametrize("current", ["", "\n\nRegister on [luma](https://luma.com/current)."])
@pytest.mark.parametrize("prefix", ["", "Tickets: ", "Registration: "])
def test_bare_cta_with_other_document_context_is_not_registration_evidence(tmp_path, warning, current, prefix):
    db = migrated_db(tmp_path)
    add_user(db)
    description = warning + "\n\n" + prefix + "[Register](https://luma.com/old)" + current
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/old", source="luma", state="canceled")
    if current:
        _listing(db, "https://luma.com/current", source="luma")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_conflicting_registration_paragraphs_do_not_imply_alternative_identity(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    description = ("Register on [luma](https://luma.com/old).\n\n"
                   "Register on [luma](https://luma.com/current).")
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/old", source="luma", state="canceled")
    _listing(db, "https://luma.com/current", source="luma")
    # Ambiguous assertions retain separate records rather than hide a live event.
    assert len(web_events(db)) == len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
def test_repeated_same_registration_assertion_remains_unambiguous(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    description = ("Register on [luma](https://luma.com/current).\n\n"
                   "Register on [luma](https://lu.ma/current).")
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/current", source="luma")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("description", [
    '[Register](https://luma.com/old "Old link, do not use")',
    'Register on [luma](https://luma.com/old "Old link, do not use").',
    '<blockquote>\n\nRegister on [luma](https://luma.com/old).\n\n</blockquote>',
])
def test_unsupported_link_metadata_or_html_context_is_not_identity(tmp_path, description):
    db = migrated_db(tmp_path)
    add_user(db)
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=description)
    _listing(db, "https://luma.com/old", source="luma", state="canceled")
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("description", [None, "", " \n "])
@pytest.mark.parametrize("venue", [None, "New venue"])
def test_absent_meetup_description_preserves_registration_evidence(tmp_path, description, venue):
    db = migrated_db(tmp_path)
    add_user(db)
    original = "Register on [luma](https://luma.com/old)."
    _listing(db, "https://www.meetup.com/builders/events/12345/", description=original)
    _listing(db, "https://luma.com/old", source="luma", state="canceled")
    with closing(connect(db)) as conn:
        event_id = upsert_raw(conn, RawEvent(
            source="meetup", title="Agent Builder Lab", url="https://www.meetup.com/builders/events/12345/",
            starts_at=datetime.fromisoformat(START), description=description, venue=venue,
        ))
        update_fit(conn, event_id, fit_score=.8, reasoning="Synthetic shared fit", urgency="medium",
                   urgency_reason=None, prompt_version="v2", taste_sha="synthetic")
    assert web_events(db) == agent_events(db) == []


@pytest.mark.parametrize("change", [
    {"title": "Different event"},
    {"starts_at": datetime.fromisoformat("2026-10-08T18:00:00-04:00")},
])
def test_changed_occurrence_does_not_preserve_missing_meetup_description(tmp_path, change):
    db = migrated_db(tmp_path)
    _listing(db, "https://www.meetup.com/builders/events/12345/",
             description="Register on [luma](https://luma.com/old).")
    with closing(connect(db)) as conn:
        raw = RawEvent(
            source="meetup", title="Agent Builder Lab", url="https://www.meetup.com/builders/events/12345/",
            starts_at=datetime.fromisoformat(START), description=None,
        )
        event_id = upsert_raw(conn, raw.model_copy(update=change))
        assert conn.execute("SELECT description FROM events WHERE id = ?", (event_id,)).fetchone()[0] is None


@freeze_time("2026-10-06T12:00:00Z")
def test_new_meetup_description_replaces_old_registration_evidence(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    url = "https://www.meetup.com/builders/events/12345/"
    _listing(db, url, description="Register on [luma](https://luma.com/old).")
    _listing(db, "https://luma.com/old", source="luma", state="canceled")
    _listing(db, "https://luma.com/current", source="luma")
    _listing(db, url, description="Register on [luma](https://luma.com/current).")
    assert len(web_events(db)) == len(agent_events(db)) == 1
