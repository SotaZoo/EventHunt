"""Invitation lifecycle; storage and mail remain explicit boundaries."""
from email.errors import HeaderParseError
from email.headerregistry import Address
from urllib.parse import urlsplit

from api.integrations.invitation_mail import DeliveryUnknown, InvitationMessage, MailNotSent
from api.repositories.invitations import InvitationRepository, RequestLimited


def request_email(value: object) -> str:
    """Validate an address without treating it as identity proof or requiring Gmail."""
    if not isinstance(value, str):
        raise ValueError("Enter a valid email address.")
    value = value.strip().lower()
    if not value or len(value) > 254 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Enter a valid email address.")
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError):
        raise ValueError("Enter a valid email address.") from None
    if not address.username or "." not in address.domain or address.addr_spec != value:
        raise ValueError("Enter a valid email address.")
    return value


class InvitationService:
    def __init__(self, repository: InvitationRepository, mailer, base_url: str):
        self.repository, self.mailer, self.base_url = repository, mailer, base_url.rstrip("/")

    def deliver(self, mail_id: int, *, retry: bool = False) -> str:
        """Attempt one claimed message and persist the provider's bounded outcome."""
        try:
            work = self.repository.claim(mail_id, retry=retry)
        except RequestLimited:
            return self.repository.mail_status(mail_id)
        if work is None:
            return self.repository.mail_status(mail_id)
        if work["kind"] == "notification":
            message = InvitationMessage(
                recipient="info@sotazoo.com", subject="Event Hunt: invitation request",
                body=f"Event Hunt invitation request #{work['request_id']}\n\n{work['email']}\n\nReview with: uv run nyc-ai-scout user requests",
                message_id=work["message_id"],
            )
        else:
            url = urlsplit(self.base_url)
            if url.scheme != "https" or not url.netloc or url.username or url.query or url.fragment:
                self.repository.finish(mail_id, "failed", "public_url_not_configured", attempt_token=work["attempt_token"])
                return "failed"
            message = InvitationMessage(
                recipient=work["email"], subject="You're invited to Event Hunt",
                body=(f"You're invited to Event Hunt.\n\nSign in with Google using {work['email']}:\n"
                      f"{self.base_url}/login\n\n"
                      "If this address isn't linked to a Google account, request again with a Google-linked "
                      "email address. That request needs separate approval.\n\n"
                      "This sign-in link does not grant access to another account.\n\n"
                      "Questions? Contact info@sotazoo.com."),
                message_id=work["message_id"],
            )
        try:
            provider_id = self.mailer.send(message)
        except MailNotSent as exc:
            self.repository.finish(mail_id, "failed", str(exc), attempt_token=work["attempt_token"])
        except DeliveryUnknown as exc:
            self.repository.finish(mail_id, "unknown", str(exc), attempt_token=work["attempt_token"])
        else:
            self.repository.finish(mail_id, "sent", attempt_token=work["attempt_token"], provider_message_id=provider_id)
        return self.repository.mail_status(mail_id)
