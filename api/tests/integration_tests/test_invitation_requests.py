import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from api.app import create_app
from api.commands.main import app
from api.repositories.events import connect
from api.tests.fixtures.web_helpers import add_user, migrated_db


class Mailbox:
    def __init__(self):
        self.messages = []
        self.error = None

    def send(self, message):
        if self.error:
            raise self.error
        self.messages.append(message)


@pytest.fixture
def invitations(tmp_path, monkeypatch, httpx_mock):
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    httpx_mock.add_response(method="GET", url="https://events.example.com/healthz",
                            is_optional=True, is_reusable=True)
    mailbox = Mailbox()
    client = TestClient(create_app(db_path=db, session_secret="test-secret", invitation_mailer=mailbox))
    return db, client, mailbox


def hosted_delivery(client, mail_id, expected):
    """Observe delivery through the hosted application's real startup lifecycle."""
    with client:
        repo = client.app.state.invitations.repository
        deadline = time.monotonic() + 3
        while repo.mail_status(mail_id) != expected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert repo.mail_status(mail_id) == expected


def test_request_is_durable_private_and_duplicate_safe(invitations):
    db, client, mailbox = invitations
    for email in [" Friend@Example.com ", "friend@example.com"]:
        response = client.post("/api/invitation-requests", json={"email": email})
        assert response.status_code == 202
        assert response.json() == {"message": "Request received. We'll email you if you're invited."}
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM invitation_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM invitation_mail").fetchone()[0] == 1
    assert len(mailbox.messages) == 1
    assert mailbox.messages[0].recipient == "info@sotazoo.com"
    assert "friend@example.com" in mailbox.messages[0].body
    assert client.get("/api/invitation-requests").status_code == 405


@pytest.mark.parametrize("email", ["", "not-email", "a@b\r\nBcc: victim@example.com", "a@@b.com"])
def test_invalid_request_is_not_saved(invitations, email):
    db, client, mailbox = invitations
    assert client.post("/api/invitation-requests", json={"email": email}).status_code == 422
    assert not mailbox.messages
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM invitation_requests").fetchone()[0] == 0


def test_public_request_has_source_and_global_limits(invitations):
    _, client, mailbox = invitations
    for index in range(5):
        assert client.post("/api/invitation-requests", json={"email": f"friend{index}@example.com"}).status_code == 202
    response = client.post("/api/invitation-requests", json={"email": "next@example.com"})
    assert response.status_code == 429
    assert response.headers["retry-after"]
    assert len(mailbox.messages) == 5


def test_cross_origin_and_oversized_submissions_are_rejected(invitations):
    _, client, mailbox = invitations
    assert client.post("/api/invitation-requests", json={"email": "a@example.com"}, headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/invitation-requests", content="x" * 4097).status_code == 413
    assert not mailbox.messages


def test_approval_is_idempotent_and_mail_retry_does_not_repeat_approval(invitations, monkeypatch):
    from api.integrations.invitation_mail import BrevoMailer, MailNotSent

    db, client, mailbox = invitations
    mailbox.error = MailNotSent("not_configured")
    assert client.post("/api/invitation-requests", json={"email": "friend@example.com"}).status_code == 202
    runner = CliRunner()
    monkeypatch.setattr(BrevoMailer, "send", lambda self, message: mailbox.send(message))
    listed = runner.invoke(app, ["user", "requests"])
    assert listed.exit_code == 0
    assert "pending" in listed.output and "failed" in listed.output
    approved = runner.invoke(app, ["user", "approve-request", "1"])
    assert approved.exit_code == 0, approved.output
    assert "approved" in approved.output and "pending" in approved.output
    hosted_delivery(client, 2, "failed")
    mailbox.error = None
    assert runner.invoke(app, ["user", "approve-request", "1"]).exit_code == 0
    # Repeat approval does not implicitly resend failed/uncertain work.
    assert not mailbox.messages
    assert runner.invoke(app, ["user", "retry-invitation-mail", "2"]).exit_code == 0
    assert not mailbox.messages
    hosted_delivery(client, 2, "sent")
    assert len(mailbox.messages) == 1
    message = mailbox.messages[0]
    assert message.recipient == "friend@example.com"
    assert "https://events.example.com/login" in message.body
    assert "request again" in message.body and "Google" in message.body
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alpha_invites").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM invitation_mail WHERE kind='invitation'").fetchone()[0] == 1


