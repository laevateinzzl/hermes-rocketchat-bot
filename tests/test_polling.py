"""Tests for Rocket.Chat polling inbound transport."""

import pytest

from adapter import InMemoryCheckpointStore, PollingTransport


# ---------------------------------------------------------------------------
# Checkpoint store tests
# ---------------------------------------------------------------------------


def test_checkpoint_starts_empty():
    store = InMemoryCheckpointStore()
    assert store.get("room1") == 0


def test_checkpoint_save_and_get():
    store = InMemoryCheckpointStore()
    store.save("room1", "2024-01-01T00:00:00.000Z")
    assert store.get("room1") == "2024-01-01T00:00:00.000Z"


def test_checkpoint_update():
    store = InMemoryCheckpointStore()
    store.save("room1", "a")
    store.save("room1", "b")
    assert store.get("room1") == "b"


# ---------------------------------------------------------------------------
# Fake client for polling tests
# ---------------------------------------------------------------------------


class FakePollingClient:
    """A fake client that returns canned subscription and message data."""

    def __init__(
        self,
        subscriptions=None,
        sync_messages=None,
    ):
        self._subscriptions = subscriptions or []
        self._sync_messages = sync_messages or {}
        self.subscriptions_calls: list[dict] = []
        self.sync_calls: list[dict] = []
        self.server_url = "https://chat.example.com"
        self._user_id = "bot1"
        self._username = "hermesbot"
        self._access_token = "tok"

    async def list_subscriptions(self, updated_since=None):
        self.subscriptions_calls.append({"updated_since": updated_since})
        return self._subscriptions

    async def sync_messages(self, room_id, last_update=None):
        self.sync_calls.append({"room_id": room_id, "last_update": last_update})
        key = room_id
        if callable(self._sync_messages):
            return self._sync_messages(room_id, last_update)
        return self._sync_messages.get(key, {"updated": [], "removed": []})


# ---------------------------------------------------------------------------
# PollOnce tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_initializes_checkpoint_without_replaying():
    """First poll should store checkpoints but not emit old messages."""
    client = FakePollingClient(
        subscriptions=[
            {
                "_id": "room1",
                "t": "d",
                "name": "alice",
                "_updatedAt": "2024-01-01T00:01:00.000Z",
            }
        ],
        sync_messages={
            "room1": {
                "updated": [
                    {
                        "_id": "old-msg-1",
                        "rid": "room1",
                        "msg": "hello",
                        "u": {"_id": "alice", "username": "alice"},
                        "ts": "2024-01-01T00:00:00.000Z",
                    }
                ],
                "removed": [],
            }
        },
    )

    transport = PollingTransport(client)
    events = await transport.poll_once()

    # Should NOT emit messages on first poll — just set checkpoints
    assert len(events) == 0
    # Should have recorded checkpoints
    assert transport.checkpoint_store.get("room1") != 0


@pytest.mark.asyncio
async def test_poll_skips_bot_messages():
    """Messages authored by the bot should be skipped."""
    client = FakePollingClient(
        subscriptions=[],
        sync_messages={
            "room1": {
                "updated": [
                    {
                        "_id": "bot-msg-1",
                        "rid": "room1",
                        "msg": "I am a bot",
                        "u": {"_id": "bot1", "username": "hermesbot"},
                        "ts": "2024-01-01T00:00:00.000Z",
                    }
                ],
                "removed": [],
            }
        },
    )

    transport = PollingTransport(client)
    # Manually advance checkpoint so we see these messages
    transport.checkpoint_store.save("room1", "2023-01-01T00:00:00.000Z")

    events = await transport.poll_once()
    assert len(events) == 0  # bot message skipped


@pytest.mark.asyncio
async def test_poll_skips_system_messages():
    """Rocket.Chat system messages (t=system) should be skipped."""
    client = FakePollingClient(
        subscriptions=[],
        sync_messages={
            "room1": {
                "updated": [
                    {
                        "_id": "sys-msg-1",
                        "rid": "room1",
                        "msg": "user joined",
                        "u": {"_id": "alice", "username": "alice"},
                        "t": "uj",  # user-joined system message
                        "ts": "2024-01-01T00:00:00.000Z",
                    }
                ],
                "removed": [],
            }
        },
    )

    transport = PollingTransport(client)
    transport.checkpoint_store.save("room1", "2023-01-01T00:00:00.000Z")

    events = await transport.poll_once()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_poll_emits_user_message():
    """Normal user messages should be emitted as inbound events."""
    client = FakePollingClient(
        subscriptions=[
            {
                "_id": "room1",
                "t": "d",
                "name": "alice",
                "_updatedAt": "2024-01-01T00:01:00.000Z",
            }
        ],
        sync_messages={
            "room1": {
                "updated": [
                    {
                        "_id": "user-msg-1",
                        "rid": "room1",
                        "msg": "hello bot",
                        "u": {"_id": "alice", "username": "alice"},
                        "ts": "2024-01-01T00:00:10.000Z",
                    }
                ],
                "removed": [],
            }
        },
    )

    transport = PollingTransport(client)
    transport.checkpoint_store.save("room1", "2023-01-01T00:00:00.000Z")

    events = await transport.poll_once()
    assert len(events) == 1
    assert events[0]["_id"] == "user-msg-1"
    assert events[0]["msg"] == "hello bot"


@pytest.mark.asyncio
async def test_poll_deduplicates_seen_ids():
    """Already-seen message IDs should not be emitted again."""
    client = FakePollingClient(
        subscriptions=[],
        sync_messages={
            "room1": {
                "updated": [
                    {
                        "_id": "dup-msg-1",
                        "rid": "room1",
                        "msg": "hello again",
                        "u": {"_id": "alice", "username": "alice"},
                        "ts": "2024-01-01T00:00:10.000Z",
                    }
                ],
                "removed": [],
            }
        },
    )

    transport = PollingTransport(client)
    transport.checkpoint_store.save("room1", "2023-01-01T00:00:00.000Z")
    transport._seen_ids.add("dup-msg-1")  # already seen

    events = await transport.poll_once()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_poll_updates_checkpoint_after_poll():
    """After a successful poll, the checkpoint should advance."""
    client = FakePollingClient(
        subscriptions=[
            {
                "_id": "room1",
                "t": "d",
                "name": "alice",
                "_updatedAt": "2024-01-01T00:02:00.000Z",
            }
        ],
        sync_messages={
            "room1": {
                "updated": [
                    {
                        "_id": "msg-1",
                        "rid": "room1",
                        "msg": "hi",
                        "u": {"_id": "alice", "username": "alice"},
                        "ts": "2024-01-01T00:01:30.000Z",
                    }
                ],
                "removed": [],
            }
        },
    )

    transport = PollingTransport(client)
    transport.checkpoint_store.save("room1", "2023-01-01T00:00:00.000Z")

    await transport.poll_once()
    # Checkpoint should be the subscription's _updatedAt
    assert transport.checkpoint_store.get("room1") == "2024-01-01T00:02:00.000Z"
