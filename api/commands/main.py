from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich import print as rprint
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from api.commands.deploy import deploy_app
from api.commands.events import event_commands
from api.commands.invitations import invitation_commands
from api.configs.settings import (
    database_path,
    database_settings,
    friend_mode_enabled,
    google_oauth_config_errors,
    google_oauth_configured,
    public_base_url,
    public_base_url_config_error,
    session_secret_config_error,
    supabase_config_errors,
    supabase_configured,
    supabase_publishable_key,
    supabase_url,
    taste_profile_text,
)
from api.integrations.llm import CURRENT_SCORING_PROMPT
from api.integrations.scrapers import SCRAPERS
from api.repositories import _storage as storage

DEFAULT_SEED_PATH = Path("data/taste-seed.txt")
DEFAULT_TASTE_PROFILE_PATH = Path("data/taste-profile.md")
DEFAULT_EVAL_OUT_PATH = Path("tests/eval/results-{version}.json")
DEFAULT_BACKUP_DIR = Path("data/backups")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="NYC AI events agent.")
user_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage private-alpha users.")
app.add_typer(user_app, name="user")
app.add_typer(event_commands, name="event")
user_app.add_typer(invitation_commands)
app.add_typer(deploy_app, name=None)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _open_cli_db():
    return storage.open_database().open_compat()


@user_app.command("add")
def user_add(
    email: str = typer.Option(..., "--email", help="User email"),
    name: str = typer.Option(..., "--name", help="Display name"),
) -> None:
    """Create an active private-alpha user and print a one-time invite code."""
    import secrets
    invite_code = secrets.token_urlsafe(24)
    conn = _open_cli_db()
    try:
        try:
            user = storage.create_user(
                conn,
                email=email,
                display_name=name,
                invite_code=invite_code,
            )
        except (sqlite3.IntegrityError, SAIntegrityError):
            rprint(f"[red]User already exists: {email.strip().lower()}[/red]")
            raise typer.Exit(1) from None
    finally:
        conn.close()
    rprint(f"[green]Created user {user['email']} ({user['display_name']})[/green]")
    rprint(f"Invite code: {invite_code}")


@user_app.command("list")
def user_list() -> None:
    """List private-alpha users without leaking invite codes."""
    conn = _open_cli_db()
    try:
        rows = storage.list_users(conn)
    finally:
        conn.close()
    if not rows:
        rprint("[yellow]No users yet.[/yellow]")
        return
    for row in rows:
        status = "active" if row["is_active"] else "inactive"
        rprint(f"{row['id']:>3}  {row['email']}  {row['display_name']}  {status}")


@user_app.command("invite-google")
def user_invite_google(
    email: str = typer.Option(..., "--email", help="Friend email allowed to use Google sign-in"),
) -> None:
    """Allow a friend email to sign in with Google."""
    conn = _open_cli_db()
    try:
        invite = storage.create_alpha_invite(conn, email=email)
    finally:
        conn.close()
    rprint(f"[green]Google sign-in allowed for {invite['normalized_email']}[/green]")


@user_app.command("list-google-invites")
def user_list_google_invites() -> None:
    """List Google sign-in allowlist entries."""
    conn = _open_cli_db()
    try:
        rows = storage.list_alpha_invites(conn)
    finally:
        conn.close()
    if not rows:
        rprint("[yellow]No Google invites yet.[/yellow]")
        return
    for row in rows:
        if row["revoked_at"]:
            status = "revoked"
        elif row["accepted_user_id"]:
            status = f"accepted:{row['accepted_user_email']}"
        else:
            status = "pending"
        rprint(f"{row['id']:>3}  {row['normalized_email']}  {status}")


@user_app.command("revoke-google-invite")
def user_revoke_google_invite(
    email: str = typer.Option(..., "--email", help="Friend email to revoke"),
) -> None:
    """Revoke a Google allowlist entry and deactivate its accepted user."""
    revoked = None
    conn = _open_cli_db()
    try:
        revoked = storage.revoke_alpha_invite(conn, email)
    finally:
        conn.close()
    normalized_email = storage.normalize_email(email)
    if revoked is None:
        rprint(f"[yellow]No active Google invite for {normalized_email}[/yellow]")
        return
    rprint(f"[green]Revoked Google sign-in for {normalized_email}[/green]")


