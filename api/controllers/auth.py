"""Auth controllers — login, logout, OAuth callbacks.

Currently re-exports from the legacy web app while route bodies remain in
``api.app``. Physical extraction is deferred until after the
routes are stabilized in the new layout.
"""
from api.app import (  # noqa: F401
    google_callback,
    google_start,
    login,
    login_form,
    logout,
)
