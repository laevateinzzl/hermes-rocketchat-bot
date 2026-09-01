"""Tests for native outbound media delivery (v0.2 P0.1).

Hermes' post-stream media delivery routes MEDIA files through
``send_image_file`` / ``send_document`` / ``send_video`` / ``send_voice`` /
``send_animation`` / ``send_multiple_images``.  The Rocket.Chat adapter must
override those with a native ``rooms.media`` upload instead of the base
"couldn't deliver" fallback.
"""

import tempfile
from pathlib import Path

import pytest  # type: ignore[reportMissingImports]

from adapter import (
    RocketChatAdapter,
    RocketChatClientError,
    RocketChatConfig,
    RocketChatRateLimitError,
)


class FakeUploadClient:
    """Fake client recording upload_attachment / post_message calls."""

    def __init__(self):
        self.uploads: list[dict] = []
        self.identity = None
        self._upload_counter = 0

    async def upload_attachment(self, room_id, file_path, text="", tmid=""):
        self._upload_counter += 1
        call = {
            "room_id": room_id,
            "file_path": str(file_path),
            "text": text,
            "tmid": tmid,
        }
        self.uploads.append(call)
        return {
            "_id": f"media-{self._upload_counter}",
            "file": {"_id": f"f{self._upload_counter}"},
        }

    async def post_message(self, room_id, text, tmid=""):
        if not hasattr(self, "post_message_calls"):
            self.post_message_calls: list[dict] = []
        call = {"room_id": room_id, "text": text, "tmid": tmid}
        self.post_message_calls.append(call)
        return {"_id": "m" + str(len(self.post_message_calls))}

    async def update_message(self, room_id, message_id, text):
        if not hasattr(self, "update_message_calls"):
            self.update_message_calls: list[dict] = []
        self.update_message_calls.append(
            {"room_id": room_id, "message_id": message_id, "text": text}
        )
        return {"_id": message_id}

    @property
    def server_url(self):
        return "https://chat.example.com"


class FailingUploadClient(FakeUploadClient):
    """Client whose upload always fails."""

    async def upload_attachment(self, room_id, file_path, text="", tmid=""):
        raise RocketChatClientError("Upload failed: permission denied")


def _make_adapter(client=None, connected=True) -> RocketChatAdapter:
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    setattr(adapter, "_client", client or FakeUploadClient())
    adapter._connected = connected
    return adapter


@pytest.fixture
def media_file():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(b"fake-image-data")
        path = tmp.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def doc_file():
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(b"%PDF-fake")
        path = tmp.name
    yield path
    Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# send_image_file / send_document / send_video / send_voice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_image_file_uploads_natively(media_file):
    """send_image_file must upload the local file instead of a fallback notice."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_image_file(
        chat_id="room-1",
        image_path=media_file,
        caption="a picture",
    )

    assert result.success
    assert result.message_id == "media-1"
    assert client.uploads == [
        {
            "room_id": "room-1",
            "file_path": media_file,
            "text": "a picture",
            "tmid": "",
        }
    ]


@pytest.mark.asyncio
async def test_send_document_uploads_natively(doc_file):
    """send_document must upload the file with its caption as text."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_document(
        chat_id="room-1",
        file_path=doc_file,
        caption="report",
        file_name="report.pdf",
    )

    assert result.success
    assert client.uploads[0]["file_path"] == doc_file
    assert client.uploads[0]["text"] == "report"


@pytest.mark.asyncio
async def test_send_video_uploads_natively(media_file):
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_video(chat_id="room-1", video_path=media_file)

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_voice_uploads_natively(media_file):
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_voice(chat_id="room-1", audio_path=media_file)

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_voice_accepts_is_voice_kwarg(media_file):
    """Hermes passes ``is_voice=`` (ccc367dce0); the adapter must not crash."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_voice(
        chat_id="room-1", audio_path=media_file, is_voice=True
    )

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_voice_transcodes_non_opus_when_voice(media_file, monkeypatch):
    """is_voice=True with a non-Opus file must use the Hermes transcode helper."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    transcoded = []

    def fake_transcode(path, **kwargs):
        transcoded.append(str(path))
        return "/tmp/converted.ogg"

    monkeypatch.setattr("adapter.transcode_to_ogg_opus", fake_transcode)

    result = await adapter.send_voice(
        chat_id="room-1", audio_path=media_file, is_voice=True
    )

    assert result.success
    assert transcoded == [media_file]
    assert client.uploads[0]["file_path"] == "/tmp/converted.ogg"


