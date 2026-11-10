from __future__ import annotations

import json
import plistlib
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from api.commands import deploy as deploy_module
from api.commands.deploy import DEFAULT_BRANCH, EnvConfig, runtime_deploy_template_dir
from api.commands.main import app

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_PLIST_NAMES = (
    "com.yang.nyc-ai-events.plist",
    "com.yang.nyc-ai-events-cleanup.plist",
    "com.yang.nyc-ai-events-web.plist",
    "com.yang.nyc-ai-events-backup.plist",
    "com.yang.nyc-ai-events.scout-bot.plist",
    "com.yang.nyc-ai-events.capacity-poller.plist",
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "-b", DEFAULT_BRANCH, str(work)],
        check=True,
        capture_output=True,
    )
    (work / "README.md").write_text("base\n")
    _git(work, "add", "README.md")
    _git(work, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", DEFAULT_BRANCH)
    return work, _git(work, "rev-parse", "HEAD")


def _make_wheel(
    tmp_path: Path,
    *,
    version: str = "0.2.0",
    name: str = "nyc-ai-events",
    unsafe_capacity_poller: bool = False,
    omitted_plist: str | None = None,
    wrong_capacity_label: bool = False,
) -> Path:
    filename_name = name.replace("-", "_")
    wheel = tmp_path / f"{filename_name}-{version}-py3-none-any.whl"
    dist_info = f"{filename_name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        for filename in PRODUCTION_PLIST_NAMES:
            if filename == omitted_plist:
                continue
            template = (ROOT / "deploy" / filename).read_text()
            if filename == "com.yang.nyc-ai-events.capacity-poller.plist":
                if unsafe_capacity_poller:
                    template = template.replace(
                        "RUNTIME_PATH/.venv/bin/nyc-ai-scout poll-capacity",
                        "/usr/local/bin/uv run nyc-ai-scout poll-capacity",
                    )
                if wrong_capacity_label:
                    template = template.replace(
                        "com.yang.nyc-ai-events.capacity-poller</string>",
                        "com.yang.nyc-ai-events.wrong</string>",
                        1,
                    )
                template = template.replace(
                    "</plist>",
                    "  <!-- release-artifact-template -->\n</plist>",
                )
            archive.writestr(f"api/_runtime/deploy/{filename}", template)
    return wheel


def _env_file(tmp_path: Path, *, database_path: Path, name: str = "staging") -> Path:
    path = tmp_path / f".env.{name}"
    path.write_text(
        f"DATABASE_PATH={database_path}\n"
        "NYC_AI_EVENTS_SESSION_SECRET=private-session-secret-private-session\n"
        "NYC_AI_EVENTS_PUBLIC_BASE_URL=https://example.test\n"
        "NYC_AI_EVENTS_GOOGLE_CLIENT_ID=google-client\n"
        "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET=google-secret\n"
    )
    return path


def _config(
    tmp_path: Path,
    checkout: Path,
    *,
    name: str,
    production: bool = False,
    db_path: Path | None = None,
) -> EnvConfig:
    database_path = db_path or tmp_path / "events.db"
    services = (f"com.yang.nyc-ai-events-{name}-web",)
    return EnvConfig(
        name=name,
        checkout=checkout,
        env_file=_env_file(tmp_path, database_path=database_path, name=name),
        db_path=database_path,
        services=services,
        smoke_services=services,
        web_port=8848 if not production else 8847,
        requires_origin_ancestor_check=production,
        sync_extras=() if production else ("dev",),
        log_files=(),
    )


def test_staging_deploy_dry_run_plans_without_mutating(tmp_path: Path, monkeypatch) -> None:
    checkout, _sha = _make_repo(tmp_path)
    cfg = _config(tmp_path, checkout, name="staging")
    calls: list[list[str]] = []
    real_run = deploy_module._run

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(deploy_module, "_run", recording_run)

    result = CliRunner().invoke(app, ["deploy", "--env=staging", "--sha=HEAD", "--dry-run"])

    assert result.exit_code == 0
    assert "env: staging" in result.output
    assert "DRY RUN - no changes made." in result.output
    assert ["git", "fetch", "--prune", "origin"] in calls
    assert not any(call[:2] == ["git", "checkout"] for call in calls)
    assert not any(call[:1] == ["uv"] for call in calls)
    assert not any(call[:1] == ["launchctl"] for call in calls)
    assert not (checkout / "data" / "backups").exists()


def test_deploy_rejects_env_database_path_that_does_not_match_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout, _sha = _make_repo(tmp_path)
    cfg = _config(tmp_path, checkout, name="staging")
    cfg.env_file.write_text(
        cfg.env_file.read_text().replace(str(cfg.db_path), str(tmp_path / "wrong.db"))
    )
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(app, ["deploy", "--env=staging", "--sha=HEAD", "--dry-run"])

    assert result.exit_code != 0
    assert "MISMATCH: DATABASE_PATH" in result.output
    assert str(cfg.db_path) in result.output


def test_production_dry_run_rejects_sha_not_on_origin_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout, _sha = _make_repo(tmp_path)
    _git(checkout, "checkout", "-b", "feature/local-only")
    (checkout / "feature.txt").write_text("feature\n")
    _git(checkout, "add", "feature.txt")
    _git(
        checkout,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "local feature",
    )
    feature_sha = _git(checkout, "rev-parse", "HEAD")
    cfg = _config(tmp_path, checkout, name="production", production=True)
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        ["deploy", "--env=production", f"--sha={feature_sha}", "--dry-run"],
    )

    assert result.exit_code != 0
    assert "Production safety" in result.output


