"""Tests for the Hermes RocketChatAdapter class."""

import pytest  # type: ignore[reportMissingImports]

from adapter import (
    PersistentSeenIdStore,
    RocketChatAdapter,
    RocketChatConfig,
    RocketChatIdentity,
)

# ---------------------------------------------------------------------------
# Fake client and transport for adapter tests
# ---------------------------------------------------------------------------


class FakeClient:
    """Records calls for adapter integration tests."""

    def __init__(self, identity=None):
        self._identity = identity
        self.post_message_calls: list[dict] = []
        self.update_message_calls: list[dict] = []
        self.initialize_calls = 0
        self._user_id = "bot1"
        self._access_token = "tok"
        self._username = "hermesbot"

    async def initialize(self):
        self.initialize_calls += 1
        if self._identity is None:
            self._identity = RocketChatIdentity(
                user_id=self._user_id,
                username=self._username,
                name="Hermes Bot",
                auth_token=self._access_token,
            )
        return self._identity

    async def post_message(self, room_id, text, tmid=""):
        call = {"room_id": room_id, "text": text, "tmid": tmid}
        self.post_message_calls.append(call)
        return {"_id": f"sent-{len(self.post_message_calls)}"}

    async def room_info(self, room_id):
        if not hasattr(self, "room_info_calls"):
            self.room_info_calls: list[str] = []
        self.room_info_calls.append(room_id)
        return {"_id": room_id, "t": "d", "name": "DMs"}

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


class FakeTransport:
    """A fake inbound transport that captures the callback and can inject events."""

    def __init__(self):
        self._on_message = None
        self.started = False
        self.stopped = False

    def set_on_message(self, callback):
        self._on_message = callback

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def inject(self, event):
        """Simulate an inbound message from the transport."""
        if self._on_message:
            await self._on_message(event)


# ---------------------------------------------------------------------------
# Adapter lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_connect_initializes_client_and_starts_transport():
    """connect() should parse config, create client, auth, and start transport."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        transport="polling",
        poll_interval_seconds=3.0,
    )

    adapter = RocketChatAdapter(cfg)
    # Override the client and transport creation to use fakes
    transport = FakeTransport()
    client = FakeClient()

    # Monkey-patch connect to use our fakes

    async def fake_connect():
        adapter._client = client
        await client.initialize()
        adapter._transport = transport
        transport.set_on_message(adapter._on_inbound)
        await transport.start()
        adapter._connected = True
        return True

    adapter.connect = fake_connect  # type: ignore[method-assign]

    result = await adapter.connect()

    assert result
    assert adapter._connected
    assert transport.started
    assert client.initialize_calls == 1


@pytest.mark.asyncio
async def test_adapter_disconnect_stops_transport():
    """disconnect() should stop the transport and mark as disconnected."""
    adapter = RocketChatAdapter(RocketChatConfig())
    transport = FakeTransport()
    adapter._transport = transport
    adapter._connected = True

    await adapter.disconnect()

    assert transport.stopped
    assert not adapter._connected


# ---------------------------------------------------------------------------
# Send tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_send_posts_message_with_tmid():
    """send() should call client.post_message with thread info."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    result = await adapter.send(
        chat_id="room-1",
        content="hello world",
        reply_to="thread-msg-1",
    )

    assert result.success
    assert result.message_id == "sent-1"
    assert client.post_message_calls[0]["room_id"] == "room-1"
    assert client.post_message_calls[0]["text"] == "hello world"
    assert client.post_message_calls[0]["tmid"] == "thread-msg-1"


@pytest.mark.asyncio
async def test_adapter_send_without_reply_to():
    """send() without reply_to should not include tmid."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    result = await adapter.send(chat_id="room-1", content="hi")

    assert result.success
    assert client.post_message_calls[0]["tmid"] == ""


@pytest.mark.asyncio
async def test_adapter_send_accepts_gateway_metadata_kwarg():
    """Hermes gateway may pass send(..., metadata=...) for notices."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    result = await adapter.send(
        chat_id="room-1",
        content="working",
        metadata={"kind": "notice"},
    )

    assert result.success
    assert client.post_message_calls[0]["text"] == "working"


