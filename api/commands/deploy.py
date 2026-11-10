from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.parser import Parser
from pathlib import Path
from typing import Annotated, Any

import typer

from api.configs.required_env import check_env_file, parse_env_file
from api.repositories.migrate import migration_dir_for_dialect

ENV_CHOICES = ("staging", "production")
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
PACKAGE_NAME = "nyc-ai-events"
PRODUCTION_LAUNCHD_PLISTS = (
    "com.yang.nyc-ai-events.plist",
    "com.yang.nyc-ai-events-cleanup.plist",
    "com.yang.nyc-ai-events-web.plist",
    "com.yang.nyc-ai-events-backup.plist",
    "com.yang.nyc-ai-events.scout-bot.plist",
    "com.yang.nyc-ai-events.capacity-poller.plist",
)
# launchd can briefly retain a label after bootout; keep recovery bounded.
LAUNCHD_BOOTSTRAP_ATTEMPTS = 3
LAUNCHD_BOOTSTRAP_RETRY_SECONDS = 1.0
UV_FALLBACK_DIRS = (
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
    Path.home() / ".local" / "bin",
)
SECRET_KEY_PATTERNS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "WEBHOOK_URL",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "API_KEY",
    "COOKIE",
    "DATABASE_ROW",
    "DB_ROW",
)
_LAUNCHD_FIELD_RE = re.compile(r"^\s*(?P<name>[^=\n]+?)\s*=\s*(?P<value>.*?)\s*$", re.M)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_RUNTIME_DISTRIBUTION_IDENTITY_CODE = """
import importlib.metadata
import json

distribution = importlib.metadata.distribution("nyc-ai-events")
raw_direct_url = distribution.read_text("direct_url.json")
direct_url = json.loads(raw_direct_url) if raw_direct_url else {}
print(json.dumps({
    "version": distribution.version,
    "editable": bool(direct_url.get("dir_info", {}).get("editable")),
}))
""".strip()


@dataclasses.dataclass(frozen=True)
class EnvConfig:
    name: str
    checkout: Path
    env_file: Path
    db_path: Path
    services: tuple[str, ...]
    smoke_services: tuple[str, ...]
    web_port: int
    requires_origin_ancestor_check: bool
    sync_extras: tuple[str, ...]
    log_files: tuple[Path, ...]
    scheduled_services: tuple[str, ...] = ()


class SmokeFailure(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ReleaseArtifact:
    path: Path
    name: str
    version: str
    sha256: str


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def resolve_env(name: str) -> EnvConfig:
    base = Path.home() / ".local/share/nyc-ai-events"
    if name == "staging":
        checkout = base / "app-staging"
        services = ("com.yang.nyc-ai-events-staging-web",)
        return EnvConfig(
            name="staging",
            checkout=checkout,
            env_file=base / ".env.staging",
            db_path=checkout / "data/staging.db",
            services=services,
            smoke_services=services,
            web_port=8848,
            requires_origin_ancestor_check=False,
            sync_extras=("dev",),
            log_files=(
                base / "logs/staging-web.out.log",
                base / "logs/staging-web.err.log",
            ),
            scheduled_services=(),
        )
    if name == "production":
        checkout = base / "app"
        return EnvConfig(
            name="production",
            checkout=checkout,
            env_file=checkout / ".env.friends-alpha",
            db_path=checkout / "data/events.db",
            services=("com.yang.nyc-ai-events-web",),
            smoke_services=(
                "com.yang.nyc-ai-events",
                "com.yang.nyc-ai-events-web",
                "com.yang.nyc-ai-events.scout-bot",
                "com.yang.nyc-ai-events.capacity-poller",
            ),
            web_port=8847,
            requires_origin_ancestor_check=True,
            sync_extras=(),
            log_files=(
                checkout / "data/web.out.log",
                checkout / "data/web.err.log",
            ),
            scheduled_services=(
                "com.yang.nyc-ai-events",
                "com.yang.nyc-ai-events.capacity-poller",
            ),
        )
    raise typer.BadParameter(f"Unknown env: {name!r}. Choices: {ENV_CHOICES}")


def redact_for_log(env_vars: Mapping[str, str]) -> dict[str, str]:
    return {
        key: (
            "***REDACTED***"
            if any(pattern in key.upper() for pattern in SECRET_KEY_PATTERNS)
            else value
        )
        for key, value in env_vars.items()
    }


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
    )