@pytest.mark.asyncio
async def test_send_voice_transcode_failure_uploads_original(media_file, monkeypatch):
    """When transcode fails, the original file still goes out (best-effort)."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    monkeypatch.setattr("adapter.transcode_to_ogg_opus", lambda path, **kw: None)

    result = await adapter.send_voice(
        chat_id="room-1", audio_path=media_file, is_voice=True
    )

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_voice_opus_skips_transcode(media_file, monkeypatch):
    """.ogg/.opus sources are already voice-native; no transcode attempt."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    called = []

    def fake_transcode(path, **kwargs):
        called.append(str(path))
        return "/tmp/converted.ogg"

    monkeypatch.setattr("adapter.transcode_to_ogg_opus", fake_transcode)
    ogg_file = media_file.replace(".png", ".ogg")

    result = await adapter.send_voice(
        chat_id="room-1", audio_path=ogg_file, is_voice=True
    )

    assert result.success
    assert called == []
    assert client.uploads[0]["file_path"] == ogg_file


@pytest.mark.asyncio
async def test_media_send_respects_thread_metadata(media_file):
    """metadata thread_id must be forwarded to the upload (tmid)."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_image_file(
        chat_id="room-1",
        image_path=media_file,
        metadata={"thread_id": "thread-9"},
    )

    assert result.success
    assert client.uploads[0]["tmid"] == "thread-9"


@pytest.mark.asyncio
async def test_media_send_returns_failure_when_not_connected(media_file):
    adapter = _make_adapter(connected=False)

    result = await adapter.send_image_file(chat_id="room-1", image_path=media_file)

    assert not result.success
    assert result.error == "send_path_degraded"


@pytest.mark.asyncio
async def test_send_not_connected_reports_send_path_degraded():
    """Transient (not-connected) send failures return the replayable code."""
    adapter = _make_adapter(connected=False)

    result = await adapter.send(chat_id="room-1", content="hello")

    assert not result.success
    assert result.error == "send_path_degraded"


@pytest.mark.asyncio
async def test_send_transient_client_error_reports_send_path_degraded():
    """Network-level send errors must be replayable, not final (8e1db41041)."""

    class NetworkFailingClient(FakeUploadClient):
        async def upload_attachment(self, room_id, file_path, text="", tmid=""):
            raise RocketChatClientError("connection reset by peer")

    adapter = _make_adapter(NetworkFailingClient())

    result = await adapter.send_image_file(chat_id="room-1", image_path="/tmp/x.png")

    assert not result.success
    assert result.error == "send_path_degraded"


@pytest.mark.asyncio
async def test_send_auth_error_stays_visible():
    """Definitive (non-transient) failures must keep their real message."""

    class AuthFailingClient(FakeUploadClient):
        async def upload_attachment(self, room_id, file_path, text="", tmid=""):
            raise RocketChatClientError("HTTP 401: Unauthorized")

    adapter = _make_adapter(AuthFailingClient())

    result = await adapter.send_image_file(chat_id="room-1", image_path="/tmp/x.png")

    assert not result.success
    assert result.error == "HTTP 401: Unauthorized"


@pytest.mark.asyncio
async def test_send_zero_max_message_length_posts_single_message():
    """ROCKETCHAT_MAX_MESSAGE_LENGTH=0 means no limit: one post, no chunking."""
    client = FakeUploadClient()
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        max_message_length=0,
    )
    adapter = RocketChatAdapter(cfg)
    setattr(adapter, "_client", client)
    adapter._connected = True

    long_text = "x" * 5000
    result = await adapter.send(chat_id="room-1", content=long_text)

    assert result.success
    assert client.post_message_calls[0]["text"] == long_text
    assert len(client.post_message_calls) == 1


def test_split_long_text_zero_or_negative_limit_does_not_loop():
    """A 0/negative chunk limit must return one chunk, never spin."""
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    text = "x" * 100
    assert adapter._split_long_text(text, 0) == [text]
    assert adapter._split_long_text(text, -5) == [text]


@pytest.mark.asyncio
async def test_media_send_surfaces_upload_error(media_file):
    client = FailingUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_document(chat_id="room-1", file_path=media_file)

    assert not result.success
    assert "Upload failed" in result.error


# ---------------------------------------------------------------------------
# send_image (URL) and send_animation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_image_local_path_uploads(media_file):
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_image(
        chat_id="room-1",
        image_url=media_file,
        caption="from disk",
    )

    assert result.success
    assert client.uploads[0]["file_path"] == media_file
    assert client.uploads[0]["text"] == "from disk"


@pytest.mark.asyncio
async def test_send_image_file_uri_uploads(media_file):
    from urllib.parse import quote

    client = FakeUploadClient()
    adapter = _make_adapter(client)
    file_uri = f"file://{quote(media_file)}"

    result = await adapter.send_image(chat_id="room-1", image_url=file_uri)

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_image_http_downloads_then_uploads(media_file, monkeypatch):
    """http(s) image sources must be downloaded (guarded) then uploaded."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    async def fake_download(url, ext):
        return media_file

    monkeypatch.setattr(adapter, "_download_media_url", fake_download)

    result = await adapter.send_image(
        chat_id="room-1",
        image_url="https://example.com/pic.png",
    )

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_image_download_failure_returns_error(monkeypatch):
    """Unsafe/unreachable URL must yield a failure SendResult, not a fallback send."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    async def failing_download(url, ext):
        raise RocketChatClientError("Blocked unsafe URL (SSRF protection)")

    monkeypatch.setattr(adapter, "_download_media_url", failing_download)

    result = await adapter.send_image(
        chat_id="room-1",
        image_url="https://example.com/evil.png",
    )

    assert not result.success
    assert "Blocked unsafe URL" in result.error
    assert client.uploads == []


@pytest.mark.asyncio
async def test_send_animation_gif_uploads(media_file):
    """send_animation must upload the GIF natively (file or downloaded)."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send_animation(
        chat_id="room-1",
        animation_url=media_file,
    )

    assert result.success
    assert client.uploads[0]["file_path"] == media_file


