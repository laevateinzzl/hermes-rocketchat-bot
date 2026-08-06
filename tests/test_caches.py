"""Tests for bounded caches (v0.2 P1.1).

Mirrors Hermes' Slack eviction work (533e54123 / d42b29579 / 91693f9d4):
per-room/per-message tracking structures must not grow without bound.  The
adapter's room-info, placeholder, stream-preview and status-text stores all
use oldest-first eviction.
"""

import pytest  # type: ignore[reportMissingImports]

from adapter import BoundedDict, RocketChatAdapter, RocketChatConfig


def test_bounded_dict_evicts_oldest():
    store = BoundedDict(maxsize=3)
    store["a"] = 1
    store["b"] = 2
    store["c"] = 3
    store["d"] = 4

    assert list(store.keys()) == ["b", "c", "d"]
    assert "a" not in store


def test_bounded_dict_reinsertion_refreshes_position():
    store = BoundedDict(maxsize=3)
    store["a"] = 1
    store["b"] = 2
    store["c"] = 3
    store["a"] = 10  # re-set: moves to the end
    store["d"] = 4

    assert list(store.keys()) == ["c", "a", "d"]
    assert store["a"] == 10


def test_bounded_dict_min_maxsize():
    assert BoundedDict(maxsize=0).maxsize == 1


@pytest.mark.asyncio
async def test_adapter_stores_are_bounded():
    """All tracking stores must be bounded OrderedDicts with sane caps."""
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))

    assert isinstance(adapter._room_info, BoundedDict)
    assert adapter._room_info.maxsize > 0
    assert isinstance(adapter._typing_placeholders, BoundedDict)
    assert isinstance(adapter._stream_previews, BoundedDict)
    assert isinstance(adapter._last_status_text, BoundedDict)


@pytest.mark.asyncio
async def test_room_info_eviction_via_get_chat_info():
    """get_chat_info must miss after eviction and hit before it."""
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    small = BoundedDict(maxsize=5)
    adapter._room_info = small

    for i in range(5):
        small[f"room-{i}"] = {"name": f"Room {i}"}

    assert await adapter.get_chat_info("room-0") == {"name": "Room 0"}

    small["room-5"] = {"name": "Room 5"}  # evicts room-0

    assert await adapter.get_chat_info("room-0") == {}
    assert await adapter.get_chat_info("room-5") == {"name": "Room 5"}


@pytest.mark.asyncio
async def test_placeholder_bounded_under_load():
    """Many placeholder keys must not grow past the store cap."""
    adapter = RocketChatAdapter(RocketChatConfig(server_url="https://chat.example.com"))
    small = BoundedDict(maxsize=4)
    adapter._typing_placeholders = small

    for i in range(10):
        small[f"chat-{i}\u0000"] = f"msg-{i}"

    assert len(small) == 4
    assert "chat-0\u0000" not in small
    assert "chat-9\u0000" in small
