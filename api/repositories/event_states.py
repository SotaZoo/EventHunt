"""User-event state (status, rating, dismissal)."""
from api.repositories._storage import (  # noqa: F401
    clear_user_event_state,
    ensure_user_event_state,
    fetch_events_for_user,
    get_previous_user_status,
    set_user_event_rating,
    set_user_event_status,
)
