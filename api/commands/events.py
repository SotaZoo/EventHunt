"""Operator review of catalog facts; no in-app administration or guessed corrections."""
import typer

from api.repositories import _storage as storage
from api.repositories.time_warnings import review_time_warning

event_commands = typer.Typer(no_args_is_help=True)


@event_commands.command('time-warning')
def time_warning(url: str = typer.Option(..., '--url'), evidence: str = typer.Option(..., '--evidence'),
                 clear: bool = typer.Option(False, '--clear')) -> None:
    """Flag a verified date/time conflict, or clear it after reviewing resolution evidence."""
    database = storage.open_database()
    try:
        with database.compat_connect() as conn:
            count = review_time_warning(conn, url=url, evidence=evidence, clear=clear)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    finally:
        database.engine.dispose()
    typer.echo(f'Time warning {"cleared" if clear else "recorded"} for {count} source listing(s).')
