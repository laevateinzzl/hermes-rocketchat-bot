"""Tests for mention gating and message filtering."""

import pytest  # type: ignore[reportMissingImports]

from adapter import should_handle_message


def test_direct_message_does_not_require_mention():
    assert should_handle_message(
        room_type="direct",
        text="hello",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


def test_channel_requires_explicit_mention():
    assert not should_handle_message(
        room_type="channel",
        text="hello",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )

    assert should_handle_message(
        room_type="channel",
        text="@hermesbot hello",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


def test_channel_accepts_configured_alias():
    assert should_handle_message(
        room_type="group",
        text="@assistant summarize this",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=["assistant"],
    )


def test_group_dm_is_also_direct():
    assert should_handle_message(
        room_type="direct",
        text="hi",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


def test_mentions_field_triggers_for_bot_username():
    assert should_handle_message(
        room_type="channel",
        text="some message",
        mentions=["hermesbot"],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


def test_mentions_field_triggers_for_alias():
    assert should_handle_message(
        room_type="channel",
        text="some message",
        mentions=["assistant"],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=["assistant"],
    )


def test_case_insensitive_mention_match():
    assert should_handle_message(
        room_type="channel",
        text="@HERMESBOT hello",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


def test_no_false_positive_for_partial_username():
    """Ensure @hermesbot_extra does not match hermesbot."""
    assert not should_handle_message(
        room_type="channel",
        text="@hermesbot_extra hello",
        mentions=[],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


# ---------------------------------------------------------------------------
# v0.2 P2.1 — per-room mention override and ignore-other-mentions
# ---------------------------------------------------------------------------


def test_ignore_other_user_mentions_suppresses_mass_mention():
    """When enabled, mentions of other users alongside the bot suppress the wake."""
    assert not should_handle_message(
        room_type="channel",
        text="hi everyone",
        mentions=["hermesbot", "alice", "bob"],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
        ignore_other_user_mentions=True,
    )


def test_ignore_other_user_mentions_allows_direct_bot_mention():
    assert should_handle_message(
        room_type="channel",
        text="hi",
        mentions=["hermesbot"],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
        ignore_other_user_mentions=True,
    )


def test_ignore_other_user_mentions_off_responds_to_mass_mention():
    assert should_handle_message(
        room_type="channel",
        text="hi everyone",
        mentions=["hermesbot", "alice"],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
        ignore_other_user_mentions=False,
    )


def test_mention_dict_shape_handled():
    """Rocket.Chat may deliver mentions as {username: ...} objects, not strings."""
    assert should_handle_message(
        room_type="channel",
        text="hi",
        mentions=[{"username": "hermesbot"}],
        bot_user_id="bot1",
        bot_username="hermesbot",
        mention_names=[],
    )


def test_parse_config_reads_mention_overrides():
    from adapter import parse_config

    cfg = parse_config(
        {
            "always_respond_rooms": "room-a, room-b",
            "ignore_other_user_mentions": "true",
        }
    )
    assert cfg.always_respond_rooms == ["room-a", "room-b"]
    assert cfg.ignore_other_user_mentions


class _FakeClient:
    def __init__(self):
        self._identity = type("I", (), {"user_id": "bot1", "username": "hermesbot"})()

    @property
    def identity(self):
        return self._identity


@pytest.mark.asyncio
async def test_on_inbound_always_respond_room_bypasses_mention_gate():
    """A configured always-respond room delivers unmentioned messages."""
    from adapter import RocketChatAdapter, RocketChatConfig

    cfg = RocketChatConfig(
        server_url="https://chat.example.com",
        always_respond_rooms=["room-alpha"],
    )
    adapter = RocketChatAdapter(cfg)
    setattr(adapter, "_client", _FakeClient())
    adapter._connected = True

    handled = []

    async def fake_handle(event):
        handled.append(event)

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    event = {
        "_id": "msg-1",
        "rid": "room-alpha",
        "msg": "no mention here",
        "u": {"_id": "alice", "username": "alice"},
        "t": "",
        "mentions": [],
        "_room_type": "c",
    }

    await adapter._on_inbound(event)

    assert len(handled) == 1