# ---------------------------------------------------------------------------
# send_multiple_images
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_multiple_images_uploads_batch(media_file, doc_file, monkeypatch):
    """Each file:// image in the batch must be uploaded via send_image."""
    from urllib.parse import quote

    client = FakeUploadClient()
    adapter = _make_adapter(client)

    images = [
        (f"file://{quote(media_file)}", "first"),
        (f"file://{quote(doc_file)}", "second"),
    ]

    await adapter.send_multiple_images(chat_id="room-1", images=images)

    assert len(client.uploads) == 2
    assert client.uploads[0]["file_path"] == media_file
    assert client.uploads[0]["text"] == "first"
    assert client.uploads[1]["file_path"] == doc_file
    assert client.uploads[1]["text"] == "second"


@pytest.mark.asyncio
async def test_send_multiple_images_skips_bad_sources(media_file, monkeypatch):
    """Unsupported sources must not crash the batch loop."""
    from urllib.parse import quote

    client = FakeUploadClient()
    adapter = _make_adapter(client)

    images = [
        (f"file://{quote(media_file)}", "good"),
        ("ftp://weird/source", "bad"),
    ]

    results = await adapter.send_multiple_images(chat_id="room-1", images=images)

    assert len(client.uploads) == 1
    assert results[1].success is False


