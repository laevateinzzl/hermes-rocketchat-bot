"""Tests for outbound file upload and standalone sender."""

import pytest
import tempfile
from pathlib import Path

from adapter import (
    RocketChatClient,
    RocketChatClientError,
    resolve_delivery_target,
    standalone_send,
)


# ---------------------------------------------------------------------------
# Fake session and client for upload tests
# ---------------------------------------------------------------------------


class FakeUploadResponse:
    """Fake HTTP response for file upload calls."""

    def __init__(self, *, status=200, json_data=None):
        self.status = status
        self._json = json_data

    async def json(self):
        return self._json

    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeUploadSession:
    """Records calls for upload tests."""

    def __init__(self, responses=None):
        self.requests: list[dict] = []
        self._responses = list(responses or [])

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        resp = self._responses.pop(0) if self._responses else FakeUploadResponse()
        return resp

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeUploadClient(RocketChatClient):
    """RocketChatClient wired to a FakeUploadSession."""

    def __init__(
        self,
        session,
        server_url="https://chat.example.com",
        user_id="bot1",
        access_token="tok",
    ):
        super().__init__(
            server_url=server_url, user_id=user_id, access_token=access_token
        )
        self._session = session

    async def _get_session(self):
        return self._session


# ---------------------------------------------------------------------------
# Media upload tests (on RocketChatClient)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_attachment_calls_rooms_media_and_confirm():
    """upload_attachment should call rooms.media then rooms.mediaConfirm."""
    # Create a real temp file so the existence check passes
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(b"fake-image-data")
        tmp_path = tmp.name

    try:
        session = FakeUploadSession(
            responses=[
                # rooms.media response
                FakeUploadResponse(
                    status=200,
                    json_data={
                        "success": True,
                        "file": {
                            "_id": "file-1",
                            "name": "test.png",
                            "type": "image/png",
                        },
                    },
                ),
                # rooms.mediaConfirm response
                FakeUploadResponse(
                    status=200,
                    json_data={"success": True},
                ),
                # chat.postMessage response
                FakeUploadResponse(
                    status=200,
                    json_data={"success": True, "message": {"_id": "msg-upload-1"}},
                ),
            ]
        )
        client = FakeUploadClient(session)

        result = await client.upload_attachment(
            room_id="room-1",
            file_path=tmp_path,
            text="here is an image",
            tmid="thread-1",
        )

        assert result["_id"] == "msg-upload-1"

        # Verify rooms.media was called
        media_call = session.requests[0]
        assert "rooms.media" in media_call["url"]
        # Real multipart payload (httpx-style files= on fake sessions) —
        # the old JSON-metadata-only upload would carry no files= key.
        assert "json" not in media_call
        assert "files" in media_call or "data" in media_call

        # Verify rooms.mediaConfirm was called
        confirm_call = session.requests[1]
        assert "rooms.mediaConfirm" in confirm_call["url"]

        # Verify chat.postMessage was called with file info
        post_call = session.requests[2]
        assert "chat.postMessage" in post_call["url"]
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_standalone_send_text_only():
    """standalone_send must fail cleanly without network access."""
    await _assert_clean_failure(message="test message")


@pytest.mark.asyncio
async def test_standalone_send_media_files_is_callable():
    """standalone_send must accept media_files (contract parity)."""
    await _assert_clean_failure(message="here is an image", media_files=[])


async def _assert_clean_failure(message: str, media_files=None):
    """Run standalone_send with a client that cannot authenticate and assert
    the failure contract — no real network, no unclosed sessions."""

    class FailingAuthClient(RocketChatClient):
        def __init__(self):
            pass

        async def initialize(self):
            raise RocketChatClientError("HTTP 401: Unauthorized")

        async def _get_session(self):
            raise AssertionError("network access attempted")

    result = await standalone_send(
        pconfig={
            "server_url": "https://chat.example.com",
            "auth_mode": "token",
            "user_id": "bot1",
            "access_token": "tok",
        },
        chat_id="room-1",
        message=message,
        media_files=media_files,
        _client_factory=FailingAuthClient,
    )

    assert isinstance(result, dict)
    assert result.get("success") is False
    assert "401" in result.get("error", "")


