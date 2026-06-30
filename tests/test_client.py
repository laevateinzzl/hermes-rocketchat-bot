"""Tests for Rocket.Chat REST client (authentication, send, download)."""

import pytest

from adapter import RocketChatClient, RocketChatClientError, RocketChatIdentity


# ---------------------------------------------------------------------------
# Fake response / session helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """A minimal fake HTTP response for testing."""

    def __init__(self, *, status=200, json_data=None, text_data=""):
        self.status = status
        self._json_data = json_data
        self._text = text_data

    async def json(self):
        if self._json_data is None:
            raise ValueError("no json data")
        return self._json_data

    async def text(self):
        return self._text

    async def read(self):
        """Return raw bytes (for aiohttp response compatibility)."""
        if isinstance(self._text, bytes):
            return self._text
        return self._text.encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    """Records HTTP calls so tests can assert request shape."""

    def __init__(self, responses=None):
        self.requests: list[dict] = []
        self._responses = list(responses or [])
        self._closed = False

    def add_response(self, resp):
        self._responses.append(resp)

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        resp = self._responses.pop(0) if self._responses else FakeResponse()
        return resp

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self._closed = True


class FakeClient(RocketChatClient):
    """A RocketChatClient wired up to a FakeSession for test inspection."""

    def __init__(self, session):
        super().__init__(
            server_url="https://chat.example.com",
            user_id="test-bot-id",
            access_token="test-access-token",
        )
        self._session = session

    async def _get_session(self):
        return self._session


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_auth_calls_me_endpoint():
    session = FakeSession(
        responses=[
            FakeResponse(
                json_data={
                    "success": True,
                    "_id": "bot-user-id",
                    "username": "hermesbot",
                    "name": "Hermes Bot",
                }
            )
        ]
    )
    client = FakeClient(session)

    identity = await client.initialize()

    assert identity.user_id == "bot-user-id"
    assert identity.username == "hermesbot"
    req = session.requests[0]
    assert req["method"] == "GET"
    assert "/api/v1/me" in req["url"]
    assert req["headers"]["X-User-Id"] == "test-bot-id"
    assert req["headers"]["X-Auth-Token"] == "test-access-token"


@pytest.mark.asyncio
async def test_password_auth_calls_login_endpoint():
    session = FakeSession(
        responses=[
            FakeResponse(
                json_data={
                    "status": "success",
                    "data": {"userId": "bot-user-id", "authToken": "returned-token"},
                }
            )
        ]
    )
    client = RocketChatClient(
        server_url="https://chat.example.com",
        username="hermesbot",
        password="secret",
    )
    client._session = session
    # override _get_session to return our fake
    async def _get_session():
        return session
    client._get_session = _get_session  # type: ignore[method-assign]

    identity = await client.initialize()

    assert identity.user_id == "bot-user-id"
    assert identity.auth_token == "returned-token"
    assert client._access_token == "returned-token"
    assert client._user_id == "bot-user-id"
    req = session.requests[0]
    assert req["method"] == "POST"
    assert "/api/v1/login" in req["url"]


@pytest.mark.asyncio
async def test_auth_failure_raises_error():
    session = FakeSession(
        responses=[
            FakeResponse(
                status=401,
                json_data={"status": "error", "message": "Unauthorized"},
            )
        ]
    )
    # also need a 401 for the _get_session attempt
    client = RocketChatClient(
        server_url="https://chat.example.com",
        user_id="bad-id",
        access_token="bad-token",
    )
    client._session = session
    client._get_session = lambda: session  # type: ignore[method-assign]

    with pytest.raises(RocketChatClientError):
        await client.initialize()


# ---------------------------------------------------------------------------
# Send tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_calls_chat_post_message():
    session = FakeSession(
        responses=[
            # initialize() response
            FakeResponse(
                json_data={
                    "success": True,
                    "_id": "bot-user-id",
                    "username": "hermesbot",
                }
            ),
            # chat.postMessage response
            FakeResponse(
                json_data={
                    "success": True,
                    "message": {"_id": "msg-123"},
                }
            ),
        ]
    )
    client = FakeClient(session)
    await client.initialize()

    result = await client.post_message(
        room_id="room-abc",
        text="hello world",
        tmid="thread-1",
    )

    assert result["_id"] == "msg-123"
    req = session.requests[1]
    assert req["method"] == "POST"
    assert "/api/v1/chat.postMessage" in req["url"]
    assert req["json"]["roomId"] == "room-abc"
    assert req["json"]["text"] == "hello world"
    assert req["json"]["tmid"] == "thread-1"


@pytest.mark.asyncio
async def test_post_message_without_tmid():
    session = FakeSession(
        responses=[
            FakeResponse(
                json_data={"success": True, "_id": "bot-user-id", "username": "hermesbot"}
            ),
            FakeResponse(
                json_data={"success": True, "message": {"_id": "msg-456"}}
            ),
        ]
    )
    client = FakeClient(session)
    await client.initialize()

    result = await client.post_message(room_id="room-abc", text="hi")

    req = session.requests[1]
    assert "tmid" not in req["json"]


# ---------------------------------------------------------------------------
# Download tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_attachment_uses_auth_headers():
    session = FakeSession(
        responses=[
            # initialize()
            FakeResponse(
                json_data={
                    "success": True,
                    "_id": "bot-user-id",
                    "username": "hermesbot",
                }
            ),
            # download response
            FakeResponse(status=200, text_data="fake-binary-data"),
        ]
    )
    client = FakeClient(session)
    await client.initialize()

    data = await client.download_attachment("https://chat.example.com/file-upload/abc/photo.jpg")

    assert data == b"fake-binary-data"
    req = session.requests[1]
    assert req["method"] == "GET"
    assert "file-upload/abc/photo.jpg" in req["url"]
    assert req["headers"]["X-User-Id"] == "test-bot-id"
    assert req["headers"]["X-Auth-Token"] == "test-access-token"
