"""Wake the configured hosted application without transmitting operator credentials."""

from urllib.parse import urlsplit

import httpx

HOST_WAKE_TIMEOUT_SECONDS = 90


def wake_hosted_sender(base_url: str) -> bool:
    """Request an ordinary health check; success does not establish mail delivery."""
    try:
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.netloc or url.username or url.password
                or url.query or url.fragment or url.path not in {"", "/"}):
            return False
        with httpx.Client(timeout=HOST_WAKE_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.get(base_url.rstrip("/") + "/healthz")
    except (ValueError, httpx.InvalidURL, httpx.HTTPError):
        return False
    return response.status_code == 200
