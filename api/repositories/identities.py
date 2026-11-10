"""Google identity records."""

from api.repositories._storage import (  # noqa: F401
    create_or_accept_google_user,
    create_or_accept_supabase_user,
    get_user_by_identity,
    link_identity,
)