def _resolve_sha(sha: str, checkout: Path) -> str:
    result = _run(["git", "rev-parse", sha], cwd=checkout)
    return result.stdout.strip()


def _verify_sha_on_origin_default_branch(sha: str, checkout: Path) -> None:
    remote_ref = f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"
    try:
        _run(["git", "merge-base", "--is-ancestor", sha, remote_ref], cwd=checkout)
    except subprocess.CalledProcessError as exc:
        raise typer.BadParameter(
            f"Production safety: SHA {sha[:12]} is not an ancestor of {remote_ref}. "
            "Either the SHA is local-only, on a feature branch, or your origin is stale. "
            f"git merge-base --is-ancestor exit={exc.returncode}"
        ) from exc


def _backup_db(env: EnvConfig, sha: str) -> Path:
    return _backup_db_with_label(env, prefix="pre-deploy", label=sha[:12])


def _safe_label(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return safe[:80] or "unknown"


def _backup_db_with_label(env: EnvConfig, *, prefix: str, label: str) -> Path:
    if not env.db_path.exists():
        return env.db_path
    backup_dir = env.checkout / "data/backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{prefix}-{_safe_label(label)}-{timestamp}.db"
    shutil.copy2(env.db_path, backup_path)
    return backup_path


def _migration_sets(db_path: Path) -> tuple[set[str], set[str]]:
    required = {path.name for path in migration_dir_for_dialect("sqlite").glob("*.sql")}
    if not db_path.exists():
        return required, set()
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    finally:
        conn.close()
    return required, {row[0] for row in rows}


def _migration_plan_line(db_path: Path) -> str:
    try:
        required, applied = _migration_sets(db_path)
    except sqlite3.Error as exc:
        return f"migration plan: unavailable ({exc.__class__.__name__})"
    missing = sorted(required - applied)
    unknown = sorted(applied - required)
    if not db_path.exists():
        return f"migration plan: database missing; {len(required)} migration(s) will initialize it"
    return f"migration plan: {len(missing)} pending, {len(unknown)} unknown applied"


def _runtime_env(env_file: Path) -> dict[str, str]:
    runtime_env = os.environ.copy()
    runtime_env.update(parse_env_file(env_file))
    runtime_env.setdefault("PYTHONSAFEPATH", "1")
    return runtime_env


def runtime_deploy_template_dir() -> Path:
    api_root = Path(__file__).resolve().parents[1]
    packaged = api_root / "_runtime" / "deploy"
    if packaged.exists():
        return packaged
    return api_root.parent / "deploy"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _exit_with_error(message: str, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _validate_env_file_matches_config(cfg: EnvConfig) -> None:
    missing = check_env_file(cfg.env_file, cfg.name)
    if missing:
        for key in missing:
            typer.echo(f"MISSING: {key}", err=True)
        raise typer.Exit(1)
    env_values = parse_env_file(cfg.env_file)
    configured_db = _canonical_path(Path(env_values["DATABASE_PATH"]))
    expected_db = _canonical_path(cfg.db_path)
    if configured_db != expected_db:
        typer.echo(
            f"MISMATCH: DATABASE_PATH must be {expected_db} for {cfg.name}; found {configured_db}",
            err=True,
        )
        raise typer.Exit(1)


def _validate_deploy_inputs(cfg: EnvConfig) -> None:
    if not cfg.checkout.exists():
        _exit_with_error(f"Checkout missing: {cfg.checkout}. See doc/administration/deployment.md.")
    if not cfg.env_file.exists():
        _exit_with_error(f"Env file missing: {cfg.env_file}.")
    _validate_env_file_matches_config(cfg)


def _wait_for_health(port: int, timeout_s: int = 30) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/healthz"
    last_error: Exception | str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode() or "{}")
                if response.status == 200 and payload.get("status") == "ok":
                    return
                last_error = f"healthz returned {response.status}: {payload}"
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    _exit_with_error(f"healthz never became OK on :{port} ({last_error})")


def _sync_args(cfg: EnvConfig) -> list[str]:
    args = ["uv", "sync", "--locked"]
    if cfg.sync_extras:
        for extra in cfg.sync_extras:
            args.extend(["--extra", extra])
    else:
        args.append("--no-dev")
    return args


def _normalise_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_release_artifact(
    artifact_path: Path,
    *,
    expected_version: str | None = None,
) -> ReleaseArtifact:
    path = artifact_path.expanduser().resolve(strict=False)
    if not path.exists():
        raise typer.BadParameter(f"release artifact missing: {path}")
    if path.suffix != ".whl":
        raise typer.BadParameter(f"release artifact must be a wheel (.whl): {path.name}")

    try:
        with zipfile.ZipFile(path) as archive:
            metadata_paths = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise typer.BadParameter(
                    f"release artifact must contain exactly one METADATA file: {path.name}"
                )
            metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode())
    except zipfile.BadZipFile as exc:
        raise typer.BadParameter(f"release artifact is not a readable wheel: {path}") from exc

    name = str(metadata.get("Name", "")).strip()
    version = str(metadata.get("Version", "")).strip()
    if _normalise_distribution_name(name) != PACKAGE_NAME:
        raise typer.BadParameter(f"release artifact package name must be {PACKAGE_NAME}; found {name}")
    if not version:
        raise typer.BadParameter("release artifact is missing package version metadata")
    if expected_version is not None and version != expected_version:
        raise typer.BadParameter(
            f"version mismatch: artifact is {version}, expected {expected_version}"
        )
    return ReleaseArtifact(path=path, name=name, version=version, sha256=_sha256_file(path))