def test_production_dry_run_accepts_sha_on_origin_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout, sha = _make_repo(tmp_path)
    cfg = _config(tmp_path, checkout, name="production", production=True)
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        ["deploy", "--env=production", f"--sha={sha}", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "env: production" in result.output
    assert "DRY RUN - no changes made." in result.output


def test_real_staging_deploy_writes_backup_before_migrate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    db_path = checkout / "data" / "staging.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"sqlite-db")
    cfg = _config(tmp_path, checkout, name="staging", db_path=db_path)
    calls: list[list[str]] = []
    sha = "a" * 40

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{sha}\n", stderr="")
        if cmd[:3] == ["uv", "run", "nyc-ai-scout"] and "migrate" in cmd:
            backups = list((checkout / "data" / "backups").glob("pre-deploy-*.db"))
            assert backups, "backup must exist before migrate runs"
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(deploy_module, "_run", fake_run)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda port: None)

    result = CliRunner().invoke(app, ["deploy", "--env=staging", "--sha=HEAD"])

    assert result.exit_code == 0
    backups = list((checkout / "data" / "backups").glob("pre-deploy-aaaaaaaaaaaa-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"sqlite-db"
    assert ["git", "fetch", "--prune", "origin"] in calls
    assert ["git", "checkout", sha] in calls
    assert ["uv", "sync", "--locked", "--extra", "dev"] in calls
    assert ["uv", "run", "nyc-ai-scout", "migrate"] in calls
    assert any(call[:2] == ["launchctl", "kickstart"] for call in calls)


def test_release_deploy_rejects_non_production_env(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="staging")
    wheel = _make_wheel(tmp_path)
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=staging", "--artifact", str(wheel), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "release deploy is production-only" in result.output


def test_release_deploy_dry_run_plans_without_git_or_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path)
    calls: list[list[str]] = []

    def recording_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(deploy_module, "_run", recording_run)

    result = CliRunner().invoke(
        app,
        [
            "deploy-release",
            "--env=production",
            "--artifact",
            str(wheel),
            "--version=0.2.0",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "env: production" in result.output
    assert "version: 0.2.0" in result.output
    assert "artifact sha256:" in result.output
    assert "DRY RUN - no changes made." in result.output
    assert not calls
    assert not (checkout / "releases").exists()


def test_release_deploy_rejects_mismatched_version(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path, version="0.2.0")
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        [
            "deploy-release",
            "--env=production",
            "--artifact",
            str(wheel),
            "--version=9.9.9",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert "version mismatch" in result.output


def test_release_deploy_dry_run_does_not_plan_ngrok(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path)
    calls: list[list[str]] = []

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(
        deploy_module,
        "_run",
        lambda cmd, **kwargs: calls.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel), "--dry-run"],
    )

    assert result.exit_code == 0
    assert "ngrok" not in result.output.lower()
    assert calls == []
    assert not (checkout / "releases").exists()
    assert not (checkout / "data/backups").exists()


def test_release_deploy_rejects_uv_run_template_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path, unsafe_capacity_poller=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(
        deploy_module,
        "_run",
        lambda cmd, **kwargs: calls.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "production launchd template may not run uv" in result.output
    assert calls == []
    assert not (checkout / "releases").exists()


def test_release_deploy_rejects_missing_launchd_template_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    missing = "com.yang.nyc-ai-events-backup.plist"
    wheel = _make_wheel(tmp_path, omitted_plist=missing)
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "missing launchd template" in result.output
    assert not (checkout / "releases").exists()


def test_release_deploy_rejects_launchd_label_mismatch_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path, wrong_capacity_label=True)
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "launchd template label mismatch" in result.output
    assert not (checkout / "releases").exists()


def test_release_deploy_rejects_shell_unsafe_runtime_path_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime;unsafe"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path)
    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "runtime path contains unsupported characters" in result.output
    assert not (checkout / "releases").exists()


def test_release_deploy_backs_up_and_reloads_release_launchd_templates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    db_path = checkout / "data" / "events.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"sqlite-db")
    cfg = _config(tmp_path, checkout, name="production", production=True, db_path=db_path)
    wheel = _make_wheel(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="not found")
        if len(cmd) >= 2 and cmd[0].endswith("/python") and cmd[1] == "-c":
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"version": "0.2.0", "editable": False}) + "\n",
                stderr="",
            )
        if len(cmd) >= 2 and cmd[0].endswith("/nyc-ai-scout") and cmd[1] == "migrate":
            backups = list((checkout / "data" / "backups").glob("pre-release-0.2.0-*.db"))
            assert backups, "release backup must exist before migration runs"
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(deploy_module, "_run", fake_run)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda port: None)
    monkeypatch.setattr(deploy_module, "check_http_health", lambda cfg: None)
    monkeypatch.setattr(deploy_module, "check_browser_surface", lambda cfg: None)
    launch_agents_dir = tmp_path / "LaunchAgents"
    monkeypatch.setattr(
        deploy_module,
        "_launch_agents_dir",
        lambda: launch_agents_dir,
        raising=False,
    )
    launch_agents_dir.mkdir()
    legacy_alias = launch_agents_dir / "capacity-poller.plist"
    legacy_alias.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.yang.nyc-ai-events.capacity-poller",
                "ProgramArguments": ["/usr/local/bin/uv", "run", "nyc-ai-scout"],
            }
        )
    )

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel)],
    )

    assert result.exit_code == 0
    backups = list((checkout / "data" / "backups").glob("pre-release-0.2.0-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"sqlite-db"
    assert not any(call[:1] == ["git"] for call in calls)
    assert any(call[0].endswith("uv") and call[1:3] == ["pip", "install"] for call in calls)
    alias_bootout_index = calls.index(
        [
            "launchctl",
            "bootout",
            f"gui/{deploy_module.os.getuid()}/com.yang.nyc-ai-events.capacity-poller",
        ]
    )
    install_index = next(
        index
        for index, call in enumerate(calls)
        if call[0].endswith("uv") and call[1:3] == ["pip", "install"]
    )
    assert alias_bootout_index < install_index
    assert not any(call[:2] == ["launchctl", "kickstart"] for call in calls)
    bootout_labels = {
        call[-1].rsplit("/", 1)[-1]
        for call in calls
        if call[:2] == ["launchctl", "bootout"]
    }
    bootstrap_plists = {
        Path(call[-1]).name
        for call in calls
        if call[:2] == ["launchctl", "bootstrap"]
    }
    expected_labels = {name.removesuffix(".plist") for name in PRODUCTION_PLIST_NAMES}
    assert bootout_labels == expected_labels
    assert bootstrap_plists == set(PRODUCTION_PLIST_NAMES)
    assert not legacy_alias.exists()
    disabled_aliases = list(launch_agents_dir.glob("capacity-poller.plist*.disabled"))
    assert len(disabled_aliases) == 1
    assert plistlib.loads(disabled_aliases[0].read_bytes())["Label"] == (
        "com.yang.nyc-ai-events.capacity-poller"
    )

    installed_capacity_poller = (
        launch_agents_dir / "com.yang.nyc-ai-events.capacity-poller.plist"
    ).read_text()
    assert "release-artifact-template" in installed_capacity_poller
    assert "RUNTIME_PATH" not in installed_capacity_poller
    assert str(checkout) in installed_capacity_poller
    assert "uv run" not in installed_capacity_poller
    metadata = json.loads((checkout / "data" / "current-release.json").read_text())
    assert metadata["name"] == "nyc-ai-events"
    assert metadata["version"] == "0.2.0"
    assert metadata["artifact_sha256"]
    assert Path(metadata["db_backup_path"]).read_bytes() == b"sqlite-db"


def test_quiesce_launchd_aliases_is_scoped_and_uses_recoverable_names(
    tmp_path: Path,
) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    label = "com.yang.nyc-ai-events.capacity-poller"
    canonical_name = f"{label}.plist"
    rendered = {canonical_name: (label, "canonical")}
    canonical = launch_agents_dir / canonical_name
    canonical.write_bytes(plistlib.dumps({"Label": label}))
    alias = launch_agents_dir / "capacity-poller.plist"
    alias.write_bytes(plistlib.dumps({"Label": label}))
    first_disabled = launch_agents_dir / "capacity-poller.plist.disabled"
    first_disabled.write_text("existing recovery copy")
    unrelated = launch_agents_dir / "unrelated.plist"
    unrelated.write_bytes(plistlib.dumps({"Label": "com.example.unrelated"}))
    malformed = launch_agents_dir / "malformed.plist"
    malformed.write_text("not a plist")
    calls: list[list[str]] = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            3 if cmd[:2] == ["launchctl", "print"] else 0,
            stdout="",
            stderr="",
        )

    disabled = deploy_module._quiesce_launchd_label_aliases(
        launch_agents_dir,
        rendered=rendered,
        uid=501,
        run=run,
    )

    expected_disabled = launch_agents_dir / "capacity-poller.plist.1.disabled"
    assert disabled == (expected_disabled,)
    assert not alias.exists()
    assert plistlib.loads(expected_disabled.read_bytes())["Label"] == label
    assert first_disabled.read_text() == "existing recovery copy"
    assert plistlib.loads(canonical.read_bytes())["Label"] == label
    assert plistlib.loads(unrelated.read_bytes())["Label"] == "com.example.unrelated"
    assert malformed.read_text() == "not a plist"
    assert calls == [
        ["launchctl", "bootout", f"gui/501/{label}"],
        ["launchctl", "print", f"gui/501/{label}"],
    ]


def test_release_deploy_aborts_before_install_when_alias_remains_loaded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    db_path = checkout / "data" / "events.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"sqlite-db")
    cfg = _config(tmp_path, checkout, name="production", production=True, db_path=db_path)
    wheel = _make_wheel(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    (launch_agents_dir / "capacity-poller.plist").write_bytes(
        plistlib.dumps({"Label": "com.yang.nyc-ai-events.capacity-poller"})
    )
    calls: list[list[str]] = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(deploy_module, "_run", run)
    monkeypatch.setattr(deploy_module, "_launch_agents_dir", lambda: launch_agents_dir)

    result = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel)],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, deploy_module.SmokeFailure)
    assert "remains loaded" in str(result.exception)
    assert not any(call[0].endswith("uv") and call[1:3] == ["pip", "install"] for call in calls)

    calls.clear()
    retry = CliRunner().invoke(
        app,
        ["deploy-release", "--env=production", "--artifact", str(wheel)],
    )

    assert retry.exit_code != 0
    assert isinstance(retry.exception, deploy_module.SmokeFailure)
    assert "remains loaded" in str(retry.exception)
    service_target = (
        f"gui/{deploy_module.os.getuid()}/com.yang.nyc-ai-events.capacity-poller"
    )
    assert ["launchctl", "bootout", service_target] in calls
    assert ["launchctl", "print", service_target] in calls
    assert not any(call[0].endswith("uv") and call[1:3] == ["pip", "install"] for call in calls)


