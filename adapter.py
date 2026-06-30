"""Rocket.Chat platform adapter for Hermes Agent gateway.

Provides a self-contained Hermes platform plugin that connects Rocket.Chat
rooms to the Hermes messaging gateway via REST polling or WebSocket/DDP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RocketChatConfig:
    """Parsed Rocket.Chat platform configuration."""

    server_url: str = ""
    auth_mode: str = "token"
    user_id: str = ""
    access_token: str = ""
    username: str = ""
    password: str = ""
    transport: str = "polling"
    poll_interval_seconds: float = 3.0
    mention_names: list[str] = field(default_factory=list)
    force_thread: bool = False
    home_channel: str = ""
    media_cache_dir: str = ""
    allowed_users: list[str] = field(default_factory=list)
    allow_all: bool = False
    max_message_length: int = 4000


def _parse_bool(value: str | None) -> bool:
    """Parse a boolean from an environment variable string."""
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_float_safe(value: Any, default: float = 0.0) -> float:
    """Parse a float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int_safe(value: Any, default: int = 0) -> int:
    """Parse an int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_csv(value: str | None) -> list[str]:
    """Parse a comma-separated value into a list of trimmed non-empty strings."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_config(extra: dict[str, Any] | None = None) -> RocketChatConfig:
    """Parse Rocket.Chat configuration from environment and extra dict.

    Extra values override environment variables for explicitly-set keys.
    """
    if extra is None:
        extra = {}

    def _get(key: str, default: Any = "") -> str:
        return str(extra.get(key) or os.environ.get(f"ROCKETCHAT_{key.upper()}", default))

    cfg = RocketChatConfig(
        server_url=_get("server_url"),
        auth_mode=_get("auth_mode", "token"),
        user_id=_get("user_id"),
        access_token=_get("access_token"),
        username=_get("username"),
        password=_get("password"),
        transport=_get("transport", "polling"),
        poll_interval_seconds=_parse_float_safe(extra.get("poll_interval_seconds") or os.environ.get("ROCKETCHAT_POLL_INTERVAL_SECONDS", "3"), 3.0),
        mention_names=_parse_csv(extra.get("mention_names") or os.environ.get("ROCKETCHAT_MENTION_NAMES", "")),
        force_thread=_parse_bool(extra.get("force_thread") or os.environ.get("ROCKETCHAT_FORCE_THREAD")),
        home_channel=_get("home_channel"),
        media_cache_dir=extra.get("media_cache_dir") or os.environ.get("ROCKETCHAT_MEDIA_CACHE_DIR", ""),
        allowed_users=_parse_csv(extra.get("allowed_users") or os.environ.get("ROCKETCHAT_ALLOWED_USERS", "")),
        allow_all=_parse_bool(extra.get("allow_all") or os.environ.get("ROCKETCHAT_ALLOW_ALL_USERS")),
        max_message_length=_parse_int_safe(extra.get("max_message_length") or os.environ.get("ROCKETCHAT_MAX_MESSAGE_LENGTH", "4000"), 4000),
    )

    return cfg


def env_enablement() -> dict[str, Any] | None:
    """Return an enablement seed dict when minimal auth config is present.

    Returns None when required environment variables are missing, signaling
    that the platform should not be auto-enabled.
    """
    cfg = parse_config()

    if not cfg.server_url:
        return None

    # Token mode: need user_id + access_token
    if cfg.auth_mode == "token":
        if not cfg.user_id or not cfg.access_token:
            return None
    # Password mode: need username + password
    elif cfg.auth_mode == "password":
        if not cfg.username or not cfg.password:
            return None
    else:
        return None

    seed: dict[str, Any] = {
        "server_url": cfg.server_url,
        "auth_mode": cfg.auth_mode,
    }

    if cfg.home_channel:
        seed["home_channel"] = {"chat_id": cfg.home_channel}

    if cfg.allow_all:
        seed["allow_all"] = True
        seed["allowed_users"] = []
    elif cfg.allowed_users:
        seed["allowed_users"] = cfg.allowed_users

    return seed


# ---------------------------------------------------------------------------
# Mention gating
# ---------------------------------------------------------------------------


def should_handle_message(
    room_type: str,
    text: str,
    mentions: list[str],
    bot_user_id: str,
    bot_username: str,
    mention_names: list[str] | None = None,
) -> bool:
    """Decide whether an inbound Rocket.Chat message should be dispatched to Hermes.

    - Direct messages always pass through.
    - Group/channel messages require an explicit mention of the bot username
      or a configured alias, either via Rocket.Chat mention metadata or via
      an @-mention in the message text.
    """
    if mention_names is None:
        mention_names = []

    # DMs always pass through
    if room_type == "direct":
        return True

    # Build the set of trigger names: bot username + configured aliases
    triggers: set[str] = set()
    if bot_username:
        triggers.add(bot_username.lower())
    for alias in mention_names:
        if alias:
            triggers.add(alias.lower())

    if not triggers:
        return False

    # Check Rocket.Chat mention metadata (array of usernames)
    for m in mentions:
        if m.strip().lower() in triggers:
            return True

    # Check text for @mention patterns
    text_lower = text.lower()
    for trigger in triggers:
        if _has_token_mention(text_lower, trigger):
            return True

    return False


def _has_token_mention(text_lower: str, token: str) -> bool:
    """Check if text contains @token as a whole-word mention.

    A mention is a contiguous token preceded by @ and bounded by whitespace,
    punctuation, or string boundaries.
    """
    import re

    pattern = r"(?<![\w])@" + re.escape(token) + r"(?![\w])"
    return bool(re.search(pattern, text_lower))
