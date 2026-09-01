"""Tests for fatal-error reporting on persistent auth failure (v0.2 P1.2).

Mirrors Hermes 2ab153218 / 54a0f0710: an adapter that cannot authenticate
must report a non-retryable fatal error so the gateway exits instead of
reconnecting forever.  Transient network errors stay retryable.
"""

import pytest  # type: ignore[reportMissingImports]

from adapter import (
    RocketChatAdapter,
    RocketChatClientError,
    RocketChatConfig,
    WebSocketTransport,
)


class AuthFailingClient:
    """Client whose initialize() fails with a 401-style auth error."""

    async def initialize(self):
        raise RocketChatClientError(
            "Token verification failed: GET /api/v1/me failed: HTTP 401: Unauthorized"
        )

    @property
    def identity(self):
        return None

    @property
    def server_url(self):
        return "https://chat.example.com"


class NetworkFailingClient:
    """Client whose initialize() fails with a transient network error."""

    async def initialize(self):
        raise RocketChatClientError("Token verification failed: connection timeout")

    @property
    def identity(self):
        return None

    @property
    def server_url(self):
        return "https://chat.example.com"


def _adapter(client, monkeypatch):
    adapter = RocketChatAdapter(
        RocketChatConfig(
            server_url="https://chat.example.com",
            auth_mode="token",
            user_id="u1",
            access_token="tok",
        )
    )
    # connect() rebuilds self._client from the module-level RocketChatClient;
    # patch it so our fake is used (failure paths return before transports).
    monkeypatch.setattr("adapter.RocketChatClient", lambda *a, **k: client)
    return adapter


@pytest.mark.asyncio
async def test_connect_auth_401_marks_fatal_nonretryable(monkeypatch):
    adapter = _adapter(AuthFailingClient(), monkeypatch)

    result = await adapter.connect()

    assert not result
    assert adapter.has_fatal_error
    assert adapter.fatal_error_code == "AUTH_FAILED"
    assert not adapter.fatal_error_retryable
    assert "HTTP 401" in adapter.fatal_error_message


@pytest.mark.asyncio
async def test_connect_network_error_not_fatal(monkeypatch):
    """Transient failures must NOT mark the platform fatal (reconnect may help)."""
    adapter = _adapter(NetworkFailingClient(), monkeypatch)

    result = await adapter.connect()

    assert not result
    assert not adapter.has_fatal_error


@pytest.mark.asyncio
async def test_mark_auth_fatal_noop_when_base_unsupported(monkeypatch):
    """Calling the fatal setter must not crash when the base lacks support."""
    adapter = _adapter(AuthFailingClient(), monkeypatch)
    # Simulate a base without fatal-error support (older Hermes).
    adapter._set_fatal_error = None  # type: ignore[assignment]

    result = await adapter.connect()

    assert not result  # still fails closed, just no fatal metadata
    assert not adapter.has_fatal_error


@pytest.mark.asyncio
async def test_ws_reauthenticate_failure_invokes_callback():
    """Repeated re-auth failure must surface to the adapter via the callback."""
    failures = []

    def on_auth_failure(message):
        failures.append(message)

    client = AuthFailingClient()
    transport = WebSocketTransport(client, on_auth_failure=on_auth_failure)

    await transport._reauthenticate()

    assert len(failures) == 1
    assert "HTTP 401" in failures[0]


@pytest.mark.asyncio
async def test_ws_reauthenticate_success_skips_callback():
    failures = []

    def on_auth_failure(message):
        failures.append(message)

    class GoodClient:
        async def initialize(self):
            return None

        @property
        def server_url(self):
            return "https://chat.example.com"

    transport = WebSocketTransport(GoodClient(), on_auth_failure=on_auth_failure)

    await transport._reauthenticate()

    assert failures == []