@pytest.mark.asyncio
async def test_adapter_send_gateway_reply_without_thread_metadata_posts_main_room():
    """Generic Hermes reply anchors should not force Rocket.Chat thread replies."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    result = await adapter.send(
        chat_id="room-1",
        content="visible reply",
        reply_to="trigger-message-id",
        metadata={"notify": True},
    )

    assert result.success
    assert client.post_message_calls[0]["tmid"] == ""


@pytest.mark.asyncio
async def test_adapter_send_gateway_thread_metadata_uses_thread_id():
    """Rocket.Chat threads should use explicit Hermes thread metadata."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    result = await adapter.send(
        chat_id="room-1",
        content="thread reply",
        reply_to="child-message-id",
        metadata={"thread_id": "parent-thread-id", "notify": True},
    )

    assert result.success
    assert client.post_message_calls[0]["tmid"] == "parent-thread-id"


@pytest.mark.asyncio
async def test_adapter_send_typing_posts_placeholder_once_then_final_send_edits_it():
    """Hermes typing refresh should create one visible placeholder and final send should consume it."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    await adapter.send_typing("room-1")
    await adapter.send_typing("room-1")
    result = await adapter.send("room-1", "final answer", metadata={"notify": True})

    assert result.success
    assert client.post_message_calls == [
        {"room_id": "room-1", "text": "💭 Thinking…", "tmid": ""}
    ]
    assert client.update_message_calls == [
        {"room_id": "room-1", "message_id": "sent-1", "text": "final answer"}
    ]


@pytest.mark.asyncio
async def test_adapter_stop_typing_before_final_send_keeps_placeholder_for_edit():
    """Gateway stops typing before final delivery; final send must still edit the placeholder."""
    adapter = RocketChatAdapter(RocketChatConfig())
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    await adapter.send_typing("room-1")
    await adapter.stop_typing("room-1")
    result = await adapter.send("room-1", "final answer", metadata={"notify": True})

    assert result.success
    assert client.post_message_calls == [
        {"room_id": "room-1", "text": "💭 Thinking…", "tmid": ""}
    ]
    assert client.update_message_calls == [
        {"room_id": "room-1", "message_id": "sent-1", "text": "final answer"}
    ]


@pytest.mark.asyncio
async def test_adapter_send_returns_error_when_not_connected():
    """If the adapter is not connected, send should return a failure SendResult."""
    adapter = RocketChatAdapter(RocketChatConfig())
    adapter._connected = False

    result = await adapter.send(chat_id="room-1", content="hi")

    assert not result.success
    assert result.error


# ---------------------------------------------------------------------------
# Inbound message handling tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_dm_forwards_to_handle_message():
    """DM inbound events should bypass mention gating and call handle_message."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
    )

    adapter = RocketChatAdapter(cfg)
    adapter._client = FakeClient()
    adapter._connected = True

    handled_events = []

    async def fake_handle(event):
        handled_events.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    dm_event = {
        "_id": "dm-msg-1",
        "rid": "dm-room-1",
        "msg": "hello bot",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "d",  # Rocket.Chat code for direct
    }

    await adapter._on_inbound(dm_event)

    assert len(handled_events) == 1
    event = handled_events[0]
    assert event.chat_id == "dm-room-1"
    assert event.chat_type == "dm"
    assert event.user_id == "alice"


@pytest.mark.asyncio
async def test_adapter_inbound_text_attachment_sets_not_inlined_flag():
    """text/* attachments must reach the event flagged as NOT inlined."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
    )

    adapter = RocketChatAdapter(cfg)
    adapter._client = FakeClient()
    adapter._connected = True
    adapter.handle_message = lambda event: None  # type: ignore[method-assign]

    dm_event = {
        "_id": "dm-msg-txt",
        "rid": "dm-room-1",
        "msg": "see attachment",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "d",
        "files": [
            {
                "_id": "f-txt-1",
                "name": "notes.txt",
                "type": "text/plain",
            },
        ],
    }

    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]
    await adapter._on_inbound(dm_event)

    assert len(handled) == 1
    event = handled[0]
    assert event.media_urls == [
        "https://chat.example.com/file-upload/dm-room-1/f-txt-1/notes.txt"
    ]
    assert event.media_text_inlined == [False]


@pytest.mark.asyncio
async def test_adapter_channel_without_mention_is_ignored():
    """Channel messages without bot mention should not be forwarded."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        mention_names=["hermes"],
    )

    adapter = RocketChatAdapter(cfg)
    adapter._client = FakeClient()
    adapter._connected = True

    handled_events = []

    async def fake_handle(event):
        handled_events.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    channel_event = {
        "_id": "ch-msg-1",
        "rid": "room-1",
        "msg": "random chat about nothing",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "c",
    }

    await adapter._on_inbound(channel_event)

    assert len(handled_events) == 0


