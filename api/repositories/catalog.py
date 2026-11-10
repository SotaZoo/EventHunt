"""Shared cleaned discovery read model, before client counts and limits."""
from datetime import datetime
from typing import Any

from api.models.availability import TERMINAL_AVAILABILITY
from api.models.occurrences import CatalogRow, occurrence_groups, provider_identity, utc_instant


def catalog_rows(conn: Any) -> list[CatalogRow]:
    """Read complete source evidence before applying discovery or refresh windows."""
    # Historical counterparts can corroborate a cancellation for a current alias.
    return [dict(row) for row in conn.execute("SELECT * FROM events").fetchall()]


def excluded_source_identities(rows: list[CatalogRow]) -> frozenset[tuple[str, str]]:
    """Keep terminal source identities sticky across uncertain clock-drift aliases."""
    canceled = {provider_identity(row["url"]) for row in rows
                if row["cancelled_at"] or row["registration_availability"] == "canceled"}
    groups = [{provider_identity(row["url"]) for row in group} for group in occurrence_groups(rows)]
    # Carry actual cancellation through corroborated counterparts and their aliases.
    # A changed source clock is not positive evidence of a new occurrence.
    previous_size = -1
    while len(canceled) != previous_size:
        previous_size = len(canceled)
        for identities in groups:
            if not identities.isdisjoint(canceled):
                canceled.update(identities)
    return frozenset(canceled | {provider_identity(row["url"]) for row in rows
                                 if row["registration_availability"] in TERMINAL_AVAILABILITY})


def visible_occurrences(rows: list[CatalogRow], *, since: datetime,
                        until: datetime | None = None, scored_only: bool = False) -> list[CatalogRow]:
    """Choose one usable listing per confidently matched, noncanceled occurrence."""
    result: list[CatalogRow] = []
    excluded = excluded_source_identities(rows)
    warnings_by_source: dict[tuple[str, str], CatalogRow] = {}
    for row in rows:
        if row.get('time_warning'):
            identity = provider_identity(row['url'])
            previous = warnings_by_source.get(identity)
            if previous is None or str(row['time_warning_reviewed_at']) > str(previous['time_warning_reviewed_at']):
                warnings_by_source[identity] = row
    for group in occurrence_groups(rows):
        if any(row.get("cancelled_at") or row["registration_availability"] == "canceled" for row in group):
            continue
        terminal_sources = {provider_identity(row["url"]) for row in group
                            if provider_identity(row["url"]) in excluded}
        usable = [row for row in group if provider_identity(row["url"]) not in terminal_sources
                  and (not terminal_sources or row["registration_availability"] in {"open", "waitlist"})]
        candidates = []
        for row in usable:
            instant = utc_instant(row["start_at"])
            if instant is None or instant < since or (until is not None and instant > until):
                continue
            eligible = (row["scoring_status"] == "scored" and row["fit_score"] is not None
                        if scored_only else row["scoring_status"] in {"pending", "scored"})
            if eligible:
                candidates.append(row)
        if candidates:
            selected = dict(max(candidates, key=lambda row: (
                {"open": 2, "waitlist": 1}.get(row["registration_availability"], 0),
                float(row["fit_score"]) if row["fit_score"] is not None else -1,
                str(row["last_seen"]), int(row["id"]),
            )))
            # A changed source clock must not make a new URL alias look verified.
            warnings = [warnings_by_source[provider_identity(row['url'])] for row in group
                        if provider_identity(row['url']) in warnings_by_source]
            if warnings:
                selected['time_warning'] = max(warnings, key=lambda row: str(row['time_warning_reviewed_at']))['time_warning']
            result.append(selected)
    return sorted(result, key=lambda row: (utc_instant(row["start_at"]), row["id"]))
