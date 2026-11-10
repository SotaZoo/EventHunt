"""Run the complete Event Scout verification contract from one entry point.

The local CI controller runs this inside a disposable, network-isolated
container. A developer may run it in a normal checkout with online dependency
installation. A receipt is evidence of commands executed, not authorization to
deploy a SHA without human review and independent controller checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("EVENTSCOUT_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
CHECKS = ("docs", "lint", "backend", "frontend", "browser")
REQUIRED_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".ai/AGENTS.md",
    ".ai/README.md",
    ".ai/git.md",
    ".ai/product-process.md",
    ".ai/merge-requests.md",
    ".ai/code-review.md",
    ".ai/database.md",
    ".ai/testing.md",
    ".ai/code-style.md",
    ".ai/ci-cd.md",
    ".ai/security.md",
    ".ai/runtime.md",
    ".ai/ai.md",
    ".claude/skills/nyc-ai-events-coding-principles/SKILL.md",
    ".agents/skills/nyc-ai-events-coding-principles/SKILL.md",
    "api/AGENTS.md",
    "web/AGENTS.md",
    "e2e/AGENTS.md",
    "db/AGENTS.md",
    "deploy/AGENTS.md",
    "doc/AGENTS.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
)


class VerificationFailure(Exception):
    """A required check failed or could not be run."""


def _base_env() -> dict[str, str]:
    """Pass only routine tool paths and caches, never account/runtime credentials."""
    allowed = (
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE",
        "PLAYWRIGHT_BROWSERS_PATH", "UV_CACHE_DIR", "NPM_CONFIG_CACHE",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env["PYTHONPATH"] = str(ROOT)
    return env


def _command(args: list[str], *, timeout: int = 600, env: dict[str, str] | None = None) -> None:
    """Run one named command and fail on nonzero exit or timeout."""
    print("RUN", " ".join(args), flush=True)
    try:
        result = subprocess.run(
            args, cwd=ROOT, env=_base_env() if env is None else env, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationFailure(f"Command unavailable or timed out: {args[0]}") from exc
    if result.returncode:
        raise VerificationFailure(f"Command failed ({result.returncode}): {args[0]}")


def _docs() -> None:
    """Check the repository's required documentation and routing files."""
    missing = [path for path in REQUIRED_FILES if not (ROOT / path).is_file()]
    if missing:
        raise VerificationFailure(f"Missing required files: {', '.join(missing)}")


def _prepare(*, offline: bool) -> None:
    """Install locked dependencies without lifecycle scripts for Node packages."""
    uv_args = ["uv", "sync", "--extra", "dev", "--locked", "--python", "3.11"]
    if offline:
        uv_args.extend(("--offline", "--no-install-project", "--no-build"))
    else:
        # The wheel includes the web assets, which do not exist in a clean
        # checkout until the locked Node dependencies have built them.
        uv_args.append("--no-install-project")
    _command(uv_args)
    for package in ("web", "e2e"):
        npm_args = [
            "npm", "--prefix", package, "ci", "--ignore-scripts", "--no-audit", "--no-fund",
        ]
        if offline:
            npm_args.append("--offline")
        _command(npm_args)
    _command(["npm", "--prefix", "web", "run", "build:discovery"])
    if not offline:
        _command(["npx", "--prefix", "e2e", "playwright", "install", "chromium"])


def _lint(*, offline: bool) -> None:
    """Run pinned Ruff from the locked project environment."""
    args = ["uv", "run"]
    if offline:
        args.extend(("--offline", "--no-sync"))
    _command([*args, "ruff", "check", "."])


def _backend(*, offline: bool) -> None:
    """Run backend tests and build the distributable package."""
    args = ["uv", "run"]
    if offline:
        args.extend(("--offline", "--no-sync"))
    _command([*args, "pytest", "api/tests"])
    _command(["npm", "--prefix", "web", "run", "build:discovery"])
    build_args = ["uv", "build", "--no-build-isolation"]
    if offline:
        build_args.append("--offline")
    _command(build_args)


