"""Approval on an operator machine, delivery in the hosted app only."""

import time
from threading import Event, Timer

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from api.app import create_app
from api.commands.main import app
from api.repositories.invitations import InvitationRepository
from api.tests.fixtures.web_helpers import migrated_db


def test_approval_without_local_key_queues_and_wakes_only_the_host(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    repo = InvitationRepository(db_path=db)
    repo.request("friend@example.test", "synthetic-source")
    requests = []

    def host(request):
        requests.append(request)
        assert str(request.url) == "https://events.example.test/healthz"
        assert request.method == "GET"
        assert not request.content and "api-key" not in request.headers
        return httpx.Response(200)

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(host), **kwargs))
    result = CliRunner().invoke(app, ["user", "approve-request", "1"])

    assert result.exit_code == 0, result.output
    assert "approved" in result.output and "pending" in result.output
    assert len(requests) == 1
    assert repo.requests()[0]["invitation_status"] == "pending"


def test_hosted_app_delivers_existing_and_new_work_without_duplicates(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    repo = InvitationRepository(db_path=db)
    notification = repo.request("friend@example.test", "synthetic-source")
    messages = []

    class Provider:
        def send(self, message):
            messages.append(message)
            return "<synthetic-receipt>"

    def wait_for_sent(mail_id):
        deadline = time.monotonic() + 3
        while repo.mail_status(mail_id) != "sent" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert repo.mail_status(mail_id) == "sent"

    def hosted_app():
        return create_app(db_path=db, session_secret="synthetic-secret",
                          invitation_mailer=Provider(), invitation_poll_seconds=0.01)

    with TestClient(hosted_app()) as first, TestClient(hosted_app()):
        assert first.get("/healthz").status_code == 200
        wait_for_sent(notification)
        invitation = repo.approve(1)
        wait_for_sent(invitation)
    with TestClient(hosted_app()):
        assert repo.approve(1) == invitation
    assert [message.recipient for message in messages] == ["info@sotazoo.com", "friend@example.test"]
    assert repo.requests()[0]["invitation_provider_id"] == "<synthetic-receipt>"


def test_explicit_retry_queues_failed_mail_but_never_uncertain_mail(tmp_path):
    repo = InvitationRepository(db_path=migrated_db(tmp_path))
    mail_id = repo.request("friend@example.test", "synthetic-source")
    claim = repo.claim(mail_id)
    repo.finish(mail_id, "failed", "provider_rejected", attempt_token=claim["attempt_token"])
    assert repo.retry(mail_id) == "pending"
    claim = repo.claim(mail_id)
    repo.finish(mail_id, "unknown", "connection_lost", attempt_token=claim["attempt_token"])
    with pytest.raises(ValueError, match="reconcile"):
        repo.retry(mail_id)
    assert repo.mail_status(mail_id) == "unknown"


@pytest.mark.parametrize("outcome", ["timeout", "redirect", "unavailable", "invalid_host", "invalid_port"])
def test_wake_failure_preserves_approval_and_reports_pending(tmp_path, monkeypatch, httpx_mock, outcome):
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    malformed = {"invalid_host": "https://[", "invalid_port": "https://events.example.test:invalid"}
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", malformed.get(outcome, "https://events.example.test"))
    repo = InvitationRepository(db_path=db)
    repo.request("friend@example.test", "source")
    if outcome == "timeout":
        httpx_mock.add_exception(httpx.ReadTimeout("private transport details"))
    elif outcome not in malformed:
        httpx_mock.add_response(status_code=302 if outcome == "redirect" else 503,
                                headers={"Location": "https://other.example.test"})
    result = CliRunner().invoke(app, ["user", "approve-request", "1"])
    assert result.exit_code == 0, result.output
    assert "Hosted sender unavailable" in result.output
    assert "approved" in result.output and "pending" in result.output
    assert "private transport details" not in result.output
    assert repo.requests()[0]["approved_at"]
    assert repo.requests()[0]["invitation_status"] == "pending"
    assert len(httpx_mock.get_requests()) == (0 if outcome in malformed else 1)


@pytest.mark.parametrize("url", ["", "http://events.example.test", "https://user:placeholder@events.example.test",
                                  "https://events.example.test?token=secret", "https://events.example.test/other"])
def test_invalid_wake_address_never_receives_operator_data(url, httpx_mock):
    from api.integrations.hosted_delivery import wake_hosted_sender

    assert not wake_hosted_sender(url)
    assert not httpx_mock.get_requests()


def test_operator_retry_and_drain_do_not_send_locally(tmp_path, monkeypatch, httpx_mock):
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    repo = InvitationRepository(db_path=db)
    mail_id = repo.request("friend@example.test", "source")
    claim = repo.claim(mail_id)
    repo.finish(mail_id, "failed", attempt_token=claim["attempt_token"])
    httpx_mock.add_response(method="GET", url="https://events.example.test/healthz", is_reusable=True)
    runner = CliRunner()
    for command in (["retry-invitation-mail", "1"], ["send-pending-invitations"]):
        result = runner.invoke(app, ["user", *command])
        assert result.exit_code == 0, result.output
        assert "pending" in result.output
    assert repo.mail_status(mail_id) == "pending"
    assert len(httpx_mock.get_requests()) == 2


def test_shutdown_finishes_the_inflight_provider_receipt(tmp_path):
    db = migrated_db(tmp_path)
    repo = InvitationRepository(db_path=db)
    mail_id = repo.request("friend@example.test", "source")
    started, release = Event(), Event()

    class Provider:
        def send(self, message):
            started.set()
            assert release.wait(timeout=3)
            return "<shutdown-receipt>"

    with TestClient(create_app(db_path=db, session_secret="synthetic", invitation_mailer=Provider())):
        assert started.wait(timeout=3)
        timer = Timer(0.05, release.set)
        timer.start()
    timer.join()
    assert repo.mail_status(mail_id) == "sent"
    assert repo.requests()[0]["notification_provider_id"] == "<shutdown-receipt>"


def test_unconfigured_app_keeps_pending_work_for_configured_production(tmp_path, httpx_mock):
    db = migrated_db(tmp_path)
    repo = InvitationRepository(db_path=db)
    mail_id = repo.request("friend@example.test", "source")
    with TestClient(create_app(db_path=db, session_secret="synthetic")) as client:
        assert client.get("/healthz").status_code == 200
    assert repo.mail_status(mail_id) == "pending"
    assert not httpx_mock.get_requests()


def test_worker_continues_after_provider_failure_without_logging_private_data(tmp_path, caplog):
    db = migrated_db(tmp_path)
    repo = InvitationRepository(db_path=db)
    first = repo.request("first@example.test", "first")
    second = repo.request("second@example.test", "second")
    delivered = Event()

    class Provider:
        def send(self, message):
            if "first@example.test" in message.body:
                raise RuntimeError("private first@example.test provider details")
            delivered.set()
            return "<later-receipt>"

    with TestClient(create_app(db_path=db, session_secret="synthetic", invitation_mailer=Provider(),
                              invitation_poll_seconds=0.01)):
        assert delivered.wait(timeout=3)
    assert repo.mail_status(first) == "sending"  # Recovered as unknown after the existing timeout.
    assert repo.mail_status(second) == "sent"
    assert "RuntimeError" in caplog.text
    assert "first@example.test" not in caplog.text
    assert "provider details" not in caplog.text
