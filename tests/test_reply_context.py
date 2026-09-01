"""Tests for thread reply-context backfill (v0.2 P2.2).

Mirrors Hermes fc0009b9b / c8089dabc: thread replies should carry the parent
message text/author so the agent gets context.  The adapter fetches the
parent via chat.getMessage (cached per thread) and populates
reply_to_text / reply_to_author_* / reply_to_is_own_message.
"""

import pytest  # type: ignore[reportMissingImports]

from adapter import (
    BoundedDict,
    RocketChatAdapter,
    RocketChatClientError,
    RocketChatConfig,
)


class FakeClient:
    """Fake client with get_message + identity for reply-context tests."""

    def __init__(self, messages=None):
        self._messages = dict(messages or {})
        self.get_message_calls: list[str] = []
        self._identity = type("I", (), {"user_id": "bot1", "username": "hermesbot"})()

    async def get_message(self, message_id):
        self.get_message_calls.append(message_id)
        if message_id not in self._messages:
            raise RocketChatClientError("chat.getMessage failed: message not found")
        return self._messages[message_id]

    @property
    def identity(self):
        return self._identity

    @property
    def server_url(self):
        return "https://chat.example.com"


PARENT = {
    "_id": "parent-1",
    "rid": "room-1",
    "msg": "what is the weather?",
    "u": {"_id": "alice", "username": "alice"},
    "t": "",
}


def _adapter(client):
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    setattr(adapter, "_client", client)
    adapter._connected = True
    return adapter


def _thread_event():
    return {
        "_id": "reply-1",
        "rid": "room-1",
        "msg": "tell me more",
        "tmid": "parent-1",
        "u": {"_id": "bob", "username": "bob"},
        "t": "",
        "mentions": [],
        "_room_type": "c",
    }


@pytest.mark.asyncio
async def test_thread_reply_populates_parent_context():
    client = FakeClient({"parent-1": PARENT})
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    # Mention gate: message must mention the bot to pass in a channel.
    event = _thread_event()
    event["msg"] = "@hermesbot tell me more"
    event["mentions"] = [{"username": "hermesbot"}]

    await adapter._on_inbound(event)

    assert len(handled) == 1
    ev = handled[0]
    assert ev.reply_to_message_id == "parent-1"
    assert ev.reply_to_text == "what is the weather?"
    assert ev.reply_to_author_id == "alice"
    assert ev.reply_to_author_name == "alice"
    assert not ev.reply_to_is_own_message


@pytest.mark.asyncio
async def test_reply_to_own_message_detected():
    own = dict(PARENT)
    own["u"] = {"_id": "bot1", "username": "hermesbot"}
    client = FakeClient({"parent-1": own})
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    event = _thread_event()
    event["msg"] = "@hermesbot reply"
    event["mentions"] = [{"username": "hermesbot"}]

    await adapter._on_inbound(event)

    assert handled[0].reply_to_is_own_message


@pytest.mark.asyncio
async def test_reply_context_cached_per_thread():
    client = FakeClient({"parent-1": PARENT})
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    for i in range(3):
        event = _thread_event()
        event["_id"] = f"reply-{i}"
        event["msg"] = f"@hermesbot msg {i}"
        event["mentions"] = [{"username": "hermesbot"}]
        await adapter._on_inbound(event)

    assert len(handled) == 3
    assert client.get_message_calls == ["parent-1"]  # fetched once


@pytest.mark.asyncio
async def test_missing_parent_is_graceful():
    """A deleted/inaccessible parent must not block inbound delivery."""
    client = FakeClient({})  # no parent available
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    event = _thread_event()
    event["msg"] = "@hermesbot hi"
    event["mentions"] = [{"username": "hermesbot"}]

    await adapter._on_inbound(event)

    assert len(handled) == 1
    ev = handled[0]
    assert ev.reply_to_message_id == "parent-1"
    assert ev.reply_to_text == ""


@pytest.mark.asyncio
async def test_non_thread_message_skips_fetch():
    client = FakeClient({"parent-1": PARENT})
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    event = _thread_event()
    event.pop("tmid")  # plain message, not a thread reply
    event["msg"] = "@hermesbot hi"
    event["mentions"] = [{"username": "hermesbot"}]

    await adapter._on_inbound(event)

    assert len(handled) == 1
    assert client.get_message_calls == []


def test_reply_cache_is_bounded():
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    assert isinstance(adapter._reply_cache, BoundedDict)
    assert adapter._reply_cache.maxsize > 0


@pytest.mark.asyncio
async def test_missing_parent_negative_cached():
    """A deleted parent must be negatively cached — no getMessage per reply."""
    client = FakeClient({})  # parent-1 unavailable
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    for i in range(3):
        event = _thread_event()
        event["_id"] = f"reply-{i}"
        event["msg"] = f"@hermesbot msg {i}"
        event["mentions"] = [{"username": "hermesbot"}]
        await adapter._on_inbound(event)

    assert len(handled) == 3
    # Fetched once for the missing parent, then served from the negative cache.
    assert len(client.get_message_calls) == 1


@pytest.mark.asyncio
async def test_reply_cache_positive_entries_expire(monkeypatch):
    """Positive entries older than the TTL are re-fetched."""
    import adapter as adapter_module

    client = FakeClient({"parent-1": PARENT})
    adapter = _adapter(client)
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    event = _thread_event()
    event["msg"] = "@hermesbot first"
    event["mentions"] = [{"username": "hermesbot"}]
    await adapter._on_inbound(event)
    assert client.get_message_calls == ["parent-1"]

    # Age the cache entry past the TTL.
    import time

    for key in list(adapter._reply_cache.keys()):
        ts, value = adapter._reply_cache[key]
        adapter._reply_cache[key] = (ts - 400, value)

    event["_id"] = "reply-2"
    event["msg"] = "@hermesbot second"
    await adapter._on_inbound(event)

    assert client.get_message_calls == ["parent-1", "parent-1"]