def _frontend() -> None:
    """Run frontend unit tests and its production build."""
    _command(["npm", "--prefix", "web", "test", "--", "--runInBand"])
    _command(["npm", "--prefix", "web", "run", "build"])
    _command(["npm", "--prefix", "web", "run", "typecheck"])


def _unused_local_port() -> int:
    """Reserve and release a loopback port for the synthetic browser test API."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_api(port: int, proc: subprocess.Popen[bytes]) -> None:
    """Wait for the local fixture API and fail if it exits or never starts."""
    url = f"http://127.0.0.1:{port}/login"
    for _ in range(30):
        if proc.poll() is not None:
            raise VerificationFailure("Fixture API exited before becoming ready")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    raise VerificationFailure("Fixture API did not become ready in 30 seconds")


def _browser(*, offline: bool) -> None:
    """Run the Cucumber/Playwright user journey against a synthetic database."""
    _command(["npm", "--prefix", "web", "run", "build:discovery"])
    with tempfile.TemporaryDirectory(prefix="eventscout-verify-") as tmp_dir:
        tmp = Path(tmp_dir)
        port = _unused_local_port()
        env = _base_env()
        env.update(
            {
                "DATABASE_PATH": str(tmp / "events.db"),
                "NYC_AI_EVENTS_SESSION_SECRET": "synthetic-e2e-secret",
                "E2E_BASE_URL": f"http://127.0.0.1:{port}",
                "E2E_MCP_RESOURCE_URL": "https://events.example.test/mcp",
                "E2E_SESSION_FILE": str(tmp / "e2e-session.json"),
            }
        )
        uv_args = ["uv", "run"]
        if offline:
            uv_args.extend(("--offline", "--no-sync"))
        _command([*uv_args, "python", "e2e/seed_db.py"], env=env)
        log_path = tmp / "api.log"
        with log_path.open("wb") as log_file:
            proc = subprocess.Popen(
                [
                    *uv_args,
                    "uvicorn", "--factory", "api.tests.fixtures.oauth_browser_app:create_app",
                    "--host", "127.0.0.1", "--port", str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_for_api(port, proc)
                _command(["npm", "--prefix", "e2e", "test"], env=env)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    """Atomically persist a compact, non-secret verification receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix="verify-", delete=False
    ) as file:
        json.dump(payload, file, sort_keys=True)
        file.write("\n")
        temp_path = Path(file.name)
    temp_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    """Run selected checks and return zero only for a complete passing set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("all", *CHECKS), default="all")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--source-sha")
    args = parser.parse_args(argv)
    if args.source_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        parser.error("--source-sha must be a full lowercase Git commit SHA")

    selected = CHECKS if args.check == "all" else (args.check,)
    receipt: dict[str, object] = {
        "source_sha": args.source_sha,
        "status": "failed",
        "checks": [],
        "durations_sec": {},
    }
    try:
        if args.check == "all":
            started = time.monotonic()
            try:
                _prepare(offline=args.offline)
                receipt["checks"].append({"name": "prepare", "status": "passed"})
            except VerificationFailure:
                receipt["checks"].append({"name": "prepare", "status": "failed"})
                raise
            finally:
                receipt["durations_sec"]["prepare"] = round(time.monotonic() - started, 2)
        runners = {
            "docs": lambda: _docs(),
            "lint": lambda: _lint(offline=args.offline),
            "backend": lambda: _backend(offline=args.offline),
            "frontend": lambda: _frontend(),
            "browser": lambda: _browser(offline=args.offline),
        }
        for name in selected:
            started = time.monotonic()
            try:
                runners[name]()
                receipt["checks"].append({"name": name, "status": "passed"})
            except VerificationFailure:
                receipt["checks"].append({"name": name, "status": "failed"})
                raise
            finally:
                receipt["durations_sec"][name] = round(time.monotonic() - started, 2)
    except VerificationFailure as exc:
        print(str(exc), file=sys.stderr)
    else:
        receipt["status"] = "passed"
    finally:
        if args.receipt:
            _write_receipt(args.receipt, receipt)
        print(
            f"VERIFY {receipt['status']}: "
            + ", ".join(f"{item['name']}={item['status']}" for item in receipt["checks"]),
            flush=True,
        )
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
