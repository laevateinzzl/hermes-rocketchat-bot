"""Tests for mention gating and message filtering."""

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
