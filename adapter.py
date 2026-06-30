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


# ---------------------------------------------------------------------------
# Rocket.Chat REST client
# ---------------------------------------------------------------------------


@dataclass
class RocketChatIdentity:
    """Identity information returned after authentication."""

    user_id: str = ""
    username: str = ""
    name: str = ""
    auth_token: str = ""


class RocketChatClientError(Exception):
    """Raised when a Rocket.Chat API call fails."""


class RocketChatClient:
    """Async REST client for the Rocket.Chat API.

    Handles authentication (token or password), message sending,
    attachment download, and file upload.
    """

    def __init__(
        self,
        server_url: str,
        user_id: str = "",
        access_token: str = "",
        username: str = "",
        password: str = "",
    ):
        self._server_url = server_url.rstrip("/")
        self._user_id = user_id
        self._access_token = access_token
        self._username = username
        self._password = password
        self._identity: RocketChatIdentity | None = None

    @property
    def identity(self) -> RocketChatIdentity | None:
        return self._identity

    @property
    def server_url(self) -> str:
        return self._server_url

    # -- subclasses / tests override this to inject fake HTTP sessions --------

    async def _get_session(self):
        """Return an aiohttp ClientSession (lazy import to keep deps optional)."""
        try:
            import aiohttp
            return aiohttp.ClientSession()
        except ImportError:
            import httpx
            return httpx.AsyncClient()

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        headers: dict | None = None,
        raw: bool = False,
    ):
        """Make an HTTP request to the Rocket.Chat REST API."""
        url = f"{self._server_url}{path}"
        default_headers = {
            "X-User-Id": self._user_id,
            "X-Auth-Token": self._access_token,
        }
        if headers:
            default_headers.update(headers)

        session = await self._get_session()

        # Support both aiohttp and httpx session shapes
        if hasattr(session, "request"):
            # httpx.AsyncClient
            async with session:
                resp = await session.request(
                    method, url, json=json, headers=default_headers
                )
                if raw:
                    return await resp.aread() if hasattr(resp, "aread") else await resp.read()
                return await resp.json()
        else:
            # aiohttp.ClientSession
            async with session.request(
                method, url, json=json, headers=default_headers
            ) as resp:
                if raw:
                    return await resp.read()
                return await resp.json()

    # -- authentication ------------------------------------------------------

    async def initialize(self) -> RocketChatIdentity:
        """Authenticate and return the bot identity."""
        if self._username and self._password:
            return await self._login_password()
        else:
            return await self._verify_token()

    async def _verify_token(self) -> RocketChatIdentity:
        """Verify a pre-configured user ID + access token via /api/v1/me."""
        try:
            data = await self._request("GET", "/api/v1/me")
        except Exception as exc:
            raise RocketChatClientError(f"Token verification failed: {exc}") from exc

        if not data.get("success", False) and not data.get("_id"):
            raise RocketChatClientError("Token verification failed: invalid response")

        identity = RocketChatIdentity(
            user_id=data.get("_id", self._user_id),
            username=data.get("username", ""),
            name=data.get("name", ""),
            auth_token=self._access_token,
        )
        self._identity = identity
        return identity

    async def _login_password(self) -> RocketChatIdentity:
        """Authenticate with username + password via /api/v1/login."""
        try:
            data = await self._request(
                "POST",
                "/api/v1/login",
                json={"user": self._username, "password": self._password},
                headers={},  # no auth headers yet
            )
        except Exception as exc:
            raise RocketChatClientError(f"Password login failed: {exc}") from exc

        if data.get("status") != "success":
            msg = data.get("message", data.get("error", "unknown error"))
            raise RocketChatClientError(f"Password login failed: {msg}")

        auth_data = data.get("data", data)
        self._user_id = auth_data.get("userId", "")
        self._access_token = auth_data.get("authToken", "")

        identity = RocketChatIdentity(
            user_id=self._user_id,
            username=self._username,
            name="",
            auth_token=self._access_token,
        )
        self._identity = identity
        return identity

    # -- send ----------------------------------------------------------------

    async def post_message(
        self,
        room_id: str,
        text: str,
        tmid: str = "",
    ) -> dict:
        """Send a chat message via /api/v1/chat.postMessage."""
        payload: dict[str, Any] = {
            "roomId": room_id,
            "text": text,
        }
        if tmid:
            payload["tmid"] = tmid

        data = await self._request("POST", "/api/v1/chat.postMessage", json=payload)

        if not data.get("success", False):
            msg = data.get("error", "chat.postMessage failed")
            raise RocketChatClientError(f"Send failed: {msg}")

        return data.get("message", {})

    # -- download ------------------------------------------------------------

    async def download_attachment(self, url: str) -> bytes:
        """Download a protected Rocket.Chat file attachment with auth headers."""
        headers = {
            "X-User-Id": self._user_id,
            "X-Auth-Token": self._access_token,
        }
        session = await self._get_session()

        if hasattr(session, "request"):
            # httpx.AsyncClient
            async with session:
                resp = await session.get(url, headers=headers)
                resp.raise_for_status()
                return await resp.aread() if hasattr(resp, "aread") else await resp.read()
        else:
            # aiohttp.ClientSession
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.read()


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------


