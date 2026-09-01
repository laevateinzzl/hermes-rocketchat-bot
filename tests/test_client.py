"""Tests for Rocket.Chat REST client (authentication, send, download)."""

import pytest

from adapter import (
    RocketChatClient,
    RocketChatClientError,
    RocketChatRateLimitError,
)


# ---------------------------------------------------------------------------
# Fake response / session helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """A minimal fake HTTP response for testing."""

    def __init__(self, *, status=200, json_data=None, text_data="", headers=None):
        self.status = status
        self._json_data = json_data
        self._text = text_data
        self.headers = headers or {}

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

    async def _get_session():
        return session

    client._get_session = _get_session  # type: ignore[method-assign]

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
                json_data={
                    "success": True,
                    "_id": "bot-user-id",
                    "username": "hermesbot",
                }
            ),
            FakeResponse(json_data={"success": True, "message": {"_id": "msg-456"}}),
        ]
    )
    client = FakeClient(session)
    await client.initialize()

    await client.post_message(room_id="room-abc", text="hi")

    req = session.requests[1]
    assert "tmid" not in req["json"]


# ---------------------------------------------------------------------------
# Rate limit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_response_raises_retry_after_error():
    """Rocket.Chat 429 errors should expose retry-after seconds for polling."""
    session = FakeSession(
        responses=[
            FakeResponse(
                status=429,
                json_data={
                    "success": False,
                    "error": "Error, too many requests. You must wait 27 seconds before trying again.",
                },
                text_data="Error, too many requests. You must wait 27 seconds before trying again.",
            )
        ]
    )
    client = FakeClient(session)

    with pytest.raises(RocketChatRateLimitError) as exc_info:
        await client.list_subscriptions()

    assert exc_info.value.retry_after == 27


# ---------------------------------------------------------------------------
# Message sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_messages_falls_back_to_room_history_when_sync_endpoint_missing():
    """Older Rocket.Chat servers can lack chat.syncMessages but support history."""
    session = FakeSession(
        responses=[
            FakeResponse(status=404, text_data="404 Not Found"),
            FakeResponse(
                json_data={
                    "success": True,
                    "messages": [
                        {"_id": "new-msg", "msg": "newer"},
                        {"_id": "old-msg", "msg": "older"},
                    ],
                }
            ),
        ]
    )
    client = FakeClient(session)

    data = await client.sync_messages(
        "room-abc",
        last_update="2026-07-01T01:30:00.000Z",
        room_type="d",
    )

    assert [msg["_id"] for msg in data["updated"]] == ["old-msg", "new-msg"]
    assert data["removed"] == []
    sync_req = session.requests[0]
    assert sync_req["method"] == "POST"
    assert "/api/v1/chat.syncMessages" in sync_req["url"]
    history_req = session.requests[1]
    assert history_req["method"] == "GET"
    assert "/api/v1/im.history" in history_req["url"]
    assert history_req["params"] == {
        "roomId": "room-abc",
        "count": 100,
        "oldest": "2026-07-01T01:30:00.000Z",
        "inclusive": "false",
    }


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

    data = await client.download_attachment(
        "https://chat.example.com/file-upload/abc/photo.jpg"
    )

    assert data == b"fake-binary-data"
    req = session.requests[1]
    assert req["method"] == "GET"
    assert "file-upload/abc/photo.jpg" in req["url"]
    assert req["headers"]["X-User-Id"] == "test-bot-id"
    assert req["headers"]["X-Auth-Token"] == "test-access-token"


