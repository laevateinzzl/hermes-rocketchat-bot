"""Tests for live tool-progress status text in the thinking placeholder (v0.2 P0.3).

Hermes feeds live per-tool status phrases ("is running pytest…") via
``adapter.set_status_text()`` when ``supports_status_text`` is True.  The
Rocket.Chat adapter renders that phrase inside the 💭 Thinking… placeholder,
so channel users see what the bot is doing in real time.
"""

import pytest  # type: ignore[reportMissingImports]

from adapter import (
    RocketChatAdapter,
    RocketChatConfig,
    THINKING_PLACEHOLDER_TEXT,
)


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


def _make_adapter(client=None, connected=True) -> RocketChatAdapter:
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    setattr(adapter, "_client", client or FakeClient())
    adapter._connected = connected
    return adapter


@pytest.mark.asyncio
async def test_supports_status_text_flag():
    """The adapter advertises status-text support to the gateway."""
    adapter = _make_adapter()
    assert adapter.supports_status_text


@pytest.mark.asyncio
async def test_placeholder_renders_live_status_text():
    """send_typing must render the current status phrase inside the placeholder."""
    client = FakeClient()
    adapter = _make_adapter(client)
    adapter.set_status_text("room-1", "is running pytest…")

    await adapter.send_typing("room-1")

    assert client.post_message_calls == [
        {
            "room_id": "room-1",
            "text": f"{THINKING_PLACEHOLDER_TEXT} is running pytest…",
            "tmid": "",
        }
    ]


@pytest.mark.asyncio
async def test_placeholder_updates_when_status_changes():
    """A later typing refresh must edit the placeholder with the new phrase."""
    client = FakeClient()
    adapter = _make_adapter(client)

    await adapter.send_typing("room-1")  # plain placeholder
    adapter.set_status_text("room-1", "is searching the web…")
    await adapter.send_typing("room-1")

    assert len(client.post_message_calls) == 1  # no second bubble
    assert client.update_message_calls == [
        {
            "room_id": "room-1",
            "message_id": "sent-1",
            "text": f"{THINKING_PLACEHOLDER_TEXT} is searching the web…",
        }
    ]


@pytest.mark.asyncio
async def test_placeholder_not_reposted_when_status_unchanged():
    """Repeated refreshes with the same status must not spam chat.update."""
    client = FakeClient()
    adapter = _make_adapter(client)
    adapter.set_status_text("room-1", "working…")

    await adapter.send_typing("room-1")
    await adapter.send_typing("room-1")
    await adapter.send_typing("room-1")

    assert len(client.post_message_calls) == 1
    assert client.update_message_calls == []


@pytest.mark.asyncio
async def test_status_text_clear_restores_plain_placeholder():
    """set_status_text(chat, None) must restore the plain thinking text."""
    client = FakeClient()
    adapter = _make_adapter(client)

    await adapter.send_typing("room-1")
    adapter.set_status_text("room-1", "working…")
    await adapter.send_typing("room-1")
    adapter.set_status_text("room-1", None)
    await adapter.send_typing("room-1")

    assert len(client.post_message_calls) == 1
    assert client.update_message_calls[-1]["text"] == THINKING_PLACEHOLDER_TEXT


@pytest.mark.asyncio
async def test_status_text_not_connected_is_noop():
    """send_typing must remain a no-op while disconnected."""
    client = FakeClient()
    adapter = _make_adapter(client, connected=False)
    adapter.set_status_text("room-1", "working…")

    await adapter.send_typing("room-1")

    assert client.post_message_calls == []