@user_app.command("rotate-invite")
def user_rotate_invite(
    email: str = typer.Option(..., "--email", help="User email"),
) -> None:
    """Print a new one-time invite code and invalidate the old hash."""
    import secrets

    invite_code = secrets.token_urlsafe(24)
    conn = _open_cli_db()
    try:
        rowcount = storage.rotate_user_invite(conn, email, invite_code)
    finally:
        conn.close()
    normalized_email = storage.normalize_email(email)
    if rowcount == 0:
        rprint(f"[red]User not found: {normalized_email}[/red]")
        raise typer.Exit(1)
    rprint(f"[green]Rotated invite for {normalized_email}[/green]")
    rprint(f"Invite code: {invite_code}")


@user_app.command("deactivate")
def user_deactivate(
    email: str = typer.Option(..., "--email", help="User email"),
) -> None:
    """Deactivate a user. Existing browser sessions are rejected on next request."""
    conn = _open_cli_db()
    try:
        rowcount = storage.deactivate_user(conn, email)
    finally:
        conn.close()
    normalized_email = storage.normalize_email(email)
    if rowcount == 0:
        rprint(f"[red]User not found: {normalized_email}[/red]")
        raise typer.Exit(1)
    rprint(f"[green]Deactivated {normalized_email}[/green]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind"),
    port: int = typer.Option(8847, help="Port to bind"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the private-alpha web app."""
    import os

    import uvicorn

    from api.app import create_app

    _setup_logging(verbose)
    shared_alpha = friend_mode_enabled()
    base_url = public_base_url()
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if shared_alpha or host not in local_hosts:
        base_url_error = public_base_url_config_error(base_url)
        if base_url_error is not None:
            rprint(f"[red]{base_url_error} before shared/private-alpha access.[/red]")
            raise typer.Exit(2)
        secret_error = session_secret_config_error(os.environ.get("NYC_AI_EVENTS_SESSION_SECRET"))
        if secret_error is not None:
            rprint(f"[red]{secret_error} before shared/private-alpha access.[/red]")
            raise typer.Exit(2)
        if not (google_oauth_configured() or supabase_configured()):
            oauth_errors = (
                supabase_config_errors()
                if supabase_url() or supabase_publishable_key()
                else google_oauth_config_errors()
            )
            rprint("[red]" + "; ".join(oauth_errors) + " for friend mode.[/red]")
            raise typer.Exit(2)

    web_app = create_app()
    uvicorn.run(web_app, host=host, port=port, log_level="debug" if verbose else "info")


@app.command()
def scrape(
    source: str = typer.Option("all", help=f"Source name or 'all'. Available: {list(SCRAPERS)}"),
    limit: int = typer.Option(50, help="Max events per source"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scrape events from one or all sources into the local DB."""
    _setup_logging(verbose)
    if source == "all":
        sources = list(SCRAPERS)
    elif source in SCRAPERS:
        sources = [source]
    else:
        rprint(f"[red]Unknown source: {source}. Available: {list(SCRAPERS)} or 'all'[/red]")
        raise typer.Exit(1)
    conn = _open_cli_db()
    total = 0
    completed = 0
    try:
        for s in sources:
            rprint(f"[cyan]→ {s}[/cyan]")
            try:
                scraper = SCRAPERS[s]()
                events = scraper.scrape(limit=limit)
                for raw in events:
                    storage.upsert_raw(conn, raw)
                total += len(events)
                completed += 1
                rprint(f"[green]  {s}: {len(events)} events[/green]")
            except Exception as e:
                rprint(f"[red]  {s} failed: {e}[/red]")
    finally:
        conn.close()
    if not completed:
        raise typer.Exit(1)
    rprint(f"[green]Done. {total} events total across {completed} of {len(sources)} sources.[/green]")


@app.command(name="eval")
def run_eval(
    prompt_version: str = typer.Option(
        CURRENT_SCORING_PROMPT, "--prompt-version", help="Frozen prompt under prompts/scoring/<version>.md"
    ),
    seed_path: Annotated[
        Path,
        typer.Option("--seed", help="Hand-rated taste seed"),
    ] = DEFAULT_SEED_PATH,
    profile_path: Annotated[
        Path,
        typer.Option(
            "--taste-profile",
            help="Markdown taste profile fed to the scoring prompt",
        ),
    ] = DEFAULT_TASTE_PROFILE_PATH,
    batch_size: int = typer.Option(20, help="Events per LLM call (K=20 default)"),
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Where to write the JSON report. {version} expands to --prompt-version.",
        ),
    ] = DEFAULT_EVAL_OUT_PATH,
    gate: float = typer.Option(0.70, help="precision@5 hard gate"),
    threshold: float = typer.Option(0.70, help="fit_score threshold for recall metric"),
    limit: int = typer.Option(0, help="Cap scored events (0 = all)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the M0 precision@5 gate against the taste seed."""
    from api.integrations.llm import score_batch
    from api.tasks.eval_load_seed import load
    from api.tasks.eval_precision import ScoredRecord, evaluate

    _setup_logging(verbose)

    if not seed_path.exists():
        rprint(f"[red]seed not found: {seed_path}. Pass a labeled event seed with --seed.[/red]")
        raise typer.Exit(2)
    if not profile_path.exists():
        rprint(
            f"[red]taste profile not found: {profile_path}. Copy "
            f"data/taste-profile.example.md → {profile_path} and edit.[/red]"
        )
        raise typer.Exit(2)

    seed_records = load(seed_path)
    if limit and limit < len(seed_records):
        seed_records = seed_records[:limit]

    if not seed_records:
        rprint("[red]Seed has no labeled records. Aborting.[/red]")
        raise typer.Exit(2)

    taste_profile = profile_path.read_text()
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")

    rprint(
        f"[cyan]Eval prompt={prompt_version}  seed={len(seed_records)} records  "
        f"batch={batch_size}  threshold={threshold}  gate={gate}[/cyan]"
    )

    # Synthetic int ids — load_seed records are positional anyway.
    events = [
        {
            "id": i,
            "title": r.title,
            "date": r.date,
            "host": r.host,
            "notes": r.notes,
            "url": r.url,
        }
        for i, r in enumerate(seed_records)
    ]

    async def _run() -> list:
        all_fits: list = []
        for start in range(0, len(events), batch_size):
            chunk = events[start : start + batch_size]
            rprint(f"  scoring batch {start}-{start + len(chunk) - 1}…")
            fits = await score_batch(
                chunk,
                taste_profile=taste_profile,
                today=today_str,
                prompt_version=prompt_version,
            )
            all_fits.extend(fits)
        return all_fits

    fits = asyncio.run(_run())

    if len(fits) != len(seed_records):
        rprint(
            f"[yellow]warning: scored {len(fits)} / {len(seed_records)} records "
            f"(some events were dropped or hallucinated)[/yellow]"
        )
    if len(fits) < 0.8 * len(seed_records):
        rprint(
            f"[red]too few events scored ({len(fits)} / {len(seed_records)}) — "
            f"likely scoring failure; not running gate.[/red]"
        )
        raise typer.Exit(2)

    fit_by_id = {f.event_id: f for f in fits}
    scored = [
        ScoredRecord(seed=seed_records[ev["id"]], fit=fit_by_id[ev["id"]])
        for ev in events
        if ev["id"] in fit_by_id
    ]

    report = evaluate(scored, threshold=threshold, gate=gate)

    out_resolved = Path(str(out).format(version=prompt_version))
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "prompt_version": prompt_version,
        "ran_at": datetime.now(UTC).isoformat(),
        "today": today_str,
        **report.to_dict(),
    }
    out_resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    rprint("")
    rprint("[bold]Top 5 by fit_score:[/bold]")
    for r in report.top_5:
        marker = "[green]+1[/green]" if r.label == 1 else (
            "[red]-1[/red]" if r.label == -1 else "[dim]0[/dim]"
        )
        rprint(f"  {marker}  {r.fit.fit_score:.2f}  {r.fit.urgency:<6}  {r.seed.title[:80]}")

    rprint("")
    urg = (
        f"{report.urgency_precision:.2%}"
        if report.urgency_precision is not None
        else "n/a (no urgency=high flagged)"
    )
    rprint(
        f"[bold]precision@5 = {report.precision_at_5:.2%}[/bold]  "
        f"recall@{threshold} = {report.recall_at_threshold:.2%}  "
        f"urgency_precision = {urg}"
    )
    rprint(f"  scored: {report.n_scored}  +1: {report.n_positive}  -1: {report.n_negative}")
    rprint(f"  → {out_resolved}")

    if report.passed_gate:
        rprint(f"[bold green]GATE PASSED ({report.precision_at_5:.2%} ≥ {gate:.0%})[/bold green]")
    else:
        rprint(f"[bold red]GATE FAILED ({report.precision_at_5:.2%} < {gate:.0%})[/bold red]")
        raise typer.Exit(1)


