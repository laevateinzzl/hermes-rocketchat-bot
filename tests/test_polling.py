"""Tests for Rocket.Chat polling inbound transport."""

import time

import pytest

from adapter import (
    InMemoryCheckpointStore,
    PollingTransport,
    RocketChatClientError,
    RocketChatRateLimitError,
)


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

    async def sync_messages(self, room_id, last_update=None, room_type=None):
        self.sync_calls.append(
            {"room_id": room_id, "last_update": last_update, "room_type": room_type}
        )
        key = room_id
        if callable(self._sync_messages):
            return self._sync_messages(room_id, last_update)
        return self._sync_messages.get(key, {"updated": [], "removed": []})


# ---------------------------------------------------------------------------
# PollOnce tests
# ---------------------------------------------------------------------------


def test_poll_error_backoff_uses_rate_limit_retry_after():
    """Polling should respect Rocket.Chat's 429 wait time instead of hammering."""
    transport = PollingTransport(FakePollingClient(), poll_interval=3)
    error = RocketChatRateLimitError("rate limited", retry_after=27)

    assert transport._sleep_after_error(error) == 27


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
    transport._seen_ids["dup-msg-1"] = time.monotonic()  # already seen

    events = await transport.poll_once()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_poll_passes_room_type_to_sync_messages():
    """The REST client needs room type to choose history fallback endpoints."""
    client = FakePollingClient(
        subscriptions=[
            {
                "_id": "room1",
                "rid": "room1",
                "t": "p",
                "name": "private-room",
                "_updatedAt": "2024-01-01T00:02:00.000Z",
            }
        ],
        sync_messages={"room1": {"updated": [], "removed": []}},
    )

    transport = PollingTransport(client)
    transport.checkpoint_store.save("room1", "2023-01-01T00:00:00.000Z")

    await transport.poll_once()

    assert client.sync_calls[0] == {
        "room_id": "room1",
        "last_update": "2023-01-01T00:00:00.000Z",
        "room_type": "p",
    }


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


# ---------------------------------------------------------------------------
# 0.3.0-B: polling re-authentication on credential failure
# ---------------------------------------------------------------------------


class AuthFailingPollClient(FakePollingClient):
    """Fails auth once, then recovers — initialize() is a no-op success."""

    def __init__(self):
        super().__init__()
        self.initialize_calls = 0
        self.fail_first = True

    async def initialize(self):
        self.initialize_calls += 1
        return True

    async def list_subscriptions(self, updated_since=None):
        if self.fail_first:
            self.fail_first = False
            raise RocketChatClientError(
                "Token verification failed: HTTP 401: Unauthorized"
            )
        return []


@pytest.mark.asyncio
async def test_poll_reauthenticates_on_auth_failure():
    """A definitive 401 during polling must trigger one re-auth attempt."""
    import asyncio

    client = AuthFailingPollClient()
    fatal: list[str] = []
    transport = PollingTransport(
        client,
        poll_interval=0.01,
        on_auth_failure=lambda m: fatal.append(m),
    )
    transport._running = True

    task = asyncio.create_task(transport._poll_loop())
    await asyncio.sleep(0.1)
    transport._running = False
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert client.initialize_calls >= 1
    assert fatal == []  # re-auth succeeded → no fatal reported


@pytest.mark.asyncio
async def test_poll_reports_fatal_when_reauth_fails():
    """If re-authentication keeps failing, the fatal callback must fire."""
    import asyncio

    class AlwaysFailingClient(FakePollingClient):
        async def initialize(self):
            raise RocketChatClientError("HTTP 401: Unauthorized")

        async def list_subscriptions(self, updated_since=None):
            raise RocketChatClientError("HTTP 401: Unauthorized")

    client = AlwaysFailingClient()
    fatal: list[str] = []
    transport = PollingTransport(
        client,
        poll_interval=0.01,
        on_auth_failure=lambda m: fatal.append(m),
    )
    transport._running = True

    task = asyncio.create_task(transport._poll_loop())
    await asyncio.sleep(0.1)
    transport._running = False
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert len(fatal) >= 1