@pytest.mark.asyncio
async def test_adapter_channel_with_mention_is_forwarded():
    """Channel messages with bot mention should be forwarded."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        mention_names=["hermes"],
    )

    adapter = RocketChatAdapter(cfg)
    client = FakeClient()
    # Initialize identity so bot_username is known for mention matching
    await client.initialize()
    adapter._client = client
    adapter._connected = True

    handled_events = []

    async def fake_handle(event):
        handled_events.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    channel_event = {
        "_id": "ch-msg-2",
        "rid": "room-1",
        "msg": "@hermesbot hello",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "c",
    }

    await adapter._on_inbound(channel_event)

    assert len(handled_events) == 1


@pytest.mark.asyncio
async def test_adapter_thread_reply_targeting():
    """Messages in a thread should set reply_to_message_id for the outbound reply."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
    )

    adapter = RocketChatAdapter(cfg)
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    handled_events = []

    async def fake_handle(event):
        handled_events.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    # A DM with tmid (threaded)
    threaded_event = {
        "_id": "thread-msg-1",
        "rid": "dm-room-1",
        "msg": "follow up",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "tmid": "parent-msg-1",
        "mentions": [],
        "_room_type": "d",
    }

    await adapter._on_inbound(threaded_event)

    assert len(handled_events) == 1
    event = handled_events[0]
    # When tmid is present, reply_to_message_id should use tmid
    assert event.reply_to_message_id == "parent-msg-1"


# ---------------------------------------------------------------------------
# Chat info tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_info_returns_room_metadata():
    """get_chat_info should return best-effort room metadata."""
    adapter = RocketChatAdapter(RocketChatConfig())

    # Seed some room info
    adapter._room_info["room-1"] = {
        "name": "general",
        "type": "channel",
        "topic": "General discussion",
    }

    info = await adapter.get_chat_info("room-1")

    assert info is not None
    assert info["name"] == "general"
    assert info["type"] == "channel"


@pytest.mark.asyncio
async def test_get_chat_info_unknown_room_returns_empty():
    """get_chat_info for an unknown room returns a minimal dict."""
    adapter = RocketChatAdapter(RocketChatConfig())

    info = await adapter.get_chat_info("unknown-room")

    assert info == {}


# ---------------------------------------------------------------------------
# build_source tests
# ---------------------------------------------------------------------------


def test_build_source_dm():
    """build_source should construct a proper source dict for a DM."""
    adapter = RocketChatAdapter(RocketChatConfig())
    source = adapter.build_source(
        chat_id="dm-1",
        chat_type="dm",
        user_id="alice",
        user_name="Alice",
        room_type="direct",
        room_name="Alice",
    )
    assert source["chat_id"] == "dm-1"
    assert source["chat_type"] == "dm"
    assert source["user_id"] == "alice"
    assert source["user_name"] == "Alice"


def test_build_source_channel():
    """build_source should include channel metadata."""
    adapter = RocketChatAdapter(RocketChatConfig())
    source = adapter.build_source(
        chat_id="room-1",
        chat_type="channel",
        user_id="alice",
        user_name="Alice",
        room_type="channel",
        room_name="general",
    )
    assert source["chat_type"] == "channel"
    assert source["room_name"] == "general"


# ---------------------------------------------------------------------------
# Persistent seen-id dedup tests
# ---------------------------------------------------------------------------


def test_seen_id_store_dedup_second_call_suppressed(tmp_path):
    """The same message id should be seen on first mark and suppressed after."""
    store_path = str(tmp_path / "seen.json")
    store = PersistentSeenIdStore(path=store_path, ttl_seconds=3600)
    assert not store.contains("msg-1")
    store.mark("msg-1")
    assert store.contains("msg-1")


def test_seen_id_store_persists_across_instances(tmp_path):
    """A new store loading the same file should remember previously marked ids."""
    store_path = str(tmp_path / "seen.json")
    store1 = PersistentSeenIdStore(path=store_path, ttl_seconds=3600)
    store1.mark("msg-persist")
    store1.flush()

    store2 = PersistentSeenIdStore(path=store_path, ttl_seconds=3600)
    assert store2.contains("msg-persist")


def test_seen_id_store_ttl_expires_old_entries(tmp_path):
    """Entries older than ttl should be treated as not-seen."""
    import time

    store_path = str(tmp_path / "seen.json")
    store = PersistentSeenIdStore(path=store_path, ttl_seconds=1.0)
    store.mark("msg-old")
    # Backdate the entry so it is older than ttl
    store._seen["msg-old"] = time.time() - 10
    store.flush()

    store2 = PersistentSeenIdStore(path=store_path, ttl_seconds=1.0)
    assert not store2.contains("msg-old")


