import httpx
import pytest

from api.integrations.invitation_mail import (
    BrevoMailer,
    DeliveryUnknown,
    InvitationMessage,
    MailNotSent,
)

MESSAGE = InvitationMessage("friend@example.test", "Event Hunt", "Synthetic invitation", "synthetic-message")


def test_missing_sender_is_definitively_unsent():
    with pytest.raises(MailNotSent, match="not_configured"):
        BrevoMailer().send(MESSAGE)


@pytest.mark.parametrize('error, expected', [
    (httpx.ConnectError('private provider detail'), MailNotSent),
    (httpx.ConnectTimeout('private provider detail'), MailNotSent),
    (httpx.ReadTimeout('private provider detail'), DeliveryUnknown),
    (httpx.WriteError('private provider detail'), DeliveryUnknown),
])
def test_http_failure_classification_does_not_expose_provider_details(monkeypatch, error, expected):
    monkeypatch.setenv('EVENT_HUNT_BREVO_API_KEY', 'synthetic-key')

    def provider(request):
        raise error

    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    with pytest.raises(expected) as caught:
        BrevoMailer().send(MESSAGE)
    assert 'private provider detail' not in str(caught.value)
