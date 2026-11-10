from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.repositories.identities import link_identity
from api.repositories.invites import create_alpha_invite
from api.repositories.migrate import apply_pending, migration_dir_for_dialect
from api.repositories.users import create_user
from api.services.sessions import make_serializer, new_session


def main() -> None:
    db_path = Path(os.environ.get("DATABASE_PATH", "data/e2e-events.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        apply_pending(conn, migration_dir_for_dialect("sqlite"))
        user = create_user(conn, email="discovery@example.test", display_name="Test Friend",
                           invite_code="e2e-discovery-only")
        create_alpha_invite(conn, email="discovery@example.test")
        link_identity(conn, provider="supabase", subject="synthetic-subject", user_id=user["id"],
                      email="discovery@example.test", email_verified=True)
        session_path = db_path.parent / "e2e-session.json"
        revocation_user = create_user(conn, email="revocation@example.test", display_name="Revocation fixture",
                                      invite_code="revocation-fixture-only")
        session_path.write_text(json.dumps({
            "cookie": make_serializer(os.environ["NYC_AI_EVENTS_SESSION_SECRET"]).dumps(new_session(user["id"])),
            "revocation_cookie": make_serializer(os.environ["NYC_AI_EVENTS_SESSION_SECRET"]).dumps(new_session(revocation_user["id"])),
            "supabase_access": "synthetic-web-access", "supabase_refresh": "synthetic-web-refresh",
        }))
        today = datetime.now(ZoneInfo("America/New_York")).replace(hour=8, minute=30, second=0, microsecond=0)
        for title, hour, minute, score, mode in [
            ("Morning builders", 8, 30, .9, "in-person"),
            ("Evening demos", 18, 0, .8, "mixed"),
            ("Online workshop", 7, 31, None, "online"),
        ]:
            start = today.replace(hour=hour, minute=minute)
            if mode == "online":
                start += timedelta(days=1)
            description = ("Build working agents with mentors. Lunch provided. Limited spots available."
                           if title == "Morning builders" else
                           "Panel with Dick Hardt, OAuth 2.0 co-author. Pizza provided.")
            event = upsert_raw(conn, RawEvent(source="luma", title=title,
                              url=f"https://example.test/{mode}", starts_at=start,
                              venue="Test venue" if mode != "online" else None,
                              attendance_mode=mode, host="Synthetic hosts", description=description,
                              registration_availability="waitlist" if title == "Evening demos" else "unknown",
                              availability_evidence="fixture: Join Waitlist" if title == "Evening demos" else None))
            if score is not None:
                update_fit(conn, event, fit_score=score, reasoning="Not the highlight",
                           urgency="high", urgency_reason="Last places available",
                           prompt_version="v3", taste_sha="e2e", food_provided="yes",
                           highlight={"text": "Build working agents with mentors" if title == "Morning builders" else
                                      "Panel with Dick Hardt, OAuth 2.0 co-author",
                                      "evidence": description.split(". Lunch")[0] if title == "Morning builders" else
                                      "Panel with Dick Hardt, OAuth 2.0 co-author"},
                           urgency_note={"kind": "scarcity", "text": "Limited spots",
                                         "evidence": "Limited spots available."} if title == "Morning builders" else None)
        duplicate = upsert_raw(conn, RawEvent(source="eventbrite", title="Morning builders",
                               url="https://example.test/duplicate-morning", starts_at=today,
                               venue="Test venue", host="Synthetic hosts", attendance_mode="in-person"))
        update_fit(conn, duplicate, fit_score=.8, reasoning="Duplicate source",
                   urgency="medium", urgency_reason=None, prompt_version="e2e", taste_sha="e2e")
        for status in ("full", "closed", "canceled"):
            upsert_raw(conn, RawEvent(source="luma", title=f"Unavailable {status}",
                       url=f"https://example.test/{status}", starts_at=today,
                       registration_availability=status, availability_evidence=f"fixture: {status}"))
        event_id = upsert_raw(
            conn,
            RawEvent(
                source="luma",
                url="https://example.test/e2e-public-alpha-night",
                title="E2E Public Alpha Night",
                starts_at=(today + timedelta(days=3)).replace(hour=11, minute=59),
                venue="Cerebral Valley",
                host="AI Builders",
            ),
        )
        update_fit(
            conn,
            event_id,
            fit_score=0.91,
            reasoning="Seeded event for private-access e2e coverage",
            urgency="medium",
            urgency_reason=None,
            prompt_version="e2e",
            taste_sha="e2e",
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