def _read_constraints_file(path: Path | None) -> tuple[Path, str] | None:
    if path is None:
        return None
    constraints_path = path.expanduser().resolve(strict=False)
    if not constraints_path.exists():
        raise typer.BadParameter(f"constraints file missing: {constraints_path}")
    if constraints_path.suffix != ".txt":
        raise typer.BadParameter("constraints file should be a uv-exported requirements.txt")
    return constraints_path, _sha256_file(constraints_path)


def _runtime_venv(cfg: EnvConfig) -> Path:
    return cfg.checkout / ".venv"


def _runtime_python(cfg: EnvConfig) -> Path:
    return _runtime_venv(cfg) / "bin" / "python"


def _runtime_cli(cfg: EnvConfig) -> Path:
    return _runtime_venv(cfg) / "bin" / "nyc-ai-scout"


def _resolve_uv_bin(uv_bin: str) -> str:
    if os.sep in uv_bin:
        return uv_bin
    resolved = shutil.which(uv_bin)
    if resolved:
        return resolved
    for directory in UV_FALLBACK_DIRS:
        candidate = directory / uv_bin
        if candidate.exists():
            return str(candidate)
    return uv_bin


def _web_service_label(cfg: EnvConfig) -> str:
    for label in cfg.services:
        if label.endswith("-web"):
            return label
    _exit_with_error(f"No web service label configured for {cfg.name}")
    raise AssertionError("unreachable")


def _release_artifact_target(cfg: EnvConfig, artifact: ReleaseArtifact) -> Path:
    safe_version = _safe_label(artifact.version)
    return cfg.checkout / "releases" / f"v{safe_version}" / artifact.path.name


def _constraints_target(
    cfg: EnvConfig,
    *,
    release: ReleaseArtifact,
    constraints: tuple[Path, str],
) -> Path:
    _source, digest = constraints
    safe_version = _safe_label(release.version)
    return cfg.checkout / "releases" / f"v{safe_version}" / f"requirements-{digest[:12]}.txt"


def _copy_if_needed(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source != target:
        shutil.copy2(source, target)
    return target


def _write_release_metadata(
    cfg: EnvConfig,
    *,
    release: ReleaseArtifact,
    installed_artifact: Path,
    constraints: tuple[Path, str] | None,
    installed_constraints: Path | None,
    backup_path: Path,
) -> Path:
    metadata_path = cfg.checkout / "data/current-release.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": release.name,
        "version": release.version,
        "artifact_path": str(installed_artifact),
        "artifact_sha256": release.sha256,
        "constraints_path": str(installed_constraints) if installed_constraints else None,
        "constraints_sha256": constraints[1] if constraints else None,
        "db_backup_path": str(backup_path),
        "deployed_at": datetime.now(UTC).isoformat(),
    }
    tmp_path = metadata_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(metadata_path)
    return metadata_path


