"""Tests for live streaming reply previews via edit_message (v0.2 P0.2).

Hermes' stream consumer sends the first reply chunk through ``send()`` with
``metadata.expect_edits=True`` and then grows it with ``edit_message()``.
The Rocket.Chat adapter must:
  - implement edit_message (chat.update) so the consumer stops using the
    non-streaming path;
  - reuse the 💭 Thinking… placeholder as the first editable preview so the
    user sees one growing message instead of a placeholder + duplicate bubble.
"""

import pytest  # type: ignore[reportMissingImports]

from adapter import RocketChatAdapter, RocketChatConfig, RocketChatClientError


class FakeClient:
    """Fake client recording post/update message calls."""

    def __init__(self):
        self.post_message_calls: list[dict] = []
        self.update_message_calls: list[dict] = []
        self._identity = None
        self._post_n = 0

    async def post_message(self, room_id, text, tmid=""):
        self._post_n += 1
        call = {"room_id": room_id, "text": text, "tmid": tmid}
        self.post_message_calls.append(call)
        return {"_id": f"sent-{self._post_n}"}

    async def update_message(self, room_id, message_id, text):
        call = {"room_id": room_id, "message_id": message_id, "text": text}
        self.update_message_calls.append(call)
        return {"_id": message_id}

    @property
    def identity(self):
        return self._identity

    @property
    def server_url(self):
        return "https://chat.example.com"


class FailingClient(FakeClient):
    """Client whose update always fails."""

    async def update_message(self, room_id, message_id, text):
        raise RocketChatClientError("chat.update failed: unauthorized")


def _make_adapter(client=None, connected=True, **cfg_kwargs) -> RocketChatAdapter:
    cfg = RocketChatConfig(server_url="https://chat.example.com", **cfg_kwargs)
    adapter = RocketChatAdapter(cfg)
    setattr(adapter, "_client", client or FakeClient())
    adapter._connected = connected
    return adapter


# ---------------------------------------------------------------------------
# edit_message basics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_message_updates_existing_message():
    """edit_message must call chat.update and return the message id."""
    client = FakeClient()
    adapter = _make_adapter(client)

    result = await adapter.edit_message(
        chat_id="room-1",
        message_id="msg-1",
        content="growing reply",
    )

    assert result.success
    assert result.message_id == "msg-1"
    assert client.update_message_calls == [
        {"room_id": "room-1", "message_id": "msg-1", "text": "growing reply"}
    ]


@pytest.mark.asyncio
async def test_edit_message_truncates_to_max_length():
    """Intermediate edits must respect the max message length."""
    client = FakeClient()
    adapter = _make_adapter(client, max_message_length=10)

    result = await adapter.edit_message(
        chat_id="room-1",
        message_id="msg-1",
        content="thisisaverylongreply",
    )

    assert result.success
    assert client.update_message_calls[0]["text"] == "thisisaver"


@pytest.mark.asyncio
async def test_edit_message_accepts_finalize_and_metadata():
    """The stream consumer passes finalize= and metadata=; both must be tolerated."""
    client = FakeClient()
    adapter = _make_adapter(client)

    result = await adapter.edit_message(
        chat_id="room-1",
        message_id="msg-1",
        content="final text",
        finalize=True,
        metadata={"thread_id": "thread-1"},
    )

    assert result.success
    assert client.update_message_calls[0]["text"] == "final text"


@pytest.mark.asyncio
async def test_edit_message_not_connected_returns_failure():
    adapter = _make_adapter(connected=False)

    result = await adapter.edit_message(
        chat_id="room-1", message_id="msg-1", content="hi"
    )

    assert not result.success
    assert result.error


@pytest.mark.asyncio
async def test_edit_message_surfaces_client_error():
    client = FailingClient()
    adapter = _make_adapter(client)

    result = await adapter.edit_message(
        chat_id="room-1", message_id="msg-1", content="hi"
    )

    assert not result.success
    assert "unauthorized" in result.error


# ---------------------------------------------------------------------------
# Placeholder → live preview integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_first_preview_reuses_placeholder():
    """First expect_edits send must edit the thinking placeholder, not post a second bubble."""
    client = FakeClient()
    adapter = _make_adapter(client)

    await adapter.send_typing("room-1")
    assert len(client.post_message_calls) == 1  # the placeholder

    result = await adapter.send(
        "room-1",
        "first chunk",
        metadata={"expect_edits": True},
    )

    assert result.success
    # Placeholder message edited into the preview; no second post.
    assert len(client.post_message_calls) == 1
    assert client.update_message_calls == [
        {"room_id": "room-1", "message_id": "sent-1", "text": "first chunk"}
    ]


@pytest.mark.asyncio
async def test_streaming_no_placeholder_posts_new_message():
    """Without a placeholder, the preview send posts a normal message."""
    client = FakeClient()
    adapter = _make_adapter(client)

    result = await adapter.send(
        "room-1",
        "first chunk",
        metadata={"expect_edits": True},
    )

    assert result.success
    assert len(client.post_message_calls) == 1
    assert client.update_message_calls == []


@pytest.mark.asyncio
async def test_typing_suppressed_while_stream_preview_live():
    """send_typing must not create a second placeholder while a preview is live."""
    client = FakeClient()
    adapter = _make_adapter(client)

    await adapter.send_typing("room-1")
    await adapter.send("room-1", "chunk", metadata={"expect_edits": True})

    # Refresh typing during the stream → no new placeholder bubble.
    await adapter.send_typing("room-1")

    assert len(client.post_message_calls) == 1


@pytest.mark.asyncio
async def test_stop_typing_clears_stream_preview_marker():
    """After the turn ends, the next send_typing may create a fresh placeholder."""
    client = FakeClient()
    adapter = _make_adapter(client)

    await adapter.send_typing("room-1")
    await adapter.send("room-1", "chunk", metadata={"expect_edits": True})
    await adapter.stop_typing("room-1")

    await adapter.send_typing("room-1")
    assert len(client.post_message_calls) == 2  # new placeholder


@pytest.mark.asyncio
async def test_final_notify_send_after_stream_posts_normally():
    """A final send with notify=True after the preview was consumed posts normally."""
    client = FakeClient()
    adapter = _make_adapter(client)

    await adapter.send_typing("room-1")
    await adapter.send("room-1", "chunk", metadata={"expect_edits": True})

    result = await adapter.send("room-1", "final", metadata={"notify": True})

    assert result.success
    # preview consumed the placeholder; the final send is a fresh post
    assert len(client.post_message_calls) == 2