def test_inactive_user_cannot_be_reinstated_by_request_approval(invitations, monkeypatch):
    from api.integrations.invitation_mail import BrevoMailer

    db, client, mailbox = invitations
    add_user(db, email="friend@example.com")
    with connect(db) as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE email='friend@example.com'")
    client.post("/api/invitation-requests", json={"email": "friend@example.com"})
    monkeypatch.setattr(BrevoMailer, "send", lambda self, message: mailbox.send(message))
    result = CliRunner().invoke(app, ["user", "approve-request", "1"])
    assert result.exit_code == 1
    assert "inactive" in result.output
    assert len(mailbox.messages) == 1


def test_uncertain_delivery_requires_reconciliation_and_revocation_cancels_unsent(invitations, monkeypatch):
    from api.integrations.invitation_mail import BrevoMailer, DeliveryUnknown

    _, client, mailbox = invitations
    client.post("/api/invitation-requests", json={"email": "friend@example.com"})
    mailbox.error = DeliveryUnknown("connection_lost")
    monkeypatch.setattr(BrevoMailer, "send", lambda self, message: mailbox.send(message))
    runner = CliRunner()
    assert runner.invoke(app, ["user", "approve-request", "1"]).exit_code == 0
    hosted_delivery(client, 2, "unknown")
    assert runner.invoke(app, ["user", "retry-invitation-mail", "2"]).exit_code == 1
    assert runner.invoke(app, ["user", "reconcile-invitation-mail", "2", "--outcome", "not-sent"]).exit_code == 0
    runner.invoke(app, ["user", "revoke-google-invite", "--email", "friend@example.com"])
    mailbox.error = None
    result = runner.invoke(app, ["user", "retry-invitation-mail", "2"])
    assert result.exit_code == 0
    assert "canceled" in result.output
    assert len(mailbox.messages) == 1


def test_repeated_approval_cannot_report_revoked_access_as_approved(invitations, monkeypatch):
    from api.integrations.invitation_mail import BrevoMailer

    _, client, mailbox = invitations
    client.post("/api/invitation-requests", json={"email": "friend@example.test"})
    monkeypatch.setattr(BrevoMailer, "send", lambda self, message: mailbox.send(message))
    runner = CliRunner()
    assert runner.invoke(app, ["user", "approve-request", "1"]).exit_code == 0
    hosted_delivery(client, 2, "sent")
    assert runner.invoke(app, ["user", "revoke-google-invite", "--email", "friend@example.test"]).exit_code == 0
    result = runner.invoke(app, ["user", "approve-request", "1"])
    assert result.exit_code == 1
    assert "revoked" in result.output
    assert len(mailbox.messages) == 2


def test_concurrent_requests_and_restart_keep_one_notification(invitations):
    from api.repositories.invitations import InvitationRepository

    db, _, _ = invitations
    repo = InvitationRepository(db_path=db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: repo.request("friend@example.com", f"source-{n}"), range(4)))
    restarted = InvitationRepository(db_path=db)
    assert len(restarted.requests()) == 1
    pending = restarted.pending_mail()
    assert len(pending) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: restarted.claim(pending[0]), range(4)))
    assert sum(bool(claim) for claim in claims) == 1


def test_global_budget_applies_across_sources(invitations):
    from api.repositories.invitations import InvitationRepository, RequestLimited

    db, _, _ = invitations
    repo = InvitationRepository(db_path=db)
    for index in range(100):
        repo.request(f"friend{index}@example.test", f"source-{index}")
    with pytest.raises(RequestLimited):
        repo.request("next@example.test", "another-source")
    assert len(repo.requests()) == 100