def _read_release_launchd_templates(release: ReleaseArtifact) -> dict[str, str]:
    templates: dict[str, str] = {}
    with zipfile.ZipFile(release.path) as archive:
        for filename in PRODUCTION_LAUNCHD_PLISTS:
            archive_path = f"api/_runtime/deploy/{filename}"
            try:
                templates[filename] = archive.read(archive_path).decode()
            except KeyError as exc:
                raise typer.BadParameter(
                    f"release artifact is missing launchd template: {archive_path}"
                ) from exc
            except UnicodeDecodeError as exc:
                raise typer.BadParameter(
                    f"release launchd template is not UTF-8: {archive_path}"
                ) from exc
    return templates


def _render_release_launchd_templates(
    cfg: EnvConfig,
    *,
    templates: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    runtime_path = str(cfg.checkout)
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", runtime_path):
        raise typer.BadParameter("production runtime path contains unsupported characters")

    rendered_templates: dict[str, tuple[str, str]] = {}
    for filename in PRODUCTION_LAUNCHD_PLISTS:
        rendered = templates[filename].replace("RUNTIME_PATH", runtime_path)
        if "RUNTIME_PATH" in rendered or "NGROK_DOMAIN" in rendered:
            raise typer.BadParameter(f"launchd template has unresolved placeholders: {filename}")
        try:
            payload = plistlib.loads(rendered.encode())
        except Exception as exc:
            raise typer.BadParameter(f"launchd template is invalid: {filename}") from exc
        if not isinstance(payload, dict):
            raise typer.BadParameter(f"launchd template is invalid: {filename}")
        label = payload.get("Label")
        expected_label = filename.removesuffix(".plist")
        if label != expected_label:
            raise typer.BadParameter(
                f"launchd template label mismatch: {filename} contains {label!r}"
            )
        arguments = payload.get("ProgramArguments")
        if not isinstance(arguments, list) or not all(
            isinstance(value, str) for value in arguments
        ):
            raise typer.BadParameter(
                f"launchd template has invalid ProgramArguments: {filename}"
            )
        command = " ".join(arguments)
        if re.search(r"(?:^|[/\s])uv\s+run(?:\s|$)", command):
            raise typer.BadParameter(f"production launchd template may not run uv: {filename}")
        rendered_templates[filename] = (expected_label, rendered)
    return rendered_templates


def _disable_launchd_label_aliases(
    launch_agents_dir: Path,
    *,
    rendered: Mapping[str, tuple[str, str]],
) -> tuple[Path, ...]:
    canonical_filenames = set(rendered)
    canonical_labels = {label for label, _body in rendered.values()}
    disabled_paths: list[Path] = []
    for candidate in sorted(launch_agents_dir.glob("*.plist")):
        if candidate.name in canonical_filenames:
            continue
        try:
            payload = plistlib.loads(candidate.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        if not isinstance(payload, dict) or payload.get("Label") not in canonical_labels:
            continue

        disabled = candidate.with_name(f"{candidate.name}.disabled")
        suffix = 1
        while disabled.exists():
            disabled = candidate.with_name(f"{candidate.name}.{suffix}.disabled")
            suffix += 1
        candidate.replace(disabled)
        disabled_paths.append(disabled)
        typer.echo(f"Disabled launchd label alias: {candidate.name} -> {disabled.name}")
    return tuple(disabled_paths)


def _quiesce_launchd_label_aliases(
    launch_agents_dir: Path,
    *,
    rendered: Mapping[str, tuple[str, str]],
    uid: int | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> tuple[Path, ...]:
    """Disable duplicate plist names and stop their already-loaded jobs."""
    disabled_paths = _disable_launchd_label_aliases(
        launch_agents_dir,
        rendered=rendered,
    )
    canonical_labels = {label for label, _body in rendered.values()}
    labels: set[str] = set()
    for disabled_path in sorted(launch_agents_dir.glob("*.plist*.disabled")):
        try:
            payload = plistlib.loads(disabled_path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        label = payload.get("Label") if isinstance(payload, dict) else None
        if isinstance(label, str) and label in canonical_labels:
            labels.add(label)

    uid = os.getuid() if uid is None else uid
    for label in sorted(labels):
        service_target = f"gui/{uid}/{label}"
        run(["launchctl", "bootout", service_target], check=False)
        probe = run(["launchctl", "print", service_target], check=False)
        if probe.returncode == 0:
            raise SmokeFailure(f"launchd label alias remains loaded: {label}")
    return disabled_paths


def install_release_launchd_templates(
    cfg: EnvConfig,
    *,
    release: ReleaseArtifact,
    uid: int | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Path, ...]:
    """Install and reload production launchd definitions from a release wheel."""
    templates = _read_release_launchd_templates(release)
    rendered = _render_release_launchd_templates(
        cfg,
        templates=templates,
    )
    launch_agents_dir = _launch_agents_dir()
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    uid = os.getuid() if uid is None else uid

    with tempfile.TemporaryDirectory(prefix="nyc-ai-events-launchd-") as tmp:
        rendered_dir = Path(tmp)
        staged_paths: dict[str, Path] = {}
        for filename, (_label, body) in rendered.items():
            staged_path = rendered_dir / filename
            staged_path.write_text(body)
            run(["plutil", "-lint", str(staged_path)])
            staged_paths[filename] = staged_path

        _disable_launchd_label_aliases(launch_agents_dir, rendered=rendered)

        for label, _body in rendered.values():
            run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False)

        installed_paths: list[Path] = []
        for filename, staged_path in staged_paths.items():
            target = launch_agents_dir / filename
            temporary_target = launch_agents_dir / f".{filename}.tmp"
            shutil.copy2(staged_path, temporary_target)
            temporary_target.replace(target)
            installed_paths.append(target)

        for filename, (_label, _body) in rendered.items():
            target = launch_agents_dir / filename
            first_error: subprocess.CalledProcessError | None = None
            for attempt in range(1, LAUNCHD_BOOTSTRAP_ATTEMPTS + 1):
                try:
                    run(["launchctl", "bootstrap", f"gui/{uid}", str(target)])
                    break
                except subprocess.CalledProcessError as exc:
                    first_error = first_error or exc
                    if attempt == LAUNCHD_BOOTSTRAP_ATTEMPTS:
                        if exc is first_error:
                            raise
                        raise exc from first_error
                    sleep(LAUNCHD_BOOTSTRAP_RETRY_SECONDS)

    return tuple(installed_paths)


def _release_metadata_version(cfg: EnvConfig) -> str:
    metadata_path = cfg.checkout / "data/current-release.json"
    try:
        payload = json.loads(metadata_path.read_text())
    except FileNotFoundError as exc:
        raise SmokeFailure(f"release metadata missing: {metadata_path}") from exc
    except OSError as exc:
        raise SmokeFailure(f"release metadata unreadable: {metadata_path}") from exc
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"release metadata is invalid JSON: {metadata_path}") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure(f"release metadata has invalid package identity: {metadata_path}")
    version = payload.get("version")
    if payload.get("name") != PACKAGE_NAME or not isinstance(version, str) or not version:
        raise SmokeFailure(f"release metadata has invalid package identity: {metadata_path}")
    return version


def check_release_identity(
    cfg: EnvConfig,
    *,
    expected_version: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    """Reject runtime environments that do not execute the recorded wheel."""
    expected = expected_version or _release_metadata_version(cfg)
    try:
        result = run(
            [str(_runtime_python(cfg)), "-c", _RUNTIME_DISTRIBUTION_IDENTITY_CODE],
            cwd=cfg.checkout,
        )
    except subprocess.CalledProcessError as exc:
        raise SmokeFailure(f"release identity check failed ({exc.returncode})") from exc
    except OSError as exc:
        raise SmokeFailure(f"release identity check could not start: {exc}") from exc
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure("release identity check returned invalid JSON") from exc
    if not isinstance(identity, dict):
        raise SmokeFailure("release identity check returned invalid fields")
    actual = identity.get("version")
    editable = identity.get("editable")
    if not isinstance(actual, str) or not isinstance(editable, bool):
        raise SmokeFailure("release identity check returned invalid fields")
    if actual != expected:
        raise SmokeFailure(f"release identity mismatch: expected {expected}, found {actual}")
    if editable:
        raise SmokeFailure(f"release identity mismatch: {actual} is an editable installation")


def _http_read(
    url: str,
    *,
    opener: Callable[..., Any],
) -> tuple[int, Mapping[str, str], str]:
    try:
        with opener(url, timeout=2) as response:
            status = int(response.status)
            headers = response.headers
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = exc.headers
        raw_body = exc.read()
    except Exception as exc:
        raise SmokeFailure(f"{url} failed: {exc}") from exc
    return status, headers, raw_body.decode(errors="replace")


def _is_login_redirect(status: int, headers: Mapping[str, str]) -> bool:
    if status not in _REDIRECT_STATUSES:
        return False
    location = headers.get("Location", "")
    parsed = urllib.parse.urlparse(location)
    return parsed.path == "/login"


def check_http_health(
    cfg: EnvConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    url = f"http://127.0.0.1:{cfg.web_port}/healthz"
    try:
        with opener(url, timeout=2) as response:
            status = int(response.status)
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw_body = exc.read()
    except Exception as exc:
        raise SmokeFailure(f"healthz failed: {exc}") from exc

    try:
        payload = json.loads(raw_body.decode() or "{}")
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"healthz returned non-JSON response ({status})") from exc

    if status != 200:
        raise SmokeFailure(f"healthz returned {status}: {payload}")
    if (
        payload.get("status") != "ok"
        or payload.get("db") != "ok"
        or payload.get("migrations") != "current"
    ):
        raise SmokeFailure(f"healthz degraded: {payload}")


def check_browser_surface(
    cfg: EnvConfig,
    *,
    opener: Callable[..., Any] = _NO_REDIRECT_OPENER.open,
    require_google_login: bool = True,
) -> None:
    base_url = f"http://127.0.0.1:{cfg.web_port}"
    status, headers, _body = _http_read(f"{base_url}/events", opener=opener)
    if not _is_login_redirect(status, headers):
        raise SmokeFailure("/events did not redirect anonymous visitor to /login")

    status, _headers, login_body = _http_read(f"{base_url}/login", opener=opener)
    if status != 200:
        raise SmokeFailure(f"/login returned {status}")
    if require_google_login and "Log in with Google" not in login_body:
        raise SmokeFailure("/login missing Google login CTA")
    if not require_google_login and "Event Hunt" not in login_body:
        raise SmokeFailure("/login missing Event Hunt shell")

    for path in ("/mine", "/hidden", "/settings"):
        status, headers, _body = _http_read(f"{base_url}{path}", opener=opener)
        if not _is_login_redirect(status, headers):
            raise SmokeFailure(f"{path} did not redirect anonymous visitor to /login")


def _launchd_field(output: str, name: str) -> str | None:
    for match in _LAUNCHD_FIELD_RE.finditer(output):
        if match.group("name").strip() == name:
            return match.group("value").strip()
    return None


def check_launchd_service(
    label: str,
    *,
    uid: int | None = None,
    allow_not_running: bool = False,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    uid = os.getuid() if uid is None else uid
    try:
        result = run(["launchctl", "print", f"gui/{uid}/{label}"])
    except subprocess.CalledProcessError as exc:
        raise SmokeFailure(f"{label}: launchctl print failed ({exc.returncode})") from exc
    output = result.stdout
    state = _launchd_field(output, "state")
    last_exit_code = _launchd_field(output, "last exit code")
    ok_exit = last_exit_code in (None, "0", "143", "(never exited)")
    if state != "running":
        if allow_not_running and state == "not running" and ok_exit and "event triggers" in output:
            return
        raise SmokeFailure(f"{label}: state = {state or '<missing>'}")
    pid = _launchd_field(output, "pid")
    if pid == "0":
        raise SmokeFailure(f"{label}: pid is 0")
    if not ok_exit:
        raise SmokeFailure(f"{label}: last exit code = {last_exit_code}")


def check_sqlite_migration_drift(cfg: EnvConfig) -> None:
    try:
        required, applied = _migration_sets(cfg.db_path)
    except sqlite3.Error as exc:
        raise SmokeFailure(f"migration drift check failed: {exc}") from exc
    missing = sorted(required - applied)
    unknown = sorted(applied - required)
    if missing or unknown:
        raise SmokeFailure(f"migration drift: missing={missing}, unknown_applied={unknown}")


def check_scoring_breaker(cfg: EnvConfig) -> None:
    marker = cfg.checkout / "data" / ".scoring_disabled"
    if marker.exists():
        raise SmokeFailure(f"scoring breaker engaged: {marker}")


def _tail_logs(paths: tuple[Path, ...], *, line_count: int = 20) -> list[str]:
    output: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        lines = path.read_text(errors="replace").splitlines()[-line_count:]
        if not lines:
            continue
        output.append(f"==> {path} <==")
        output.extend(lines)
    return output


deploy_app = typer.Typer(add_completion=False, no_args_is_help=True)


@deploy_app.command("deploy")
def deploy(
    env: Annotated[str, typer.Option("--env", help="staging | production")],
    sha: Annotated[str, typer.Option("--sha", help="git SHA to deploy")] = "HEAD",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="plan only, no execution")] = False,
) -> None:
    """Deploy a git SHA to staging or production.

    Migrations are forward-only. Before a real deploy runs migrations, the
    target SQLite DB is copied to data/backups/pre-deploy-<sha>-<timestamp>.db.
    """
    cfg = resolve_env(env)
    _validate_deploy_inputs(cfg)

    _run(["git", "fetch", "--prune", DEFAULT_REMOTE], cwd=cfg.checkout)
    resolved_sha = _resolve_sha(sha, cfg.checkout)
    if cfg.requires_origin_ancestor_check:
        _verify_sha_on_origin_default_branch(resolved_sha, cfg.checkout)

    for line in (
        f"env: {cfg.name}",
        f"sha: {resolved_sha[:12]}",
        f"checkout: {cfg.checkout}",
        f"db: {cfg.db_path}",
        f"services: {', '.join(cfg.services)}",
        f"web port: {cfg.web_port}",
        f"sync extras: {cfg.sync_extras or '(runtime-only)'}",
        _migration_plan_line(cfg.db_path),
    ):
        typer.echo(line)
    if dry_run:
        typer.echo("DRY RUN - no changes made.")
        return

    backup_path = _backup_db(cfg, resolved_sha)
    typer.echo(f"DB backup: {backup_path}")

    runtime_env = _runtime_env(cfg.env_file)
    _run(["git", "checkout", resolved_sha], cwd=cfg.checkout)
    _run(_sync_args(cfg), cwd=cfg.checkout)
    _run(["uv", "run", "nyc-ai-scout", "migrate"], cwd=cfg.checkout, env=runtime_env)
    uid = os.getuid()
    for service in cfg.services:
        _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{service}"])
    _wait_for_health(cfg.web_port)
    typer.echo(f"DEPLOYED: {cfg.name} @ {resolved_sha[:12]}")
    typer.echo(f"Rollback if needed: nyc-ai-scout deploy --env={cfg.name} --sha=<previous-sha>")
    typer.echo(f"DB backup at: {backup_path}")


@deploy_app.command("deploy-release")
def deploy_release(
    env: Annotated[str, typer.Option("--env", help="production")],
    artifact: Annotated[Path, typer.Option("--artifact", help="Wheel artifact to install")],
    version: Annotated[
        str | None,
        typer.Option("--version", help="Expected package version; rejects mismatches"),
    ] = None,
    constraints: Annotated[
        Path | None,
        typer.Option(
            "--constraints",
            help="uv-exported requirements.txt to pin dependencies",
        ),
    ] = None,
    uv_bin: Annotated[str, typer.Option("--uv-bin", help="uv executable")] = "uv",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="plan only, no execution")] = False,
) -> None:
    """Install a tagged release package into the production runtime.

    This is the legacy local path: code is selected as a wheel
    artifact, the live env/DB/log directories stay in place, and production
    launchd definitions are installed from that artifact after migration. Keep
    using `deploy --sha` for the source-checkout staging workflow.
    """
    cfg = resolve_env(env)
    if cfg.name != "production":
        _exit_with_error("release deploy is production-only; use deploy --sha for staging.")
    _validate_deploy_inputs(cfg)
    release = _read_release_artifact(artifact, expected_version=version)
    release_templates = _read_release_launchd_templates(release)
    constraints_info = _read_constraints_file(constraints)
    runtime_python = _runtime_python(cfg)
    runtime_cli = _runtime_cli(cfg)
    uv_executable = _resolve_uv_bin(uv_bin)
    runtime_env = _runtime_env(cfg.env_file)
    rendered_templates = _render_release_launchd_templates(
        cfg,
        templates=release_templates,
    )

    installed_artifact = _release_artifact_target(cfg, release)
    installed_constraints = (
        _constraints_target(cfg, release=release, constraints=constraints_info)
        if constraints_info
        else None
    )

    for line in (
        f"env: {cfg.name}",
        f"version: {release.version}",
        f"artifact: {release.path}",
        f"artifact sha256: {release.sha256}",
        f"constraints: {constraints_info[0] if constraints_info else '(none)'}",
        f"runtime: {cfg.checkout}",
        f"venv: {_runtime_venv(cfg)}",
        f"db: {cfg.db_path}",
        f"reload services: {', '.join(name.removesuffix('.plist') for name in PRODUCTION_LAUNCHD_PLISTS)}",
        f"web port: {cfg.web_port}",
        _migration_plan_line(cfg.db_path),
    ):
        typer.echo(line)
    if dry_run:
        typer.echo("DRY RUN - no changes made.")
        return

    (cfg.checkout / "data").mkdir(parents=True, exist_ok=True)
    (cfg.checkout / "data/logs").mkdir(parents=True, exist_ok=True)
    copied_artifact = _copy_if_needed(release.path, installed_artifact)
    copied_constraints = (
        _copy_if_needed(constraints_info[0], installed_constraints)
        if constraints_info and installed_constraints is not None
        else None
    )

    backup_path = _backup_db_with_label(cfg, prefix="pre-release", label=release.version)
    typer.echo(f"DB backup: {backup_path}")

    launch_agents_dir = _launch_agents_dir()
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    _quiesce_launchd_label_aliases(
        launch_agents_dir,
        rendered=rendered_templates,
        run=_run,
    )

    if not runtime_python.exists():
        _run([uv_executable, "venv", str(_runtime_venv(cfg))], cwd=cfg.checkout, env=runtime_env)
    install_cmd = [
        uv_executable,
        "pip",
        "install",
        "--python",
        str(runtime_python),
        "--reinstall",
    ]
    if copied_constraints is not None:
        install_cmd.extend(["--constraints", str(copied_constraints)])
    install_cmd.append(str(copied_artifact))
    _run(install_cmd, cwd=cfg.checkout, env=runtime_env)
    check_release_identity(cfg, expected_version=release.version, run=_run)
    _run([str(runtime_cli), "migrate"], cwd=cfg.checkout, env=runtime_env)

    install_release_launchd_templates(
        cfg,
        release=release,
        run=_run,
    )
    _wait_for_health(cfg.web_port)
    check_http_health(cfg)
    check_browser_surface(cfg)
    metadata_path = _write_release_metadata(
        cfg,
        release=release,
        installed_artifact=copied_artifact,
        constraints=constraints_info,
        installed_constraints=copied_constraints,
        backup_path=backup_path,
    )
    typer.echo(f"DEPLOYED RELEASE: {cfg.name} {release.version}")
    typer.echo(f"Release metadata: {metadata_path}")
    typer.echo(
        "Rollback if needed: nyc-ai-scout deploy-release --env=production "
        "--artifact=<previous-wheel> --constraints=<previous-requirements> "
        "--uv-bin=/usr/local/bin/uv"
    )
    typer.echo(f"DB backup at: {backup_path}")