def test_seen_id_store_empty_id_not_deduped(tmp_path):
    """An empty message id should never be reported as seen (no false positives)."""
    store = PersistentSeenIdStore(path=str(tmp_path / "seen.json"), ttl_seconds=3600)
    assert not store.contains("")
    store.mark("")
    assert not store.contains("")


def test_seen_id_store_atomic_write(tmp_path):
    """flush() should write a valid JSON file (no partial/corrupt files)."""
    import json

    store_path = str(tmp_path / "seen.json")
    store = PersistentSeenIdStore(path=store_path, ttl_seconds=3600)
    store.mark("m1")
    store.mark("m2")
    store.flush()
    with open(store_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert "m1" in data and "m2" in data


def test_dedup_disabled_for_polling_transport():
    """The seen-id store should not be created under polling transport."""
    cfg = RocketChatConfig(transport="polling", dedup_enabled=True)
    adapter = RocketChatAdapter(cfg)
    assert adapter._seen_id_store is None


def test_dedup_enabled_for_websocket_transport(tmp_path):
    """The seen-id store should be created under websocket transport."""
    cfg = RocketChatConfig(
        transport="websocket",
        dedup_enabled=True,
        dedup_store_path=str(tmp_path / "seen.json"),
    )
    adapter = RocketChatAdapter(cfg)
    assert adapter._seen_id_store is not None


def test_dedup_disabled_when_flag_off():
    """dedup_enabled=False should skip the store even for websocket."""
    cfg = RocketChatConfig(transport="websocket", dedup_enabled=False)
    adapter = RocketChatAdapter(cfg)
    assert adapter._seen_id_store is None


@pytest.mark.asyncio
async def test_on_inbound_dedup_suppresses_replayed_message(tmp_path):
    """A second _on_inbound call with the same _id should not reach handle_message."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        transport="websocket",
        dedup_enabled=True,
        dedup_store_path=str(tmp_path / "seen.json"),
    )
    adapter = RocketChatAdapter(cfg)
    adapter._client = FakeClient()
    adapter._connected = True

    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    dm_event = {
        "_id": "replay-msg-1",
        "rid": "dm-room-1",
        "msg": "hello bot",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "d",
    }

    # First delivery: should be handled
    await adapter._on_inbound(dm_event)
    assert len(handled) == 1

    # Replay (e.g. after a reconnect): should be suppressed
    await adapter._on_inbound(dm_event)
    assert len(handled) == 1


@pytest.mark.asyncio
async def test_on_inbound_dedup_persists_across_adapter_restart(tmp_path):
    """A replayed message should be suppressed even after the adapter is rebuilt.

    This mirrors the real-world bug: gateway restart → WebSocket resubscribes
    → server replays recent unread messages → bot answers twice.
    """
    store_path = str(tmp_path / "seen.json")
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        transport="websocket",
        dedup_enabled=True,
        dedup_store_path=store_path,
    )

    handled1 = []
    adapter1 = RocketChatAdapter(cfg)
    adapter1._client = FakeClient()
    adapter1._connected = True

    async def fake_handle1(event):
        handled1.append(event)

    adapter1.handle_message = fake_handle1  # type: ignore[method-assign]

    dm_event = {
        "_id": "persist-replay-1",
        "rid": "dm-room-1",
        "msg": "audit q2 code",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "d",
    }
    await adapter1._on_inbound(dm_event)
    assert len(handled1) == 1

    # Simulate a gateway restart: brand-new adapter, same store path
    handled2 = []
    adapter2 = RocketChatAdapter(cfg)
    adapter2._client = FakeClient()
    adapter2._connected = True

    async def fake_handle2(event):
        handled2.append(event)

    adapter2.handle_message = fake_handle2  # type: ignore[method-assign]

    # Server replays the same message after restart — must be suppressed
    await adapter2._on_inbound(dm_event)
    assert len(handled2) == 0


@pytest.mark.asyncio
async def test_inbound_room_type_fallback_uses_room_info():
    """Messages from rooms unknown to the transport resolve their type via
    rooms.info, so new DMs are not mis-gated as channels; the resolved room
    metadata also feeds get_chat_info()."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
    )
    adapter = RocketChatAdapter(cfg)
    client = FakeClient()
    adapter._client = client
    adapter._connected = True
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    dm_event = {
        "_id": "dm-no-type",
        "rid": "dm-room-new",
        "msg": "hello",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        # NO _room_type: simulates a room the transport never subscribed.
    }

    await adapter._on_inbound(dm_event)

    assert len(handled) == 1
    assert handled[0].chat_type == "dm"  # not mis-gated as a channel
    assert client.room_info_calls == ["dm-room-new"]
    # The cached room metadata now powers get_chat_info().
    info = await adapter.get_chat_info("dm-room-new")
    assert info.get("t") == "d"
    assert info.get("name") == "DMs"


