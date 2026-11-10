"""Manual approval and recoverable email delivery; no web admin surface."""
import typer

from api.configs.settings import public_base_url
from api.integrations.hosted_delivery import wake_hosted_sender
from api.repositories import _storage as storage
from api.repositories.invitations import InvitationRepository

invitation_commands = typer.Typer()


def repository() -> InvitationRepository:
    """Use the existing operator database credentials; no mail key is needed."""
    return InvitationRepository(database=storage.open_database())


def wake_sender() -> None:
    """Wake delivery without implying the provider accepted any message."""
    if not wake_hosted_sender(public_base_url()):
        typer.echo("Hosted sender unavailable. Queued mail is preserved; retry with user send-pending-invitations.")


@invitation_commands.command("requests")
def requests() -> None:
    """List requests, access state and mail IDs/status without granting access."""
    for row in repository().requests():
        status = "revoked" if row["revoked_at"] else "accepted" if row["accepted_at"] else "approved" if row["approved_at"] else "pending"
        typer.echo(f"{row['id']}  {row['email']}  {status}  "
                   f"notification:{row['notification_mail_id']}:{row['notification_status']}  "
                   f"invitation:{row['invitation_mail_id']}:{row['invitation_status'] or 'none'}  "
                   f"provider-notification:{row['notification_provider_id'] or 'none'}  "
                   f"provider-invitation:{row['invitation_provider_id'] or 'none'}")


@invitation_commands.command("approve-request")
def approve_request(request_id: int) -> None:
    """Approve once and wake production delivery; never send from the operator machine."""
    repo = repository()
    try:
        mail_id = repo.approve(request_id)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    if repo.mail_status(mail_id) == "pending":
        wake_sender()
    status = repo.mail_status(mail_id)
    typer.echo(f"Request {request_id}: approved. Invitation mail {mail_id}: {status}.")


@invitation_commands.command("retry-invitation-mail")
def retry_mail(mail_id: int) -> None:
    """Queue a definitively failed email for production, never an uncertain delivery."""
    repo = repository()
    try:
        status = repo.retry(mail_id)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    if status == "pending":
        wake_sender()
    status = repo.mail_status(mail_id)
    typer.echo(f"Mail {mail_id}: {status}.")


@invitation_commands.command("send-pending-invitations")
def send_pending() -> None:
    """Wake the hosted sender to resume pending work; never send from this machine."""
    repo = repository()
    pending = repo.pending_mail()
    if pending:
        wake_sender()
    for mail_id in pending:
        typer.echo(f"Mail {mail_id}: {repo.mail_status(mail_id)}.")


@invitation_commands.command("reconcile-invitation-mail")
def reconcile_mail(mail_id: int, outcome: str = typer.Option(..., "--outcome")) -> None:
    """After checking provider evidence, mark an uncertain send sent or not-sent."""
    try:
        repository().reconcile(mail_id, outcome)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    typer.echo(f"Mail {mail_id}: reconciled as {outcome}.")
