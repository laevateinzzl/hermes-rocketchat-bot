"""Tests for capability flags and native long-message chunking (v0.2 P0.4).

Rocket.Chat renders markdown (including fenced code blocks) and supports
multi-message delivery, so the adapter advertises ``supports_code_blocks``
and ``splits_long_messages`` and splits oversized replies at paragraph/line
boundaries instead of hard-truncating them.
"""

import pytest  # type: ignore[reportMissingImports]

from adapter import RocketChatAdapter, RocketChatConfig


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


def _make_adapter(client=None, **cfg_kwargs) -> RocketChatAdapter:
    cfg = RocketChatConfig(server_url="https://chat.example.com", **cfg_kwargs)
    adapter = RocketChatAdapter(cfg)
    setattr(adapter, "_client", client or FakeClient())
    adapter._connected = True
    return adapter


LONG_TEXT = (
    "First paragraph with a fair amount of content to push past the limit.\n"
    "\n"
    "Second paragraph also carries several words of substance for splitting.\n"
    "\n"
    "Third paragraph ends the message."
)


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


def test_supports_code_blocks_flag():
    """Rocket.Chat renders markdown fenced code blocks."""
    assert RocketChatAdapter.supports_code_blocks


def test_splits_long_messages_flag():
    """The adapter chunks natively, so the gateway skips its truncation."""
    assert RocketChatAdapter.splits_long_messages


# ---------------------------------------------------------------------------
# Chunking behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_message_sends_single_post():
    client = FakeClient()
    adapter = _make_adapter(client, max_message_length=20)

    result = await adapter.send("room-1", "short", metadata={"notify": True})

    assert result.success
    assert len(client.post_message_calls) == 1
    assert client.post_message_calls[0]["text"] == "short"


@pytest.mark.asyncio
async def test_long_message_splits_into_multiple_posts():
    client = FakeClient()
    adapter = _make_adapter(client, max_message_length=40)

    result = await adapter.send("room-1", LONG_TEXT, metadata={"notify": True})

    assert result.success
    texts = [c["text"] for c in client.post_message_calls]
    assert len(texts) > 1
    assert all(len(t) <= 40 for t in texts)
    # Content is preserved exactly across chunks (no truncation).
    assert "".join(texts) == LONG_TEXT
    # All chunks carry the same thread anchor.
    assert all(c["tmid"] == "" for c in client.post_message_calls)


@pytest.mark.asyncio
async def test_long_message_first_chunk_consumes_placeholder():
    """The placeholder is edited with the first chunk; remaining chunks post."""
    client = FakeClient()
    adapter = _make_adapter(client, max_message_length=40)

    await adapter.send_typing("room-1")
    result = await adapter.send("room-1", LONG_TEXT, metadata={"notify": True})

    assert result.success
    assert len(client.update_message_calls) == 1
    first_chunk = client.update_message_calls[0]["text"]
    assert len(first_chunk) <= 40
    # placeholder consumed; the rest of the content arrives as new posts
    assert len(client.post_message_calls) == len(
        [t for t in client.post_message_calls if t["text"]]
    )


@pytest.mark.asyncio
async def test_single_line_hard_split():
    """A single unbreakable line is split at the character limit."""
    client = FakeClient()
    adapter = _make_adapter(client, max_message_length=10)

    result = await adapter.send(
        "room-1", "abcdefghijklmnopqrstuvwxyz", metadata={"notify": True}
    )

    assert result.success
    texts = [c["text"] for c in client.post_message_calls]
    assert all(len(t) <= 10 for t in texts)
    assert "".join(texts) == "abcdefghijklmnopqrstuvwxyz"


@pytest.mark.asyncio
async def test_chunks_keep_thread_tmid():
    client = FakeClient()
    adapter = _make_adapter(client, max_message_length=40)

    result = await adapter.send(
        "room-1",
        LONG_TEXT,
        metadata={"thread_id": "thread-7", "notify": True},
    )

    assert result.success
    assert client.post_message_calls
    assert all(c["tmid"] == "thread-7" for c in client.post_message_calls)


@pytest.mark.asyncio
async def test_splitter_preserves_content_exactly():
    """The pure splitter must round-trip content with no loss or insertion."""
    adapter = _make_adapter(max_message_length=25)

    for text in (LONG_TEXT, "x" * 100, "line one\nline two\nline three"):
        chunks = adapter._split_long_text(text, 25)
        assert "".join(chunks) == text
        assert all(len(c) <= 25 for c in chunks)
