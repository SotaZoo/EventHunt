from __future__ import annotations

from pathlib import Path

COMMON_REQUIRED_ENV_KEYS = (
    "DATABASE_PATH",
    "NYC_AI_EVENTS_SESSION_SECRET",
)
PRODUCTION_REQUIRED_ENV_KEYS = (
    "NYC_AI_EVENTS_PUBLIC_BASE_URL",
    "NYC_AI_EVENTS_GOOGLE_CLIENT_ID",
    "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET",
)
REQUIRED_ENV_KEYS_BY_ENV = {
    "staging": COMMON_REQUIRED_ENV_KEYS,
    "production": COMMON_REQUIRED_ENV_KEYS + PRODUCTION_REQUIRED_ENV_KEYS,
}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def required_keys_for_env(env_name: str) -> tuple[str, ...]:
    try:
        return REQUIRED_ENV_KEYS_BY_ENV[env_name]
    except KeyError as exc:
        raise ValueError(f"unknown env: {env_name}") from exc


def check_env_file(path: Path, env_name: str) -> list[str]:
    values = parse_env_file(path)
    return [
        key
        for key in required_keys_for_env(env_name)
        if not values.get(key, "").strip()
    ]