def test_concurrent_approval_and_interrupted_send_recover_without_duplicate_mail(invitations):
    from api.repositories.invitations import InvitationRepository

    db, client, _ = invitations
    client.post("/api/invitation-requests", json={"email": "friend@example.test"})
    repo = InvitationRepository(db_path=db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: repo.approve(1), range(4)))
    assert len(set(ids)) == 1
    assert repo.claim(ids[0])
    with connect(db) as conn:
        conn.execute("UPDATE invitation_mail SET attempted_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ids[0],))
    assert repo.mail_status(ids[0]) == "unknown"
    with pytest.raises(ValueError, match="reconcile"):
        repo.claim(ids[0], retry=True)
    repo.reconcile(ids[0], "sent")
    assert repo.claim(ids[0], retry=True) is None


def test_saved_request_can_be_retried_after_lost_response(invitations):
    from api.repositories.invitations import InvitationRepository

    db, client, mailbox = invitations
    repo = InvitationRepository(db_path=db)
    repo.request("friend@example.test", "source-before-restart")
    # No delivery/HTTP response happened before process restart.
    response = client.post("/api/invitation-requests", json={"email": "friend@example.test"})
    assert response.status_code == 202
    assert len(repo.requests()) == 1
    assert len(mailbox.messages) == 1


def test_open_signup_pauses_existing_invitation_mail_without_deleting_work(tmp_path, monkeypatch):
    from api.repositories.invitations import InvitationRepository
    from api.tests.integration_tests.test_supabase_oauth_web import FakeSupabaseAuthGateway

    db = migrated_db(tmp_path)
    repo = InvitationRepository(db_path=db)
    mail_id = repo.request('friend@example.test', 'source-before-pause')
    mailbox = Mailbox()
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    with TestClient(create_app(db_path=db, session_secret='test-secret', invitation_mailer=mailbox,
                               supabase_auth_gateway=FakeSupabaseAuthGateway(), invitation_poll_seconds=.01)) as client:
        assert client.get('/healthz').status_code == 200
        assert client.post('/api/invitation-requests', json={'email': 'another@example.test'}).status_code == 409
    assert not mailbox.messages
    assert repo.mail_status(mail_id) == 'pending'


def test_stale_sender_cannot_overwrite_a_reconciled_retry(invitations):
    from api.repositories.invitations import InvitationRepository

    db, _, _ = invitations
    repo = InvitationRepository(db_path=db)
    mail_id = repo.request("friend@example.test", "source")
    first = repo.claim(mail_id)
    with connect(db) as conn:
        conn.execute("UPDATE invitation_mail SET attempted_at='2000-01-01T00:00:00+00:00' WHERE id=?", (mail_id,))
    assert repo.mail_status(mail_id) == "unknown"
    repo.reconcile(mail_id, "not-sent")
    second = repo.claim(mail_id, retry=True)
    repo.finish(mail_id, "failed", attempt_token=first["attempt_token"])
    repo.finish(mail_id, "sent", attempt_token=second["attempt_token"])
    assert repo.mail_status(mail_id) == "sent"


def test_failed_persistence_is_not_reported_as_success(invitations):
    db, client, mailbox = invitations
    with connect(db) as conn:
        conn.execute("DROP TABLE invitation_limits")
    assert client.post("/api/invitation-requests", json={"email": "friend@example.test"}).status_code == 503
    assert not mailbox.messages


def test_request_approval_invitation_and_verified_signin_complete_the_journey(invitations, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from api.integrations.invitation_mail import BrevoMailer
    from api.tests.fixtures.oauth_provider import OAuthProviderFixture

    db, _, mailbox = invitations
    gateway = OAuthProviderFixture(resource_url="https://events.example.com/mcp", email="newfriend@company.test")
    client = TestClient(create_app(db_path=db, session_secret="test-secret",
                                  invitation_mailer=mailbox, supabase_auth_gateway=gateway),
                        base_url="https://events.example.com")
    assert client.post("/api/invitation-requests", json={"email": "newfriend@company.test"}).status_code == 202
    assert mailbox.messages[0].recipient == "info@sotazoo.com"
    monkeypatch.setattr(BrevoMailer, "send", lambda self, message: mailbox.send(message))
    result = CliRunner().invoke(app, ["user", "approve-request", "1"])
    assert result.exit_code == 0, result.output
    assert len(mailbox.messages) == 1
    hosted_delivery(client, 2, "sent")
    assert mailbox.messages[1].recipient == "newfriend@company.test"
    start = client.get("/auth/supabase/start", follow_redirects=False)
    callback = parse_qs(urlsplit(start.headers["location"]).query)["callback"][0]
    state = parse_qs(urlsplit(callback).query)["state"][0]
    response = client.get(f"/auth/supabase/callback?code=synthetic-auth-code&state={state}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert client.get("/api/discovery").status_code == 200
    assert 'data-public-preview="false"' in client.get("/events").text
    listed = CliRunner().invoke(app, ["user", "requests"])
    assert "accepted" in listed.output and "sent" in listed.output
