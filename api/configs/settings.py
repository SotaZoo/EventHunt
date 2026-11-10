"""Runtime configuration — dev-mode switches, paths, and other env-var reads.

The three dev modes (DX X5 in the design doc) make it possible to run the test
suite, the eval harness, and the web UI without pi-auth, Discord, or live
scrapers being available. They are read on demand so tests can flip them with
``monkeypatch.setenv``.

Env vars:

- ``LLM_SCORING_MODE``: ``codex-cli`` (default) | ``pi-auth`` | ``cli`` | ``mock`` | ``api``
    - ``codex-cli``: ``codex exec`` subprocess scorer.
    - ``mock``: return canned ``EventFit`` from ``api/tests/fixtures/scoring.json``.
    - ``pi-auth``: read ``~/.pi/auth.json`` and call Claude via OAuth bearer.
    - ``cli``: ``claude -p`` subprocess fallback.
    - ``api``: Anthropic Messages API.
- ``CLAUDE_OAUTH_MODE``: deprecated alias for ``LLM_SCORING_MODE``.
- ``DISCORD_MODE``: ``live`` (default) | ``stub`` (log to stdout, no DM).
- ``SCRAPER_MODE``: ``live`` (default) | ``fixture`` (read from
  ``api/tests/fixtures/<source>.html`` instead of the network).
- ``DATABASE_PATH``: canonical SQLite path. ``DB_PATH`` is accepted as a
  deprecated fallback for one release. Default is ``data/events.db``.
- ``NYC_AI_EVENTS_PUBLIC_BASE_URL``: HTTPS URL friends use for OAuth callbacks.
- ``NYC_AI_EVENTS_FRIEND_MODE``: ``1`` enables fail-closed shared-alpha startup checks.
- ``NYC_AI_EVENTS_GOOGLE_CLIENT_ID`` / ``NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET``:
  Google OAuth client credentials for friends-alpha sign-in.
- ``NYC_AI_EVENTS_AGENT_API_TOKEN``: private bearer credential that enables the
  read-only Event Hunt MCP endpoint. When absent, the endpoint is not mounted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from sqlalchemy.engine import make_url

ScoringMode = Literal["pi-auth", "cli", "mock", "api", "codex-cli"]
ClaudeOAuthMode = ScoringMode
DiscordMode = Literal["live", "stub"]
ScraperMode = Literal["live", "fixture"]
DatabaseDialect = Literal["sqlite", "postgres"]
McpAuthMode = Literal["disabled", "static", "supabase"]
MIN_SESSION_SECRET_LENGTH = 32
MIN_AGENT_API_TOKEN_LENGTH = 32
PLACEHOLDER_SECRET_PREFIXES = (
    "replace-with",
    "change-me",
    "changeme",
)
PLACEHOLDER_SECRET_VALUES = {
    "secret",
    "test-secret",
    "qa-secret",
    "client-secret",
}
PLACEHOLDER_CONFIG_VALUES = {
    "...",
    "todo",
    "tbd",
    "client",
    "client-id",
    "client-secret",
    "google-oauth-client-id",
    "google-oauth-client-secret",
}


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    dialect: DatabaseDialect
    redacted_url: str


def llm_scoring_mode() -> ScoringMode:
    value = os.environ.get("LLM_SCORING_MODE") or os.environ.get("CLAUDE_OAUTH_MODE", "codex-cli")
    if value not in ("pi-auth", "cli", "mock", "api", "codex-cli"):
        raise ValueError(
            f"LLM_SCORING_MODE={value!r} is not one of pi-auth | codex-cli | cli | mock | api"
        )
    return value  # type: ignore[return-value]


def claude_oauth_mode() -> ClaudeOAuthMode:
    """Deprecated compatibility alias for ``llm_scoring_mode``."""
    return llm_scoring_mode()


def discord_mode() -> DiscordMode:
    value = os.environ.get("DISCORD_MODE", "live")
    if value not in ("live", "stub"):
        raise ValueError(f"DISCORD_MODE={value!r} is not one of live | stub")
    return value  # type: ignore[return-value]


def scraper_mode() -> ScraperMode:
    value = os.environ.get("SCRAPER_MODE", "live")
    if value not in ("live", "fixture"):
        raise ValueError(f"SCRAPER_MODE={value!r} is not one of live | fixture")
    return value  # type: ignore[return-value]


def database_path() -> Path:
    return Path(os.environ.get("DATABASE_PATH") or os.environ.get("DB_PATH") or "data/events.db")


def db_path() -> str:
    return str(database_path())


def session_secret_config_error(value: str | None) -> str | None:
    if not value:
        return "NYC_AI_EVENTS_SESSION_SECRET is required"
    stripped = value.strip()
    normalized = stripped.lower()
    if (
        len(stripped) < MIN_SESSION_SECRET_LENGTH
        or normalized in PLACEHOLDER_SECRET_VALUES
        or normalized.startswith(PLACEHOLDER_SECRET_PREFIXES)
    ):
        return (
            "NYC_AI_EVENTS_SESSION_SECRET must be a private random value "
            f"with at least {MIN_SESSION_SECRET_LENGTH} characters"
        )
    return None


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        "replace-with" in normalized
        or normalized in PLACEHOLDER_CONFIG_VALUES
        or normalized.startswith(PLACEHOLDER_SECRET_PREFIXES)
    )


def public_base_url_config_error(value: str | None = None) -> str | None:
    raw = public_base_url() if value is None else value.strip().rstrip("/")
    if not raw:
        return "NYC_AI_EVENTS_PUBLIC_BASE_URL is required"
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return "NYC_AI_EVENTS_PUBLIC_BASE_URL must be a valid HTTPS URL"
    if _looks_like_placeholder(raw):
        return "NYC_AI_EVENTS_PUBLIC_BASE_URL must be replaced with the real public URL"
    return None


def oauth_config_value_error(name: str, value: str | None) -> str | None:
    if not value:
        return f"{name} is required"
    if _looks_like_placeholder(value):
        return f"{name} must be replaced with a real Google OAuth value"
    return None


def _sqlite_url(path: Path) -> str:
    value = str(path)
    if value.startswith("/"):
        return f"sqlite:///{value}"
    return f"sqlite:///{value}"


def _postgres_url_from_split_env() -> str | None:
    user = os.environ.get("PGUSER", "").strip()
    password = os.environ.get("PGPASSWORD", "")
    database = os.environ.get("PGDATABASE", "").strip()
    host = (
        os.environ.get("PGHOST", "").strip()
        or os.environ.get("CLOUD_SQL_INSTANCE_CONNECTION_NAME", "").strip()
    )
    if not any((user, password, database, host)):
        return None
    missing = [
        name
        for name, value in (
            ("PGUSER", user),
            ("PGPASSWORD", password),
            ("PGDATABASE", database),
            ("PGHOST or CLOUD_SQL_INSTANCE_CONNECTION_NAME", host),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"incomplete Postgres database env; missing {', '.join(missing)}")
    if not host.startswith("/"):
        host = f"/cloudsql/{host}"
    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@/{quote(database, safe='')}?host={quote(host, safe='/')}"
    )


def _normalize_database_url(url: str) -> str:
    try:
        parsed = make_url(url)
    except Exception as exc:  # pragma: no cover - exact SQLAlchemy error is not part of API
        raise ValueError(f"invalid DATABASE_URL: {exc}") from exc
    backend = parsed.get_backend_name()
    if backend == "postgresql":
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        return parsed.render_as_string(hide_password=False)
    if backend == "sqlite":
        return parsed.render_as_string(hide_password=False)
    raise ValueError("DATABASE_URL must use sqlite:// or postgresql://")


def redact_database_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database url>"


def database_url() -> str:
    raw_url = os.environ.get("DATABASE_URL", "").strip()
    if raw_url:
        return _normalize_database_url(raw_url)
    split_url = _postgres_url_from_split_env()
    if split_url:
        _normalize_database_url(split_url)
        return split_url
    return _sqlite_url(database_path())


def database_settings() -> DatabaseSettings:
    url = database_url()
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        dialect: DatabaseDialect = "sqlite"
    elif backend == "postgresql":
        dialect = "postgres"
    else:  # pragma: no cover - database_url validates this first
        raise ValueError("unsupported database dialect")
    return DatabaseSettings(url=url, dialect=dialect, redacted_url=redact_database_url(url))


def public_base_url() -> str:
    return os.environ.get("NYC_AI_EVENTS_PUBLIC_BASE_URL", "").strip().rstrip("/")


def supabase_url() -> str:
    """Return the configured Supabase project origin without a trailing slash."""
    return os.environ.get("NYC_AI_EVENTS_SUPABASE_URL", "").strip().rstrip("/")


def supabase_publishable_key() -> str:
    """Return the public Supabase browser/API key."""
    return os.environ.get("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "").strip()


def supabase_url_config_error(value: str | None = None) -> str | None:
    """Return a safe error unless *value* is one exact HTTPS project origin."""
    raw = supabase_url() if value is None else value.strip().rstrip("/")
    if not raw:
        return "NYC_AI_EVENTS_SUPABASE_URL is required"
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return "NYC_AI_EVENTS_SUPABASE_URL must be one HTTPS project origin"
    if _looks_like_placeholder(raw):
        return "NYC_AI_EVENTS_SUPABASE_URL must be replaced with the real project URL"
    return None


def supabase_publishable_key_config_error(value: str | None = None) -> str | None:
    """Return a safe error for a missing or placeholder publishable key."""
    raw = supabase_publishable_key() if value is None else value.strip()
    if not raw:
        return "NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY is required"
    if _looks_like_placeholder(raw):
        return "NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY must be replaced with the real key"
    return None


def supabase_config_errors() -> list[str]:
    """Validate the Supabase settings as one indivisible configuration."""
    errors: list[str] = []
    for validator in (supabase_url_config_error, supabase_publishable_key_config_error):
        error = validator()
        if error is not None:
            errors.append(error)
    return errors


def supabase_configured() -> bool:
    return not supabase_config_errors()


def supabase_issuer_url() -> str:
    return f"{supabase_url()}/auth/v1"


def supabase_jwks_url() -> str:
    return f"{supabase_issuer_url()}/.well-known/jwks.json"


def feedback_email() -> str:
    """Read the approved feedback recipient, with an optional environment override."""
    return os.environ.get("NYC_AI_EVENTS_FEEDBACK_EMAIL", "info@sotazoo.com").strip()


def open_google_signup() -> bool:
    """Allow new Google accounts only when public admission is explicitly enabled."""
    return os.environ.get("EVENT_HUNT_OPEN_GOOGLE_SIGNUP", "").strip() == "1"


def agent_api_token() -> str:
    """Read the private bearer credential that enables the MCP endpoint."""
    return os.environ.get("NYC_AI_EVENTS_AGENT_API_TOKEN", "").strip()


def agent_api_token_config_error(value: str | None = None) -> str | None:
    """Return a safe configuration error for an unusable agent token."""
    raw = agent_api_token() if value is None else value.strip()
    normalized = raw.lower()
    if not raw:
        return "NYC_AI_EVENTS_AGENT_API_TOKEN is required"
    if (
        len(raw) < MIN_AGENT_API_TOKEN_LENGTH
        or normalized in PLACEHOLDER_SECRET_VALUES
        or normalized.startswith(PLACEHOLDER_SECRET_PREFIXES)
    ):
        return (
            "NYC_AI_EVENTS_AGENT_API_TOKEN must be a private random value "
            f"with at least {MIN_AGENT_API_TOKEN_LENGTH} characters"
        )
    return None


def mcp_resource_url(base_url: str | None = None) -> str:
    """Derive the MCP resource URL from Event Hunt's existing public host."""
    base = public_base_url() if base_url is None else base_url.strip().rstrip("/")
    return f"{base}/mcp" if base else ""