@deploy_app.command("smoke")
def smoke(env: Annotated[str, typer.Option("--env", help="staging | production")]) -> None:
    """Smoke-test a deployed environment."""
    cfg = resolve_env(env)
    try:
        if cfg.name == "production":
            check_release_identity(cfg)
        check_http_health(cfg)
        check_browser_surface(cfg, require_google_login=cfg.name == "production")
        scheduled = set(cfg.scheduled_services)
        for service in cfg.smoke_services:
            check_launchd_service(service, allow_not_running=service in scheduled)
        check_sqlite_migration_drift(cfg)
        check_scoring_breaker(cfg)
    except SmokeFailure as exc:
        typer.echo(f"SMOKE FAILED ({cfg.name}): {exc}", err=True)
        for line in _tail_logs(cfg.log_files):
            typer.echo(line, err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"SMOKE OK ({cfg.name})")


@deploy_app.command("check-env")
def check_env(env: Annotated[str, typer.Option("--env", help="staging | production")]) -> None:
    """Validate that an environment file has the required keys without printing values."""
    cfg = resolve_env(env)
    if not cfg.env_file.exists():
        _exit_with_error(f"Env file missing: {cfg.env_file}")
    _validate_env_file_matches_config(cfg)
    typer.echo(f"OK ({cfg.name})")
