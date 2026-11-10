"""Minimal event-owned registration forms; never real accounts or ticket data."""


def registration_form(url: str, controls: str) -> str:
    """Tie synthetic registration controls to the event being fetched."""
    return f'<form action="{url}">{controls}</form>'
