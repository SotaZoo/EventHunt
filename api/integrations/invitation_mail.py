"""Brevo transactional HTTPS boundary; API acceptance is not inbox delivery."""
import os
from dataclasses import dataclass
from html import escape

import httpx


class MailNotSent(Exception):
    """Definitively not accepted by the mail provider."""


class DeliveryUnknown(Exception):
    """The provider might have accepted the submission."""


@dataclass(frozen=True)
class InvitationMessage:
    recipient: str
    subject: str
    body: str
    message_id: str


class BrevoMailer:
    def send(self, message: InvitationMessage) -> str:
        """Submit once and return the provider identifier only on confirmed acceptance."""
        key = os.environ.get("EVENT_HUNT_BREVO_API_KEY", "")
        if not key:
            raise MailNotSent("not_configured")
        payload = {
            "sender": {"name": "Event Hunt", "email": "info@sotazoo.com"},
            "replyTo": {"name": "Event Hunt", "email": "info@sotazoo.com"},
            "to": [{"email": message.recipient}],
            "subject": message.subject,
            "textContent": message.body,
            "htmlContent": "<html><body><p>" + escape(message.body).replace("\n", "<br>") + "</p></body></html>",
            "headers": {"X-Event-Hunt-Message-ID": message.message_id},
        }
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.post("https://api.brevo.com/v3/smtp/email", headers={"api-key": key}, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise MailNotSent("connection_failed") from None
        except httpx.HTTPError:
            raise DeliveryUnknown("connection_lost") from None
        if 400 <= response.status_code < 500:
            raise MailNotSent("quota_exceeded" if response.status_code == 429 else "provider_rejected")
        if response.status_code != 201:
            raise DeliveryUnknown("unexpected_response")
        try:
            data = response.json()
        except ValueError:
            raise DeliveryUnknown("invalid_receipt") from None
        identifier = data.get("messageId") if isinstance(data, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 1024 or any(ord(c) < 32 for c in identifier):
            raise DeliveryUnknown("invalid_receipt")
        return identifier