@pytest.mark.asyncio
async def test_standalone_send_accepts_thread_and_force_document():
    """Hermes calls the sender with thread_id=/force_document= (contract parity)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(b"fake-image-data")
        tmp_path = tmp.name

    try:
        session = FakeUploadSession(
            responses=[
                # client.initialize -> /api/v1/me
                FakeUploadResponse(
                    status=200,
                    json_data={"success": True, "_id": "bot1", "username": "hermesbot"},
                ),
                # rooms.info -> room found
                FakeUploadResponse(
                    status=200,
                    json_data={"success": True, "room": {"_id": "room-thread-1"}},
                ),
                # rooms.media
                FakeUploadResponse(
                    status=200,
                    json_data={"success": True, "file": {"_id": "f1", "name": "x.png"}},
                ),
                # rooms.mediaConfirm
                FakeUploadResponse(status=200, json_data={"success": True}),
                # chat.postMessage (file ref with caption)
                FakeUploadResponse(
                    status=200, json_data={"success": True, "message": {"_id": "m1"}}
                ),
                # chat.postMessage (text)
                FakeUploadResponse(
                    status=200, json_data={"success": True, "message": {"_id": "m2"}}
                ),
            ]
        )
        client = FakeUploadClient(session)

        result = await standalone_send(
            pconfig={
                "server_url": "https://chat.example.com",
                "auth_mode": "token",
                "user_id": "bot1",
                "access_token": "tok",
            },
            chat_id="room-thread-1",
            message="threaded delivery",
            media_files=[tmp_path],
            thread_id="thread-9",
            force_document=False,
            _client_factory=lambda: client,
        )

        assert result["success"]

        # mediaConfirm carries the thread id
        confirm = [r for r in session.requests if "mediaConfirm" in r["url"]]
        assert confirm
        assert confirm[0]["json"]["tmid"] == "thread-9"

        # every postMessage carries the thread id
        posts = [r for r in session.requests if "chat.postMessage" in r["url"]]
        assert len(posts) == 2
        for post in posts:
            assert post["json"]["tmid"] == "thread-9"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# v0.2 P2.3 — user/username target resolution to DM rooms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_id_target_passes_through():
    """A real room id is used as-is (one rooms.info probe)."""
    session = FakeUploadSession(
        responses=[
            FakeUploadResponse(
                status=200, json_data={"success": True, "room": {"_id": "room-1"}}
            ),
        ]
    )
    client = FakeUploadClient(session)

    room_id = await resolve_delivery_target(client, "room-1")

    assert room_id == "room-1"
    assert session.requests[0]["method"] == "GET"
    assert "rooms.info" in session.requests[0]["url"]


@pytest.mark.asyncio
async def test_user_id_target_resolved_to_dm():
    """A user id target resolves via users.info -> dm.create."""
    session = FakeUploadSession(
        responses=[
            # rooms.info -> 404 (not a room)
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "room-not-found"}
            ),
            # users.info(userId) -> username
            FakeUploadResponse(
                status=200,
                json_data={
                    "success": True,
                    "user": {"_id": "u-9", "username": "alice"},
                },
            ),
            # dm.create -> room
            FakeUploadResponse(
                status=200, json_data={"success": True, "room": {"_id": "dm-9"}}
            ),
        ]
    )
    client = FakeUploadClient(session)

    room_id = await resolve_delivery_target(client, "u-9")

    assert room_id == "dm-9"
    urls = [r["url"] for r in session.requests]
    assert any("rooms.info" in u for u in urls)
    assert any("users.info" in u for u in urls)
    assert any("dm.create" in u for u in urls)


@pytest.mark.asyncio
async def test_username_target_resolved_to_dm():
    """A bare username target resolves via users.info(username) -> dm.create."""
    session = FakeUploadSession(
        responses=[
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "room-not-found"}
            ),
            # users.info(userId=alice) -> 404 (it's a username, not an id)
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "user-not-found"}
            ),
            # users.info(username=alice)
            FakeUploadResponse(
                status=200,
                json_data={
                    "success": True,
                    "user": {"_id": "u-9", "username": "alice"},
                },
            ),
            FakeUploadResponse(
                status=200, json_data={"success": True, "room": {"_id": "dm-9"}}
            ),
        ]
    )
    client = FakeUploadClient(session)

    room_id = await resolve_delivery_target(client, "alice")

    assert room_id == "dm-9"


@pytest.mark.asyncio
async def test_unresolvable_target_falls_back():
    """When nothing resolves, the original target is returned (post may fail)."""
    session = FakeUploadSession(
        responses=[
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "room-not-found"}
            ),
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "user-not-found"}
            ),
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "user-not-found"}
            ),
        ]
    )
    client = FakeUploadClient(session)

    room_id = await resolve_delivery_target(client, "ghost")

    assert room_id == "ghost"


@pytest.mark.asyncio
async def test_standalone_send_resolves_user_target():
    """standalone_send must deliver to the DM room for a user target."""
    session = FakeUploadSession(
        responses=[
            # client.initialize -> /api/v1/me
            FakeUploadResponse(
                status=200,
                json_data={"success": True, "_id": "bot1", "username": "hermesbot"},
            ),
            # rooms.info -> 404
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "room-not-found"}
            ),
            # users.info(userId) -> 404 (username target)
            FakeUploadResponse(
                status=404, json_data={"success": False, "error": "user-not-found"}
            ),
            # users.info(username) -> user
            FakeUploadResponse(
                status=200,
                json_data={
                    "success": True,
                    "user": {"_id": "u-9", "username": "alice"},
                },
            ),
            # dm.create -> room
            FakeUploadResponse(
                status=200, json_data={"success": True, "room": {"_id": "dm-9"}}
            ),
            # chat.postMessage to the DM room
            FakeUploadResponse(
                status=200, json_data={"success": True, "message": {"_id": "m-1"}}
            ),
        ]
    )
    client = FakeUploadClient(session)
    # patch the module-level resolver cache so this run is deterministic
    import adapter as adapter_module

    adapter_module._delivery_room_cache.clear()

    result = await standalone_send(
        pconfig={
            "server_url": "https://chat.example.com",
            "auth_mode": "token",
            "user_id": "bot1",
            "access_token": "tok",
        },
        chat_id="alice",
        message="hello alice",
        _client_factory=lambda: client,
    )

    assert result["success"]
    post = [r for r in session.requests if "chat.postMessage" in r["url"]]
    assert post
    assert post[0]["json"]["roomId"] == "dm-9"