@dataclass
class AttachmentCandidate:
    """Normalized attachment extracted from a Rocket.Chat message."""

    url: str = ""
    mime_type: str = ""
    title: str = ""
    rc_file_id: str = ""


def attachment_candidates_from_message(message: dict) -> list[AttachmentCandidate]:
    """Extract attachment candidates from a Rocket.Chat message.

    Handles three Rocket.Chat attachment shapes:
    - ``attachments``: array of rich attachment objects with ``image_url`` or ``title_link``
    - ``file``: single file object with ``_id``, ``name``, ``type``
    - ``files``: array of file objects
    """
    candidates: list[AttachmentCandidate] = []

    # attachments field (rich link previews / image embeds)
    for att in message.get("attachments") or []:
        url = att.get("image_url") or att.get("title_link") or ""
        if url:
            candidates.append(
                AttachmentCandidate(
                    url=url,
                    mime_type=att.get("image_type", ""),
                    title=att.get("title", ""),
                )
            )

    # files field (array, modern Rocket.Chat)
    for f in message.get("files") or []:
        candidates.append(
            AttachmentCandidate(
                url=_file_url_from_rc(f, message),
                mime_type=f.get("type", ""),
                title=f.get("name", ""),
                rc_file_id=f.get("_id", ""),
            )
        )

    # file field (single object, older Rocket.Chat)
    single = message.get("file")
    if single:
        candidates.append(
            AttachmentCandidate(
                url=_file_url_from_rc(single, message),
                mime_type=single.get("type", ""),
                title=single.get("name", ""),
                rc_file_id=single.get("_id", ""),
            )
        )

    return candidates


def _file_url_from_rc(file_obj: dict, message: dict) -> str:
    """Build a Rocket.Chat file download URL from a file object."""
    rid = message.get("rid", "")
    fid = file_obj.get("_id", "")
    if fid and rid:
        return f"/file-upload/{rid}/{fid}/{file_obj.get('name', 'file')}"
    return ""


def classify_attachment(candidate: AttachmentCandidate) -> str:
    """Map an attachment candidate to a Hermes media type string.

    Returns one of: ``"image"``, ``"document"``, ``"video"``, ``"audio"``.
    """
    mime = candidate.mime_type.lower()

    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"

    # Fallback: classify by file extension
    from pathlib import Path
    ext = Path(candidate.title).suffix.lower() if candidate.title else ""

    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff"}
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"}
    audio_exts = {".mp3", ".ogg", ".wav", ".flac", ".aac", ".m4a", ".wma", ".opus"}

    if ext in image_exts:
        return "image"
    if ext in video_exts:
        return "video"
    if ext in audio_exts:
        return "audio"

    return "document"


def sanitize_filename(name: str) -> str:
    """Sanitize a filename to be safe for local filesystem storage."""
    import re

    name = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.lstrip(".") or "file"
    return name


async def resolve_message_media(
    message: dict,
    client: Any,
    cache_dir: str = "",
) -> tuple[list[str], list[str]]:
    """Download attachments into cache and return (media_urls, media_types).

    Returns two parallel lists suitable for Hermes ``MessageEvent`` fields.
    """
    candidates = attachment_candidates_from_message(message)
    if not candidates:
        return [], []

    media_urls: list[str] = []
    media_types: list[str] = []

    for candidate in candidates:
        media_type = classify_attachment(candidate)
        local_path = ""

        if cache_dir and candidate.url:
            safe_name = sanitize_filename(candidate.title or "attachment")
            from pathlib import Path
            dest = Path(cache_dir) / safe_name
            dest.parent.mkdir(parents=True, exist_ok=True)

            try:
                data = await client.download_attachment(candidate.url)
                dest.write_bytes(data)
                local_path = str(dest)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to download attachment: %s", candidate.url
                )
                continue
        elif candidate.url:
            local_path = candidate.url
        else:
            continue

        media_urls.append(local_path)
        media_types.append(media_type)

    return media_urls, media_types