@pytest.mark.asyncio
async def test_inbound_room_type_fallback_caches_missing_rooms():
    """Unknown rooms are only probed once (negative caching)."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
    )
    adapter = RocketChatAdapter(cfg)
    client = FakeClient()
    adapter._client = client
    adapter._connected = True
    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    base_event = {
        "rid": "room-X",
        "msg": "hi",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
    }
    first = dict(base_event, _id="m1")
    second = dict(base_event, _id="m2")
    await adapter._on_inbound(first)
    await adapter._on_inbound(second)

    assert client.room_info_calls == ["room-X"]


@pytest.mark.asyncio
async def test_force_thread_anchors_reply_to_triggering_message():
    """ROCKETCHAT_FORCE_THREAD=true must anchor replies in the trigger thread."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        force_thread=True,
    )
    adapter = RocketChatAdapter(cfg)
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    result = await adapter.send(chat_id="room-1", content="reply", reply_to="m-trigger")

    assert result.success
    assert client.post_message_calls[-1]["tmid"] == "m-trigger"


@pytest.mark.asyncio
async def test_force_thread_default_does_not_thread_channel_replies():
    """Without force_thread, plain channel replies are NOT thread-anchored."""
    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        auth_mode="token",
        user_id="u1",
        access_token="tok",
        force_thread=False,
    )
    adapter = RocketChatAdapter(cfg)
    client = FakeClient()
    adapter._client = client
    adapter._connected = True

    await adapter.send(
        chat_id="room-1", content="reply", reply_to="m-trigger", metadata={}
    )

    assert client.post_message_calls[-1]["tmid"] == ""


# ---------------------------------------------------------------------------
# v0.2.1/v0.3.0: MessageEvent contract compat — the media_text_inlined kwarg
# exists only on newer Hermes base classes (upstream 00394acfae); older
# installs reject it and would drop every inbound message with a TypeError.
# ---------------------------------------------------------------------------


import dataclasses as _dc


@_dc.dataclass
class _OldStyleMessageEvent:
    """Hermes MessageEvent before the media_text_inlined contract."""

    text: str = ""
    message_type: object = None
    source: object = None
    raw_message: object = None
    message_id: str = ""
    media_urls: list = _dc.field(default_factory=list)
    media_types: list = _dc.field(default_factory=list)
    reply_to_message_id: str = ""
    reply_to_text: str = ""
    reply_to_author_id: str = ""
    reply_to_author_name: str = ""
    reply_to_is_own_message: bool = False


def test_build_message_event_adapts_to_messageevent_without_inlined(monkeypatch):
    """Older MessageEvent classes reject media_text_inlined; building an
    inbound event against one must not raise (production crash on a Hermes
    install whose base lacks the field — server HEAD d63f996a75)."""
    from types import SimpleNamespace

    import adapter as adapter_module

    adapter = RocketChatAdapter(
        RocketChatConfig(server_url="https://chat.example.com")
    )
    adapter._client = FakeClient()
    adapter._connected = True
    monkeypatch.setattr(adapter_module, "MessageEvent", _OldStyleMessageEvent)

    source = SimpleNamespace(
        chat_id="dm-room-1", chat_type="dm", user_id="alice", user_name="alice"
    )
    event = adapter._build_message_event(
        source=source,
        raw_event={"_id": "m-1", "u": {"_id": "alice", "username": "alice"}, "msg": "hi"},
        text="hi",
        media_urls=[],
        media_types=[],
        reply_to="parent-1",
        reply_context={
            "text": "parent text",
            "author_id": "alice",
            "author_name": "alice",
        },
        media_text_inlined=[None],
    )

    assert event.message_id == "m-1"
    assert event.text == "hi"
    assert event.reply_to_message_id == "parent-1"
    assert event.reply_to_text == "parent text"
    assert event.reply_to_author_name == "alice"
    assert event.chat_id == "dm-room-1"
