"""Read-only discovery catalog, independent of historical personal statuses."""
from datetime import datetime
from typing import Any

from api.repositories.catalog import catalog_rows, visible_occurrences


def upcoming_events(conn: Any, since: datetime) -> list[Any]:
    """Return every dated, eligible event since the supplied start-of-day boundary."""
    # No top-N limit: the browser's count, date indicators and filter preview
    # must all describe the complete upcoming catalog, not a ranked prefix.
    return visible_occurrences(catalog_rows(conn), since=since)