@app.command()
def cycle(
    sources: str = typer.Option("all", help="Comma-list of source names or 'all'"),
    high_bar_threshold: float = typer.Option(
        0.85,
        "--high-bar-threshold",
        envvar="HIGH_BAR_THRESHOLD",
        help=(
            "Scout-bot push cutoff (spec §10 step 1; migration 0006). Top-1 "
            "unpushed future event whose fit_score crosses this bar is posted "
            "to #scout each cycle. Default 0.85 keeps the channel quiet. "
            "Lower it to widen the cohort; raise it to silence pushes "
            "entirely while keeping the rest of the cycle alive."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """One scrape → score → post cycle. The heart of v1."""
    import os

    from api.tasks.cycle import CycleConfig, run_cycle

    _setup_logging(verbose)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        rprint("[red]DISCORD_WEBHOOK_URL not set[/red]")
        raise typer.Exit(2)
    try:
        taste_profile = taste_profile_text()
    except FileNotFoundError as exc:
        rprint(f"[red]missing taste profile: {exc}[/red]")
        raise typer.Exit(2) from None
    src_tuple: tuple[str, ...]
    if sources == "all":
        src_tuple = tuple(SCRAPERS)
    else:
        src_tuple = tuple(s.strip() for s in sources.split(",") if s.strip())
    cfg = CycleConfig(
        db_path=database_path() if database_settings().dialect == "sqlite" else None,
        data_dir=Path("data"),
        webhook_url=webhook,
        taste_profile=taste_profile,
        prompt_version=os.environ.get("PROMPT_VERSION", CURRENT_SCORING_PROMPT),
        high_bar_threshold=high_bar_threshold,
        scrape_sources=src_tuple,
    )
    result = run_cycle(cfg)
    if result.skipped_reason:
        rprint(f"[yellow]skipped: {result.skipped_reason}[/yellow]")
        return
    rprint(f"[green]scraped {result.scraped} · scored ok {result.scored_ok} · "
           f"failed {result.scored_failed} · posted {result.posted}[/green]")


@app.command()
def bot(
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Marker + log directory (default: data/)."),
    ] = Path("data"),
    db_path: Annotated[
        Path | None,
        typer.Option(
            "--db-path",
            help="SQLite path (default: from DATABASE_PATH or data/events.db).",
        ),
    ] = None,
    bot_token: str = typer.Option(
        ...,
        "--bot-token",
        envvar="DISCORD_BOT_TOKEN",
        help="Discord bot token. Read from DISCORD_BOT_TOKEN by default.",
    ),
    webhook_url: str = typer.Option(
        ...,
        "--webhook-url",
        envvar="DISCORD_WEBHOOK_URL",
        help="Webhook URL for /scout retract PATCH calls.",
    ),
    admin_discord_ids: str = typer.Option(
        "",
        "--admin-discord-ids",
        envvar="SCOUT_ADMIN_DISCORD_IDS",
        help="Comma-separated Discord user ids allowed to /scout retract.",
    ),
    friend_allowlist: str = typer.Option(
        "",
        "--friend-allowlist",
        envvar="SCOUT_FRIEND_ALLOWLIST",
        help=(
            "Comma-separated <discord_user_id>:<friend_email> pairs for "
            "/scout link onboarding."
        ),
    ),
    guild_ids: str = typer.Option(
        "",
        "--guild-ids",
        envvar="SCOUT_GUILD_IDS",
        help=(
            "Comma-separated Discord guild ids. When set, slash commands "
            "sync per-guild (instant). When empty, falls back to global "
            "sync (up to 1h propagation). Single-server friend deploys "
            "should always set this."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the scout-bot Discord slash-command service.

    Long-running. Reads ``Marker.BOT_DISABLED`` at startup; exits
    cleanly if set so Yang can kill the service via marker without
    unloading launchd. Logs land in ``<data_dir>/logs/scout-bot.log``
    (rotating).
    """
    from api.integrations.discord.bot import load_bot_config, run_bot

    _setup_logging(verbose)
    resolved_db_path = db_path if db_path is not None else database_path()
    # Don't echo the token. ``typer.echo`` and ``rprint`` of the resolved
    # ``BotConfig`` would leak the bot token; intentionally print only
    # safe paths.
    rprint(f"[cyan]scout-bot starting · data_dir={data_dir} db={resolved_db_path}[/cyan]")
    cfg = load_bot_config(
        data_dir=data_dir,
        db_path=resolved_db_path,
        bot_token=bot_token,
        webhook_url=webhook_url,
        admin_discord_ids=admin_discord_ids,
        friend_allowlist=friend_allowlist,
        guild_ids=guild_ids,
    )
    run_bot(cfg)


@app.command(name="poll-capacity")
def poll_capacity(
    db_path: Annotated[
        Path | None,
        typer.Option(
            "--db-path",
            help="SQLite path (default: from DATABASE_PATH or data/events.db).",
        ),
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Max events to refresh in one pass (default: 100).",
        ),
    ] = 100,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v"),
    ] = False,
) -> None:
    """Refresh capacity for pushed-and-future events and PATCH Discord (spec §4).

    Hourly task. Reads ``DISCORD_WEBHOOK_URL`` (same webhook the scout-bot
    pushes through). The launchd plist (Task 7) drives the cadence.

    Independent of the ``cycle`` command — capacity polling has nothing
    to scrape-score-post; it only touches the rows the scout-bot already
    pushed and edits the existing Discord messages.
    """
    import os

    from api.tasks.capacity_poller import CapacityPollerConfig, run_capacity_poll

    _setup_logging(verbose)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        rprint("[red]DISCORD_WEBHOOK_URL not set[/red]")
        raise typer.Exit(2)
    resolved_db_path = db_path if db_path is not None else (
        database_path() if database_settings().dialect == "sqlite" else None
    )
    cfg = CapacityPollerConfig(
        db_path=resolved_db_path,
        webhook_url=webhook,
        batch_size=batch_size,
    )
    result = run_capacity_poll(cfg)
    rprint(
        f"[green]scanned {result.scanned} · scrape ok {result.scrapes_ok} · "
        f"scrape fail {result.scrapes_failed} · patch ok {result.patches_ok} · "
        f"patch fail {result.patches_failed} · cancelled {result.cancellations}[/green]"
    )


@app.command(name="requeue-scoring-failures")
def requeue_scoring_failures_command(
    reason: Annotated[
        str,
        typer.Option("--reason", help="Failed scoring reason to requeue."),
    ] = "CliFallbackError",
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Maximum rows to requeue."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually mutate rows. Default is dry-run."),
    ] = False,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Requeue safe future scoring failures for the next cycle.

    Safety predicates live in ``api.repositories._storage`` and intentionally
    exclude past events, posted Discord rows, claimed rows, cancellations, and
    admin retractions. Default dry-run prevents accidental production mutation.
    """
    _setup_logging(verbose)
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    conn = _open_cli_db()
    try:
        if not apply:
            count = storage.count_requeueable_scoring_failures(
                conn,
                reason=reason,
                now_iso=now_iso,
                limit=limit,
            )
            rprint(
                f"[yellow]DRY RUN: {count} row(s) would be requeued "
                f"for reason={reason!r}[/yellow]"
            )
            return
        count = storage.requeue_scoring_failures(
            conn,
            reason=reason,
            now_iso=now_iso,
            limit=limit,
        )
    finally:
        conn.close()
    rprint(f"[green]requeued {count} scoring failure row(s) for reason={reason!r}[/green]")


@app.command(name="reset-breaker")
def reset_breaker(
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Marker directory (default: data/)."),
    ] = Path("data"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Clear the scoring circuit breaker and reset the all-failed counter.

    Operator escape hatch for the SCORING_DISABLED marker. v1 has an
    auto-cooldown (default 6h, env: ``SCOUT_BREAKER_COOLDOWN_HOURS``) so
    transient CLI rate-limits self-heal; this command exists for the cases
    where an operator wants to retry NOW or after a longer hard outage.
    Removes ``.scoring_disabled`` and zeros ``consecutive_all_failed`` in
    both the JSON file and (when configured) the DB row.
    """
    from api.tasks.cleanup import CycleHealth, save_health
    from api.tasks.markers import Marker, clear_marker

    _setup_logging(verbose)
    cleared: list[str] = []

    marker_path = data_dir / Marker.SCORING_DISABLED.value
    if marker_path.exists():
        clear_marker(data_dir, Marker.SCORING_DISABLED)
        cleared.append(str(marker_path))

    settings = database_settings()
    if settings.dialect == "postgres":
        # DB-mode: also clear the runtime_markers row and zero the health row.
        from api.repositories import runtime_markers as marker_storage

        db = storage.create_database(settings)
        with db.begin() as conn:
            if marker_storage.is_runtime_marked(conn, Marker.SCORING_DISABLED.value):
                marker_storage.clear_runtime_marker(
                    conn, Marker.SCORING_DISABLED.value,
                )
                cleared.append(f"runtime_markers[{Marker.SCORING_DISABLED.value}]")
            marker_storage.save_cycle_health_row(
                conn,
                {
                    "consecutive_all_failed": 0,
                    "last_error": None,
                    "last_failed_at": None,
                    "last_success_at": None,
                },
            )
        cleared.append("cycle_health(db).consecutive_all_failed=0")
    else:
        save_health(data_dir, CycleHealth())
        cleared.append(str(data_dir / ".cycle_health.json") + " (consecutive_all_failed=0)")

    if not cleared:
        rprint("[yellow]Nothing to clear — breaker not engaged.[/yellow]")
        return
    rprint(f"[green]reset-breaker cleared {len(cleared)} item(s): {', '.join(cleared)}[/green]")


@app.command()
def migrate(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Apply pending SQL migrations from db/migrate/sqlite/."""
    from api.repositories.migrate import (
        apply_pending,
        apply_pending_sqlalchemy,
        migration_dir_for_dialect,
    )

    _setup_logging(verbose)
    settings = database_settings()
    mig_dir = migration_dir_for_dialect(settings.dialect)
    if settings.dialect == "sqlite":
        sqlite_path = Path(make_url(settings.url).database or str(database_path()))
        conn = storage.connect(sqlite_path)
        try:
            applied = apply_pending(conn, mig_dir)
        finally:
            conn.close()
    else:
        db = storage.create_database(settings)
        with db.begin() as conn:
            applied = apply_pending_sqlalchemy(conn, mig_dir)
    if not applied:
        rprint("[yellow]No pending migrations.[/yellow]")
        return
    for name in applied:
        rprint(f"[green]✓ {name}[/green]")
    rprint(f"[green]Applied {len(applied)} migration(s).[/green]")


@app.command()
def cleanup(
    days: int = typer.Option(30, help="Delete posted events older than N days"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Delete events that are posted AND older than N days."""
    _setup_logging(verbose)
    conn = _open_cli_db()
    try:
        deleted = storage.cleanup_posted_events(conn, days)
        rprint(f"[green]deleted {deleted} old posted events[/green]")
    finally:
        conn.close()


@app.command()
def backup(
    out_dir: Annotated[
        Path,
        typer.Option("--out-dir", help="Backup output directory"),
    ] = DEFAULT_BACKUP_DIR,
    retention_days: Annotated[
        int,
        typer.Option("--retention-days", help="Delete backups older than N days"),
    ] = 30,
    mirror_dir: Annotated[
        Path | None,
        typer.Option("--mirror-dir", help="Optional off-machine/synced mirror directory"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v"),
    ] = False,
) -> None:
    """Create a WAL-safe SQLite backup and prune old backups."""
    from api.tasks.backups import backup_sqlite_database

    _setup_logging(verbose)
    settings = database_settings()
    if settings.dialect != "sqlite":
        rprint("[red]backup currently supports SQLite DATABASE_PATH only[/red]")
        raise typer.Exit(2)
    sqlite_path = Path(make_url(settings.url).database or str(database_path()))
    try:
        result = backup_sqlite_database(
            source=sqlite_path,
            backup_dir=out_dir,
            retention_days=retention_days,
            mirror_dir=mirror_dir,
        )
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
        rprint(f"[red]backup failed: {exc}[/red]")
        raise typer.Exit(1) from None
    rprint(f"[green]Backup written: {result.path}[/green]")
    if result.mirror_path is not None:
        rprint(f"[green]Mirror written: {result.mirror_path}[/green]")
    if result.pruned:
        rprint(f"[yellow]Pruned {len(result.pruned)} old backup(s).[/yellow]")


if __name__ == "__main__":
    app()