def test_launchd_install_retries_transient_bootstrap_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path)
    release = deploy_module._read_release_artifact(wheel)
    launch_agents_dir = tmp_path / "LaunchAgents"
    monkeypatch.setattr(deploy_module, "_launch_agents_dir", lambda: launch_agents_dir)
    calls: list[list[str]] = []
    delays: list[float] = []
    web_attempts = 0

    def run(cmd, **_kwargs):
        nonlocal web_attempts
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "bootstrap"] and cmd[-1].endswith(
            "com.yang.nyc-ai-events-web.plist"
        ):
            web_attempts += 1
            if web_attempts == 1:
                raise subprocess.CalledProcessError(5, cmd, stderr="Input/output error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    deploy_module.install_release_launchd_templates(
        cfg,
        release=release,
        uid=501,
        run=run,
        sleep=delays.append,
    )

    assert web_attempts == 2
    assert delays == [deploy_module.LAUNCHD_BOOTSTRAP_RETRY_SECONDS]
    assert len([call for call in calls if call[:2] == ["launchctl", "bootstrap"]]) == 7


def test_launchd_install_stops_after_bounded_bootstrap_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path)
    release = deploy_module._read_release_artifact(wheel)
    monkeypatch.setattr(deploy_module, "_launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    attempts = 0
    delays: list[float] = []

    def run(cmd, **_kwargs):
        nonlocal attempts
        if cmd[:2] == ["launchctl", "bootstrap"]:
            attempts += 1
            raise subprocess.CalledProcessError(
                4 + attempts,
                cmd,
                stderr=f"bootstrap failure {attempts}",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        deploy_module.install_release_launchd_templates(
            cfg,
            release=release,
            uid=501,
            run=run,
            sleep=delays.append,
        )

    assert exc_info.value.returncode == 7
    assert isinstance(exc_info.value.__cause__, subprocess.CalledProcessError)
    assert exc_info.value.__cause__.returncode == 5
    assert attempts == deploy_module.LAUNCHD_BOOTSTRAP_ATTEMPTS
    assert delays == [deploy_module.LAUNCHD_BOOTSTRAP_RETRY_SECONDS] * (
        deploy_module.LAUNCHD_BOOTSTRAP_ATTEMPTS - 1
    )


def test_production_smoke_checks_release_identity(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    checked: list[EnvConfig] = []

    monkeypatch.setattr(deploy_module, "resolve_env", lambda name: cfg)
    monkeypatch.setattr(deploy_module, "check_http_health", lambda cfg: None)
    monkeypatch.setattr(deploy_module, "check_browser_surface", lambda cfg, **kwargs: None)
    monkeypatch.setattr(deploy_module, "check_launchd_service", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_module, "check_sqlite_migration_drift", lambda cfg: None)
    monkeypatch.setattr(deploy_module, "check_scoring_breaker", lambda cfg: None)
    monkeypatch.setattr(
        deploy_module,
        "check_release_identity",
        lambda cfg: checked.append(cfg),
        raising=False,
    )

    result = CliRunner().invoke(app, ["smoke", "--env=production"])

    assert result.exit_code == 0
    assert checked == [cfg]


def test_release_artifact_target_preserves_valid_wheel_filename(tmp_path: Path) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    cfg = _config(tmp_path, checkout, name="production", production=True)
    wheel = _make_wheel(tmp_path, version="0.2.0")
    release = deploy_module._read_release_artifact(wheel)

    assert deploy_module._release_artifact_target(cfg, release) == (
        checkout / "releases" / "v0.2.0" / wheel.name
    )


def test_release_runtime_env_uses_safe_python_path(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path, database_path=tmp_path / "events.db", name="production")

    assert deploy_module._runtime_env(env_file)["PYTHONSAFEPATH"] == "1"


def test_release_deploy_resolves_uv_from_runtime_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    uv = fallback_dir / "uv"
    uv.write_text("")

    monkeypatch.setattr(deploy_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(deploy_module, "UV_FALLBACK_DIRS", (fallback_dir,))

    assert deploy_module._resolve_uv_bin("uv") == str(uv)


def test_release_deploy_preserves_explicit_uv_path() -> None:
    assert deploy_module._resolve_uv_bin("/custom/bin/uv") == "/custom/bin/uv"


def test_runtime_deploy_template_dir_prefers_packaged_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api_root = tmp_path / "site-packages" / "api"
    packaged = api_root / "_runtime" / "deploy"
    packaged.mkdir(parents=True)
    module_file = api_root / "commands" / "deploy.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("")

    monkeypatch.setattr(deploy_module, "__file__", str(module_file))

    assert runtime_deploy_template_dir() == packaged
