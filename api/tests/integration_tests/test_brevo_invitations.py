"""Public request and operator recovery with only the provider HTTP boundary faked."""
import json
import time

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from api.app import create_app
from api.commands.main import app
from api.tests.fixtures.web_helpers import migrated_db


@pytest.mark.parametrize('outcome, expected', [
    ('accepted', 'sent'), ('quota', 'failed'), ('unauthorized', 'failed'), ('forbidden', 'failed'),
    ('timeout', 'unknown'), ('malformed', 'unknown'), ('invalid-json', 'unknown'),
    ('null-json', 'unknown'), ('array-json', 'unknown'), ('numeric-id', 'unknown'),
    ('blank-id', 'unknown'), ('control-id', 'unknown'), ('oversized-id', 'unknown'),
    ('redirect', 'unknown'), ('server', 'unknown'),
])
def test_provider_outcomes_are_durable_visible_and_safe_to_retry(tmp_path, monkeypatch, outcome, expected):
    db = migrated_db(tmp_path)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    monkeypatch.setenv('EVENT_HUNT_BREVO_API_KEY', 'synthetic-key')
    monkeypatch.setenv('NYC_AI_EVENTS_PUBLIC_BASE_URL', 'https://events.example.test')
    sent = []

    def provider(request):
        if str(request.url) == 'https://events.example.test/healthz':
            assert request.method == 'GET'
            return httpx.Response(200)
        sent.append(request)
        assert request.url == 'https://api.brevo.com/v3/smtp/email'
        assert request.headers['api-key'] == 'synthetic-key'
        payload = json.loads(request.content)
        assert payload['sender'] == {'name': 'Event Hunt', 'email': 'info@sotazoo.com'}
        assert payload['replyTo']['email'] == 'info@sotazoo.com'
        assert payload['textContent'] and payload['htmlContent']
        if outcome == 'timeout':
            raise httpx.ReadTimeout('private provider details', request=request)
        if outcome == 'quota':
            return httpx.Response(429, json={'message': 'private provider details'})
        if outcome in {'unauthorized', 'forbidden'}:
            return httpx.Response(401 if outcome == 'unauthorized' else 403)
        if outcome == 'server':
            return httpx.Response(500)
        if outcome == 'redirect':
            return httpx.Response(307, headers={'Location': 'https://different-provider.example.test/send'})
        if outcome == 'invalid-json':
            return httpx.Response(201, content=b'not json')
        if outcome == 'null-json':
            return httpx.Response(201, content=b'null')
        if outcome == 'array-json':
            return httpx.Response(201, json=[])
        invalid_ids = {'numeric-id': 1, 'blank-id': ' ', 'control-id': '<id>\n', 'oversized-id': 'x' * 1025}
        if outcome in invalid_ids:
            return httpx.Response(201, json={'messageId': invalid_ids[outcome]})
        return httpx.Response(201, json={'messageId': '<provider-id>'} if outcome == 'accepted' else {})

    original_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    client = TestClient(create_app(db_path=db, session_secret='synthetic-secret'))
    assert client.post('/api/invitation-requests', json={'email': 'friend@example.test'}).status_code == 202
    runner = CliRunner()
    listed = runner.invoke(app, ['user', 'requests'])
    assert listed.exit_code == 0, listed.output
    assert f'notification:1:{expected}' in listed.output
    assert ('<provider-id>' in listed.output) is (outcome == 'accepted')
    assert 'private provider details' not in listed.output
    retried = runner.invoke(app, ['user', 'retry-invitation-mail', '1'])
    assert retried.exit_code == (1 if expected == 'unknown' else 0)
    assert len(sent) == 1  # The operator never calls the provider, even on retry.
    with client:
        deadline = time.monotonic() + 3
        while (client.app.state.invitations.repository.mail_status(1) in {'pending', 'sending'}
               and time.monotonic() < deadline):
            time.sleep(0.01)
        assert client.app.state.invitations.repository.mail_status(1) == expected
    assert len(sent) == (2 if expected == 'failed' else 1)