# ---------------------------------------------------------------------------
# Multipart upload (0.3.0-A: real file bytes, not JSON metadata)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_attachment_sends_real_multipart_file():
    """rooms.media must receive the actual file bytes as multipart form data."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(b"fake-image-bytes")
        tmp_path = tmp.name

    try:
        session = FakeSession(
            responses=[
                # rooms.media
                FakeResponse(
                    json_data={"success": True, "file": {"_id": "f1", "name": "x.png"}}
                ),
                # rooms.mediaConfirm
                FakeResponse(json_data={"success": True}),
                # chat.postMessage
                FakeResponse(json_data={"success": True, "message": {"_id": "m1"}}),
            ]
        )
        client = FakeClient(session)

        result = await client.upload_attachment(
            room_id="room-1", file_path=tmp_path, text="caption", tmid="t-1"
        )

        assert result["_id"] == "m1"
        media_call = session.requests[0]
        assert "rooms.media" in media_call["url"]
        # A real multipart payload (httpx-style files= for non-aiohttp fake
        # sessions), NOT the old JSON metadata-only body.
        assert "json" not in media_call
        assert media_call["files"]["file"][0].endswith(".png")
        assert media_call["files"]["file"][1] == b"fake-image-bytes"
        assert media_call["files"]["file"][2] == "image/png"
        # Credentials ride the default headers on every step.
        assert media_call["headers"]["X-Auth-Token"] == "test-access-token"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# download_attachment hardening (SSRF / credential hygiene)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_attachment_joins_relative_url():
    """Relative /file-upload/... URLs are joined onto the server origin."""
    session = FakeSession(responses=[FakeResponse(status=200, text_data=b"img")])
    client = FakeClient(session)

    data = await client.download_attachment("/file-upload/room-1/f1/notes.txt")

    assert data == b"img"
    req = session.requests[0]
    assert req["url"] == "https://chat.example.com/file-upload/room-1/f1/notes.txt"
    # Same-origin: bot credentials are attached.
    assert req["headers"]["X-User-Id"] == "test-bot-id"
    assert req["headers"]["X-Auth-Token"] == "test-access-token"


@pytest.mark.asyncio
async def test_download_attachment_cross_origin_drops_credentials(monkeypatch):
    """Cross-origin https targets are fetched WITHOUT bot credentials."""

    async def public(host):
        return True

    monkeypatch.setattr("adapter._host_is_public", public)
    session = FakeSession(responses=[FakeResponse(status=200, text_data=b"x")])
    client = FakeClient(session)

    data = await client.download_attachment("https://cdn.example.net/f.png")

    assert data == b"x"
    req = session.requests[0]
    assert "X-User-Id" not in req["headers"]
    assert "X-Auth-Token" not in req["headers"]


@pytest.mark.asyncio
async def test_download_attachment_rejects_cross_origin_http():
    """Cross-origin plain-http targets are refused (no credential leak)."""
    session = FakeSession()
    client = FakeClient(session)

    with pytest.raises(RocketChatClientError, match="require https"):
        await client.download_attachment("http://other.example.com/f.png")


@pytest.mark.asyncio
async def test_download_attachment_blocks_private_host(monkeypatch):
    """Private/loopback cross-origin hosts are refused (SSRF guard)."""

    async def private(host):
        return False

    monkeypatch.setattr("adapter._host_is_public", private)
    session = FakeSession()
    client = FakeClient(session)

    with pytest.raises(RocketChatClientError, match="private"):
        await client.download_attachment("https://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_download_attachment_rejects_non_http_scheme():
    session = FakeSession()
    client = FakeClient(session)

    with pytest.raises(RocketChatClientError, match="Unsupported attachment URL"):
        await client.download_attachment("ftp://example.com/f.png")


@pytest.mark.asyncio
async def test_download_attachment_redirect_rechecked_and_unauthenticated(
    monkeypatch,
):
    """Redirect hops are re-validated; cross-origin hops lose credentials."""

    async def public(host):
        return True

    monkeypatch.setattr("adapter._host_is_public", public)
    session = FakeSession(
        responses=[
            FakeResponse(status=302, headers={"Location": "https://cdn.example.net/f"}),
            FakeResponse(status=200, text_data=b"final"),
        ]
    )
    client = FakeClient(session)

    data = await client.download_attachment(
        "https://chat.example.com/file-upload/r/f/a.png"
    )

    assert data == b"final"
    assert len(session.requests) == 2
    # First hop: same-origin -> authenticated.
    assert session.requests[0]["headers"]["X-Auth-Token"] == "test-access-token"
    # Second hop: cross-origin -> no credentials, absolute https target.
    assert "X-Auth-Token" not in session.requests[1]["headers"]
    assert session.requests[1]["url"] == "https://cdn.example.net/f"


@pytest.mark.asyncio
async def test_download_attachment_enforces_size_cap():
    session = FakeSession(responses=[FakeResponse(status=200, text_data=b"123456")])
    client = FakeClient(session)
    client._max_download_bytes = 4

    with pytest.raises(RocketChatClientError, match="size limit"):
        await client.download_attachment("https://chat.example.com/big.bin")


# ---------------------------------------------------------------------------
# 0.3.0-B: subscription delta + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_subscriptions_single_call_with_since():
    """updatedSince must reach the server; count/offset must NOT be sent.

    subscriptions.get returns the full set in one response and its schema
    rejects pagination params (HTTP 400 "must NOT have additional
    properties"), which would spin the transports into a reconnect loop.
    """
    session = FakeSession(
        responses=[
            FakeResponse(
                json_data={"update": [{"rid": f"r{i}", "t": "c"} for i in range(150)]}
            ),
        ]
    )
    client = FakeClient(session)

    subs = await client.list_subscriptions(updated_since="2024-01-01T00:00:00.000Z")

    assert len(subs) == 150
    first = session.requests[0]
    assert first["params"]["updatedSince"] == "2024-01-01T00:00:00.000Z"
    assert "count" not in first["params"]
    assert "offset" not in first["params"]
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_list_subscriptions_single_page_without_since():
    session = FakeSession(
        responses=[
            FakeResponse(json_data={"subscriptions": [{"rid": "r1", "t": "c"}]}),
        ]
    )
    client = FakeClient(session)

    subs = await client.list_subscriptions()

    assert len(subs) == 1
    assert "updatedSince" not in session.requests[0]["params"]


# ---------------------------------------------------------------------------
# 0.3.0-C: auth strictness, JSON wrapping, sync fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_token_rejects_success_false_with_id():
    """success:false must fail even when an _id is present."""
    session = FakeSession(
        responses=[
            FakeResponse(json_data={"success": False, "_id": "bot-user-id"}),
        ]
    )
    client = FakeClient(session)

    with pytest.raises(RocketChatClientError, match="invalid response"):
        await client.initialize()


@pytest.mark.asyncio
async def test_password_login_sends_no_auth_headers():
    session = FakeSession(
        responses=[
            FakeResponse(
                json_data={
                    "status": "success",
                    "data": {"userId": "u1", "authToken": "t1"},
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

    await client.initialize()

    req = session.requests[0]
    assert "X-Auth-Token" not in req["headers"]
    assert "X-User-Id" not in req["headers"]


@pytest.mark.asyncio
async def test_request_wraps_invalid_json_body():
    """A 2xx with a non-JSON body must surface as RocketChatClientError."""
    session = FakeSession(
        responses=[
            FakeResponse(status=200, text_data="<html>not json</html>"),
        ]
    )
    client = FakeClient(session)

    with pytest.raises(RocketChatClientError, match="invalid JSON"):
        await client._request("GET", "/api/v1/me")


@pytest.mark.asyncio
async def test_sync_messages_falls_back_on_http_400():
    """Older servers answer chat.syncMessages with 400 — fall back to history."""
    session = FakeSession(
        responses=[
            FakeResponse(status=400, json_data={"success": False, "error": "no"}),
            FakeResponse(
                status=200,
                json_data={"messages": [{"_id": "m1", "msg": "hi"}], "removed": []},
            ),
        ]
    )
    client = FakeClient(session)

    data = await client.sync_messages("room-1", last_update="ts", room_type="c")

    assert data["updated"][0]["_id"] == "m1"
    assert "channels.history" in session.requests[1]["url"]


@pytest.mark.asyncio
async def test_sync_messages_400_without_room_type_reraises():
    session = FakeSession(
        responses=[FakeResponse(status=400, json_data={"success": False})]
    )
    client = FakeClient(session)

    with pytest.raises(RocketChatClientError):
        await client.sync_messages("room-1", last_update="ts", room_type="")


@pytest.mark.asyncio
async def test_history_messages_paginates_full_pages():
    """Fast rooms: pages beyond the first 100 are fetched when oldest is set."""
    page_a = {"messages": [{"_id": f"m{i}"} for i in range(100)], "removed": []}
    page_b = {"messages": [{"_id": "m-extra"}], "removed": []}
    session = FakeSession(
        responses=[FakeResponse(json_data=page_a), FakeResponse(json_data=page_b)]
    )
    client = FakeClient(session)

    data = await client.history_messages("room-1", "d", oldest="ts")

    assert len(data["updated"]) == 101
    assert session.requests[1]["params"]["offset"] == 100
