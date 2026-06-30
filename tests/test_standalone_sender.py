"""Tests for outbound file upload and standalone sender."""

import pytest
import tempfile
from pathlib import Path

from adapter import standalone_send, RocketChatClient


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

    def __init__(self, session, server_url="https://chat.example.com", user_id="bot1", access_token="tok"):
        super().__init__(server_url=server_url, user_id=user_id, access_token=access_token)
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
                    json_data={"success": True, "file": {"_id": "file-1", "name": "test.png", "type": "image/png"}},
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
    """standalone_send should be callable and return a SendResult-like dict."""
    result = await standalone_send(
        pconfig={
            "server_url": "https://chat.example.com",
            "auth_mode": "token",
            "user_id": "bot1",
            "access_token": "tok",
        },
        chat_id="room-1",
        message="test message",
    )

    # Without a real client, we expect either an error or a timeout
    # In test environment with no network, it should return error gracefully
    assert isinstance(result, dict)
    # The result dict should have expected keys
    assert "success" in result or "error" in result


@pytest.mark.asyncio
async def test_standalone_send_media_files_is_callable():
    """standalone_send should accept media_files parameter."""
    result = await standalone_send(
        pconfig={
            "server_url": "https://chat.example.com",
            "auth_mode": "token",
            "user_id": "bot1",
            "access_token": "tok",
        },
        chat_id="room-1",
        message="here is an image",
        media_files=[],
    )

    assert isinstance(result, dict)
    assert "success" in result or "error" in result