# ---------------------------------------------------------------------------
# 0.3.0-B: chunked-send semantics, rate limits, placeholder lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_partial_failure_returns_final_error_with_last_id():
    """A mid-chunk failure must report final error + last chunk id, never
    the replayable send_path_degraded code (which would duplicate the
    already-posted prefix on replay)."""

    class PartialFailClient(FakeUploadClient):
        def __init__(self):
            super().__init__()
            self._posts = 0

        async def post_message(self, room_id, text, tmid=""):
            self._posts += 1
            if self._posts >= 2:
                raise RocketChatClientError("connection reset by peer")
            return {"_id": f"m{self._posts}"}

    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        max_message_length=10,
    )
    adapter = RocketChatAdapter(cfg)
    setattr(adapter, "_client", PartialFailClient())
    adapter._connected = True

    result = await adapter.send(chat_id="room-1", content="x" * 30)

    assert not result.success
    assert result.error == "connection reset by peer"
    assert result.message_id == "m1"


@pytest.mark.asyncio
async def test_send_rate_limit_maps_to_send_path_degraded():
    """Outbound 429s are transient — the ledger must replay them."""

    class RateLimitedClient(FakeUploadClient):
        async def post_message(self, room_id, text, tmid=""):
            raise RocketChatRateLimitError(
                "HTTP 429: Too Many Requests", retry_after=1.0
            )

    adapter = _make_adapter(RateLimitedClient())

    result = await adapter.send(chat_id="room-1", content="hello")

    assert not result.success
    assert result.error == "send_path_degraded"


@pytest.mark.asyncio
async def test_send_typing_post_failure_is_swallowed():
    """A failed placeholder creation must not crash the streaming turn."""

    class TypingFailClient(FakeUploadClient):
        async def post_message(self, room_id, text, tmid=""):
            raise RocketChatClientError("room deleted")

    adapter = _make_adapter(TypingFailClient())

    # Must not raise.
    await adapter.send_typing(chat_id="room-1")


@pytest.mark.asyncio
async def test_stop_typing_scoped_to_thread_with_metadata():
    """stop_typing with metadata must not clear other threads' previews."""
    adapter = _make_adapter()
    adapter._stream_previews["room-1\u0000thread-9"] = "m9"
    adapter._stream_previews["room-1\u0000thread-2"] = "m2"

    await adapter.stop_typing("room-1", {"thread_id": "thread-9"})

    assert "room-1\u0000thread-9" not in adapter._stream_previews
    assert "room-1\u0000thread-2" in adapter._stream_previews


# ---------------------------------------------------------------------------
# 0.3.0-C: UTF-16 budgeting, media-only sends
# ---------------------------------------------------------------------------


def test_split_long_text_budgets_utf16_units():
    """Astral-heavy content must be chunked by UTF-16 units (emoji = 2)."""
    from adapter import _utf16_units

    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    emoji = "😀" * 3000  # 3000 code points == 6000 UTF-16 units

    chunks = adapter._split_long_text(emoji, 4000)

    assert len(chunks) >= 2
    assert "".join(chunks) == emoji
    for chunk in chunks:
        assert _utf16_units(chunk) <= 4000


def test_prefix_within_units_never_splits_surrogate_pairs():
    from adapter import _utf16_units, _prefix_within_units

    text = "ab😀cd"  # 6 units
    prefix = _prefix_within_units(text, 5)
    # Budget respected and never a split surrogate pair: the remainder
    # re-combines with the prefix to the exact original.
    assert _utf16_units(prefix) <= 5
    assert prefix + text[len(prefix) :] == text


@pytest.mark.asyncio
async def test_send_delivers_media_files(media_file):
    """send(media_files=[...]) must upload the files natively (legacy shim)."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send(
        chat_id="room-1", content="with attachment", media_files=[media_file]
    )

    assert result.success
    assert len(client.uploads) == 1
    assert client.uploads[0]["file_path"] == media_file


@pytest.mark.asyncio
async def test_send_media_only_skips_empty_post(media_file):
    """Media-only sends must not post an empty text message."""
    client = FakeUploadClient()
    adapter = _make_adapter(client)

    result = await adapter.send(chat_id="room-1", content="", media_files=[media_file])

    assert result.success
    assert result.message_id == "media-1"
    assert getattr(client, "post_message_calls", []) == []