def mcp_resource_url_config_error(value: str | None = None) -> str | None:
    """Return a safe configuration error for an unusable MCP resource URL."""
    raw = mcp_resource_url() if value is None else value.strip().rstrip("/")
    if not raw:
        return "NYC_AI_EVENTS_PUBLIC_BASE_URL is required to enable MCP"
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
    ):
        return "Event Hunt MCP resource URL must be an HTTPS URL ending at /mcp"
    if _looks_like_placeholder(raw):
        return "Event Hunt MCP resource URL must use the real public URL"
    return None


def mcp_auth_mode() -> McpAuthMode:
    """Select exactly one MCP authentication provider, failing closed."""
    static_token = agent_api_token()
    has_supabase_value = bool(supabase_url() or supabase_publishable_key())

    if static_token and has_supabase_value:
        raise ValueError(
            "configure exactly one MCP auth provider: NYC_AI_EVENTS_AGENT_API_TOKEN or Supabase"
        )
    if static_token:
        error = agent_api_token_config_error(static_token)
        if error is not None:
            raise ValueError(error)
        return "static"
    if has_supabase_value:
        errors = supabase_config_errors()
        if errors:
            raise ValueError("; ".join(errors))
        return "supabase"
    return "disabled"


def friend_mode_enabled() -> bool:
    return os.environ.get("NYC_AI_EVENTS_FRIEND_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def google_oauth_client_id() -> str:
    return os.environ.get("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", "").strip()


def google_oauth_client_secret() -> str:
    return os.environ.get("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", "").strip()


def google_oauth_config_errors() -> list[str]:
    errors: list[str] = []
    for name, value in (
        ("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", google_oauth_client_id()),
        ("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", google_oauth_client_secret()),
    ):
        error = oauth_config_value_error(name, value)
        if error is not None:
            errors.append(error)
    return errors


def google_oauth_configured() -> bool:
    return not google_oauth_config_errors()


def taste_profile_text(default_path: Path = Path("data/taste-profile.md")) -> str:
    inline = os.environ.get("TASTE_PROFILE_MD")
    if inline:
        return inline
    path = Path(os.environ.get("TASTE_PROFILE_PATH") or default_path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path.read_text()


@dataclass(frozen=True)
class Settings:
    """Snapshot of resolved runtime settings."""

    database_url: str
    public_base_url: str
    friend_mode_enabled: bool


def load_settings() -> Settings:
    """Resolve current settings from the environment."""
    return Settings(
        database_url=database_url(),
        public_base_url=public_base_url(),
        friend_mode_enabled=friend_mode_enabled(),
    )
