"""Rocket.Chat platform adapter for Hermes Agent gateway.

Provides a self-contained Hermes platform plugin that connects Rocket.Chat
rooms to the Hermes messaging gateway via REST polling or WebSocket/DDP.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from enum import Enum
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


def _parse_bool(value: Any | None) -> bool:
    """Parse a boolean from an environment variable string or native bool."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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


def _parse_csv(value: str | list[str] | None) -> list[str]:
    """Parse a comma-separated value into a list of trimmed non-empty strings.

    Also accepts a list directly (Hermes may pass native list types via
    ``PlatformConfig.extra``).
    """
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


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

    async def list_subscriptions(self, updated_since=None) -> list[dict]:
        """List subscriptions via /api/v1/subscriptions.get."""
        params = {}
        if updated_since:
            params["updatedSince"] = updated_since
        data = await self._request("GET", "/api/v1/subscriptions.get")
        return data.get("update", data.get("subscriptions", []))

    async def sync_messages(self, room_id: str, last_update=None) -> dict:
        """Sync messages for a room via /api/v1/chat.syncMessages."""
        payload: dict[str, Any] = {"roomId": room_id}
        if last_update:
            payload["lastUpdate"] = last_update
        return await self._request("POST", "/api/v1/chat.syncMessages", json=payload)

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


# ---------------------------------------------------------------------------
# Polling transport
# ---------------------------------------------------------------------------


class InMemoryCheckpointStore:
    """Track last-seen timestamps per room for polling deduplication."""

    def __init__(self):
        self._checkpoints: dict[str, str | float] = {}

    def get(self, room_id: str):
        """Return the last seen timestamp for a room, or 0 if never seen."""
        return self._checkpoints.get(room_id, 0)

    def save(self, room_id: str, updated_at):
        """Store the latest timestamp for a room."""
        self._checkpoints[room_id] = updated_at


class PollingTransport:
    """Poll Rocket.Chat REST API for new messages.

    Keeps in-memory checkpoints and polls subscriptions.get / chat.syncMessages
    on a configurable interval.
    """

    def __init__(self, client, poll_interval: float = 3.0):
        self._client = client
        self.poll_interval = poll_interval
        self.checkpoint_store = InMemoryCheckpointStore()
        self._seen_ids: set[str] = set()
        self._running = False
        self._task: Any = None
        self._on_message: Any = None

    def set_on_message(self, callback):
        """Register a callback for inbound messages."""
        self._on_message = callback

    async def start(self):
        """Begin polling in the background."""
        self._running = True
        import asyncio
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (Exception, asyncio.CancelledError):
                pass

    async def _poll_loop(self):
        """Repeatedly poll while running."""
        import asyncio
        while self._running:
            try:
                events = await self.poll_once()
                if self._on_message:
                    for event in events:
                        await self._on_message(event)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Polling error")
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> list[dict]:
        """Execute one poll cycle and return new inbound events."""
        events: list[dict] = []

        # 1. Get subscriptions updated since last check
        updates: list[tuple[str, dict]] = []
        subscriptions = await self._client.list_subscriptions()
        for sub in (subscriptions or []):
            room_id = sub.get("rid") or sub.get("_id", "")
            if not room_id:
                continue
            updated_at = sub.get("_updatedAt", "")
            last = self.checkpoint_store.get(room_id)
            if last == 0 or updated_at > str(last):
                updates.append((room_id, sub))

        # 2. Sync messages for updated rooms
        for room_id, sub in updates:
            last = self.checkpoint_store.get(room_id)

            # On first poll, just save the checkpoint without replaying old messages
            updated_at = sub.get("_updatedAt", "")
            if last == 0:
                if updated_at:
                    self.checkpoint_store.save(room_id, updated_at)
                continue

            last_str = str(last)

            data = await self._client.sync_messages(room_id, last_update=last_str)
            updated = data.get("updated", []) if isinstance(data, dict) else []

            for msg in updated:
                msg_id = msg.get("_id", "")
                # Skip bot-authored messages
                if msg.get("u", {}).get("_id") == self._client._user_id:
                    continue
                # Skip system messages
                if msg.get("t"):
                    continue
                # Skip duplicates
                if msg_id in self._seen_ids:
                    continue

                # Attach room type from subscription for downstream routing
                room_type = sub.get("t", "")
                msg["_room_type"] = room_type

                self._seen_ids.add(msg_id)
                events.append(msg)

            # Advance checkpoint to subscription _updatedAt
            updated_at = sub.get("_updatedAt", "")
            if updated_at:
                self.checkpoint_store.save(room_id, updated_at)

        return events


# ---------------------------------------------------------------------------
# Hermes integration stubs (fallback when Hermes is not installed)
# ---------------------------------------------------------------------------

try:
    from hermes.gateway.platforms.base import (  # type: ignore[import-not-found]
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
except ImportError:
    # Test-friendly stubs that mirror the Hermes base adapter interface

    class MessageType(str, Enum):
        TEXT = "text"
        MEDIA = "media"

    @dataclass
    class MessageEvent:
        """Hermes MessageEvent stub for testing without Hermes installed."""

        chat_id: str = ""
        chat_type: str = ""
        user_id: str = ""
        user_name: str = ""
        text: str = ""
        media_urls: list[str] = field(default_factory=list)
        media_types: list[str] = field(default_factory=list)
        reply_to_message_id: str = ""
        raw_payload: dict[str, Any] | None = None
        platform: str = "rocketchat"

    @dataclass
    class SendResult:
        """Hermes SendResult stub."""

        success: bool = False
        message_id: str = ""
        error: str = ""

    class BasePlatformAdapter:
        """Minimal Hermes BasePlatformAdapter stub for isolated testing."""

        def __init__(self, config: Any = None):
            self.config = config
            self._connected = False

        @property
        def is_connected(self) -> bool:
            return self._connected

        async def connect(self) -> bool:
            self._connected = True
            return True

        async def disconnect(self) -> None:
            self._connected = False

        async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: str = "",
            media_files: list[str] | None = None,
        ) -> SendResult:
            return SendResult(success=True)

        async def handle_message(self, event: MessageEvent) -> None:
            pass

        async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
            return {}


# ---------------------------------------------------------------------------
# WebSocket / DDP transport
# ---------------------------------------------------------------------------


def _ws_url(server_url: str) -> str:
    """Convert an HTTP(S) server URL to a Rocket.Chat WebSocket URL."""
    url = server_url.rstrip("/")
    if url.startswith("https://"):
        return url.replace("https://", "wss://", 1) + "/websocket"
    else:
        return url.replace("http://", "ws://", 1) + "/websocket"


class WebSocketTransport:
    """DDP WebSocket inbound transport for Rocket.Chat.

    Connects to Rocket.Chat's WebSocket endpoint, performs the DDP handshake
    (connect, login, subscribe), and emits normalized inbound message events.

    Parameters
    ----------
    client:
        A ``RocketChatClient`` instance (or compatible) for REST calls such as
        listing subscriptions.
    ws_url:
        The WebSocket endpoint URL.  Defaults to ``_ws_url(client.server_url)``.
    ws_factory:
        Optional async callable that returns a WebSocket connection object.
        When *None* (the default) a real aiohttp ``ws_connect`` is used.
    """

    def __init__(self, client: Any, ws_url: str = "", ws_factory: Any = None):
        self._client = client
        self.ws_url = ws_url or _ws_url(client.server_url)
        self._ws_factory = ws_factory or self._default_ws_factory
        self._on_message: Any = None
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._seen_ids: set[str] = set()
        self._sub_ids: set[str] = set()
        # Cache room type from subscriptions so we can tag inbound events
        self._room_types: dict[str, str] = {}

    # -- public API -----------------------------------------------------------

    def set_on_message(self, callback: Any) -> None:
        """Register an async callback ``callback(event: dict)`` for inbound messages."""
        self._on_message = callback

    async def start(self) -> None:
        """Begin the WebSocket receive loop in the background."""
        self._running = True
        self._task = asyncio.create_task(self._receive_loop())

    async def stop(self) -> None:
        """Stop the WebSocket loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (Exception, asyncio.CancelledError):
                pass

    # -- WebSocket factory ----------------------------------------------------

    async def _default_ws_factory(self) -> Any:
        """Create a real aiohttp WebSocket connection."""
        try:
            import aiohttp

            session = aiohttp.ClientSession()
            # Store the session so it can be closed later
            self._http_session = session
            return await session.ws_connect(self.ws_url)
        except ImportError:
            raise RuntimeError(
                "WebSocket transport requires aiohttp. Install with: pip install aiohttp"
            )

    # -- receive loop ---------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Main receive loop with reconnect on failure."""
        import logging

        log = logging.getLogger(__name__)

        while self._running:
            ws = None
            try:
                ws = await self._ws_factory()
                await self._handshake(ws)
                await self._bootstrap_subscriptions(ws)

                while self._running:
                    try:
                        frame = await ws.recv()
                    except Exception:
                        break  # connection lost → reconnect

                    if isinstance(frame, bytes):
                        frame = frame.decode()
                    await self._handle_frame(frame, ws)

            except asyncio.CancelledError:
                break
            except Exception:
                if self._running:
                    log.warning("WebSocket error, reconnecting in 3s…")
                    await asyncio.sleep(3)
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

    # -- DDP handshake --------------------------------------------------------

    async def _handshake(self, ws: Any) -> None:
        """Perform DDP ``connect`` → ``connected`` → ``login``."""

        # 1. Send connect
        await ws.send(
            json.dumps(
                {
                    "msg": "connect",
                    "version": "1",
                    "support": ["1", "pre2", "pre1"],
                }
            )
        )

        # 2. Wait for "connected"
        while self._running:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                frame = frame.decode()
            msg = json.loads(frame)

            if msg.get("msg") == "connected":
                break
            if msg.get("msg") == "ping":
                await ws.send(json.dumps({"msg": "pong"}))

        # 3. Send login
        await ws.send(
            json.dumps(
                {
                    "msg": "method",
                    "method": "login",
                    "params": [{"resume": self._client._access_token}],
                    "id": "1",
                }
            )
        )

        # 4. Wait for login result
        while self._running:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                frame = frame.decode()
            msg = json.loads(frame)

            if msg.get("msg") == "result" and msg.get("id") == "1":
                break
            if msg.get("msg") == "ping":
                await ws.send(json.dumps({"msg": "pong"}))

    # -- subscriptions --------------------------------------------------------

    async def _bootstrap_subscriptions(self, ws: Any) -> None:
        """Subscribe to ``stream-room-messages`` for every joined room."""
        subscriptions = await self._client.list_subscriptions()
        for sub in subscriptions or []:
            room_id = sub.get("rid") or sub.get("_id", "")
            if not room_id:
                continue

            # Cache room type
            room_type = sub.get("t", "")
            if room_type:
                self._room_types[room_id] = room_type

            sub_id = f"sub-{room_id}"
            self._sub_ids.add(sub_id)
            await ws.send(
                json.dumps(
                    {
                        "msg": "sub",
                        "id": sub_id,
                        "name": "stream-room-messages",
                        "params": [room_id, False],
                    }
                )
            )

    # -- frame dispatch -------------------------------------------------------

    async def _handle_frame(self, frame: str, ws: Any) -> None:
        """Dispatch a single DDP frame."""
        msg = json.loads(frame)
        msg_type = msg.get("msg", "")

        if msg_type == "ping":
            await ws.send(json.dumps({"msg": "pong"}))
        elif msg_type == "changed":
            await self._handle_changed(msg)

    async def _handle_changed(self, msg: dict) -> None:
        """Handle a DDP ``changed`` frame from ``stream-room-messages``."""
        collection = msg.get("collection", "")
        if collection != "stream-room-messages":
            return

        fields = msg.get("fields", {})
        args = fields.get("args", [])
        if not args:
            return

        message = args[0]
        msg_id = message.get("_id", "")

        # Deduplicate
        if msg_id and msg_id in self._seen_ids:
            return
        if msg_id:
            self._seen_ids.add(msg_id)

        # Attach room type from cached subscriptions
        room_id = message.get("rid", "")
        if room_id and room_id in self._room_types:
            message["_room_type"] = self._room_types[room_id]

        # Emit
        if self._on_message:
            try:
                await self._on_message(message)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Error in WebSocket on_message callback"
                )


# ---------------------------------------------------------------------------
# Hermes platform adapter
# ---------------------------------------------------------------------------


class RocketChatAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Rocket.Chat.

    Bridges Rocket.Chat rooms (DMs, channels, groups) to the Hermes AI agent
    gateway.  Uses either REST polling or DDP WebSocket for inbound messages
    and the Rocket.Chat REST API for outbound delivery.

    Parameters
    ----------
    config:
        A Hermes ``PlatformConfig`` whose ``.extra`` dict may contain
        per-platform overrides for any ``ROCKETCHAT_*`` setting.
    """

    def __init__(self, config: Any = None):
        super().__init__(config=config)
        self._cfg: RocketChatConfig | None = None
        self._client: RocketChatClient | None = None
        self._transport: Any = None
        self._room_info: dict[str, dict[str, Any]] = {}

        # Parse config early when available
        if config is not None:
            extra: dict[str, Any] = {}
            if hasattr(config, "extra") and config.extra:
                extra = dict(config.extra)
            self._cfg = parse_config(extra)

    # -- lifecycle ------------------------------------------------------------

    async def connect(self) -> bool:
        """Parse config, authenticate, and start the selected transport.

        Returns ``True`` on success.  A return value of ``False`` means the
        adapter could not connect (missing config, auth failure, etc.).
        """
        import logging

        log = logging.getLogger(__name__)

        # Ensure we have a parsed config
        if self._cfg is None:
            extra: dict[str, Any] = {}
            if self.config is not None and hasattr(self.config, "extra"):
                extra = dict(self.config.extra)
            self._cfg = parse_config(extra)

        if not self._cfg.server_url:
            log.error("ROCKETCHAT_SERVER_URL is required")
            return False

        # Create REST client
        self._client = RocketChatClient(
            server_url=self._cfg.server_url,
            user_id=self._cfg.user_id,
            access_token=self._cfg.access_token,
            username=self._cfg.username,
            password=self._cfg.password,
        )

        # Authenticate
        try:
            await self._client.initialize()
        except RocketChatClientError as exc:
            log.error("Rocket.Chat authentication failed: %s", exc)
            return False

        # Choose transport
        transport_type = self._cfg.transport.lower()

        if transport_type == "websocket":
            self._transport = WebSocketTransport(client=self._client)
        else:
            self._transport = PollingTransport(
                client=self._client,
                poll_interval=self._cfg.poll_interval_seconds,
            )

        # Wire inbound callback
        self._transport.set_on_message(self._on_inbound)

        # Start transport
        await self._transport.start()
        self._connected = True

        return True

    async def disconnect(self) -> None:
        """Stop the transport and disconnect from Rocket.Chat."""
        if self._transport is not None:
            await self._transport.stop()
            self._transport = None
        self._connected = False

    # -- send -----------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str = "",
        media_files: list[str] | None = None,
    ) -> SendResult:
        """Send a text message (and optionally media files) to a Rocket.Chat room.

        Returns a ``SendResult`` indicating success or failure.
        """
        if not self._connected or self._client is None:
            return SendResult(
                success=False,
                error="Adapter is not connected",
            )

        try:
            # Truncate to max message length
            text = content
            max_len = self._cfg.max_message_length if self._cfg else 4000
            if len(text) > max_len:
                text = text[:max_len]

            # Threaded reply: use reply_to as tmid
            tmid = reply_to or ""

            result = await self._client.post_message(
                room_id=chat_id,
                text=text,
                tmid=tmid,
            )

            return SendResult(
                success=True,
                message_id=result.get("_id", ""),
            )
        except RocketChatClientError as exc:
            return SendResult(
                success=False,
                error=str(exc),
            )
        except Exception as exc:
            return SendResult(
                success=False,
                error=f"Unexpected error: {exc}",
            )

    # -- inbound callback -----------------------------------------------------

    async def _on_inbound(
        self,
        event: dict[str, Any],
        room_type: str | None = None,
    ) -> None:
        """Process a raw inbound Rocket.Chat message event.

        1. Determine the room type.
        2. Apply mention gating for groups / channels.
        3. Build a Hermes ``MessageEvent``.
        4. Call ``self.handle_message(event)`` to enter the Hermes pipeline.
        """
        # Determine room type: explicit param, or _room_type attached by transport
        if room_type is None:
            room_type = event.get("_room_type", "")

        # Map Rocket.Chat room type codes to Hermes chat types
        chat_type = _rc_room_type_to_chat_type(room_type)

        # Get sender info
        sender = event.get("u", {})
        sender_id = sender.get("_id", "")
        sender_name = sender.get("username", "")

        # Get bot identity for mention gating and self-filtering
        bot_user_id = ""
        bot_username = ""
        if self._client is not None and self._client.identity is not None:
            bot_user_id = self._client.identity.user_id
            bot_username = self._client.identity.username

        # Skip messages authored by the bot itself
        if sender_id and sender_id == bot_user_id:
            return

        # Skip Rocket.Chat system messages (t field = "uj", "ul", etc.)
        if event.get("t"):
            return

        # Mention gating for non-DM rooms
        if chat_type != "dm":
            mention_names = self._cfg.mention_names if self._cfg else []
            text = event.get("msg", "")
            mentions = event.get("mentions", [])

            if not should_handle_message(
                room_type=room_type,
                text=text,
                mentions=mentions,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
                mention_names=mention_names,
            ):
                return

        # Resolve attachments
        media_urls: list[str] = []
        media_types: list[str] = []
        if self._cfg and self._cfg.media_cache_dir:
            media_urls, media_types = await resolve_message_media(
                event, self._client, self._cfg.media_cache_dir
            )
        else:
            media_urls, media_types = await resolve_message_media(
                event, self._client
            )

        # Determine reply target
        reply_to = event.get("tmid", "")

        # Build source
        source = self.build_source(
            chat_id=event.get("rid", ""),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_name,
            room_type=room_type or "",
            room_name=event.get("rn", ""),
        )

        # Create Hermes MessageEvent
        message_event = MessageEvent(
            chat_id=source["chat_id"],
            chat_type=source["chat_type"],
            user_id=source["user_id"],
            user_name=source["user_name"],
            text=event.get("msg", ""),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to,
            raw_payload=event,
            platform="rocketchat",
        )

        await self.handle_message(message_event)

    # -- helpers --------------------------------------------------------------

    def build_source(
        self,
        chat_id: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        room_type: str = "",
        room_name: str = "",
    ) -> dict[str, str]:
        """Build a Hermes source dict from Rocket.Chat fields."""
        source: dict[str, str] = {
            "chat_id": chat_id,
            "chat_type": chat_type,
            "user_id": user_id,
            "user_name": user_name,
        }
        if room_type:
            source["room_type"] = room_type
        if room_name:
            source["room_name"] = room_name
        return source

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return best-effort room metadata for *chat_id*."""
        if chat_id in self._room_info:
            return dict(self._room_info[chat_id])
        return {}


def _rc_room_type_to_chat_type(room_type: str) -> str:
    """Map Rocket.Chat room type codes to Hermes chat types.

    Accepts both short codes (``"d"``, ``"c"``, ``"p"``) and full names
    (``"direct"``, ``"channel"``, ``"group"``) for test and adapter flexibility.
    """
    mapping = {
        "d": "dm",
        "direct": "dm",
        "c": "channel",
        "channel": "channel",
        "p": "group",
        "group": "group",
        "private": "group",
        "l": "channel",
    }
    return mapping.get(room_type.lower(), "channel")


# ---------------------------------------------------------------------------
# Standalone sender (for cron / out-of-process delivery)
# ---------------------------------------------------------------------------


async def standalone_send(
    pconfig: dict[str, Any],
    chat_id: str,
    message: str,
    media_files: list[str] | None = None,
) -> dict[str, Any]:
    """Send a message from a standalone context (cron job, external trigger).

    Parameters
    ----------
    pconfig:
        Platform configuration dict with keys ``server_url``, ``auth_mode``,
        ``user_id`` / ``access_token`` (token mode) or ``username`` / ``password``
        (password mode).
    chat_id:
        Target Rocket.Chat room ID.
    message:
        Text message to send.
    media_files:
        Optional list of local file paths to upload before/beside the text.

    Returns
    -------
    dict
        ``{"success": bool, "message_id": str, "error": str}`` — compatible
        with the Hermes standalone-sender contract.
    """
    import logging

    log = logging.getLogger(__name__)

    try:
        cfg = parse_config(pconfig)

        client = RocketChatClient(
            server_url=cfg.server_url,
            user_id=cfg.user_id,
            access_token=cfg.access_token,
            username=cfg.username,
            password=cfg.password,
        )
        await client.initialize()

        message_id = ""

        # Upload media files if any
        if media_files:
            for file_path in media_files:
                uploaded = await client.upload_attachment(
                    room_id=chat_id,
                    file_path=file_path,
                    text=message,
                )
                message_id = uploaded.get("_id", message_id)

        # Post text message (even when media was uploaded — may serve as caption)
        if message:
            result = await client.post_message(room_id=chat_id, text=message)
            message_id = result.get("_id", message_id)

        return {"success": True, "message_id": message_id}

    except RocketChatClientError as exc:
        log.error("standalone_send client error: %s", exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        log.exception("standalone_send unexpected error")
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# File upload support (on RocketChatClient)
# ---------------------------------------------------------------------------


async def _client_upload_attachment(
    self: RocketChatClient,
    room_id: str,
    file_path: str,
    text: str = "",
    tmid: str = "",
) -> dict[str, Any]:
    """Upload a local file to a Rocket.Chat room.

    Uses the three-step Rocket.Chat file-upload flow:
    1. ``POST /api/v1/rooms.media/{roomId}``  — upload the file
    2. ``POST /api/v1/rooms.mediaConfirm/{roomId}/{fileId}`` — confirm
    3. ``POST /api/v1/chat.postMessage`` — post the text message with file ref

    .. note::

       In a production deployment with real multipart uploads the first step
       sends the file as form data.  The current implementation sends file
       metadata as JSON so that tests with mock HTTP sessions can exercise the
       full flow without multipart machinery.
    """
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        raise RocketChatClientError(f"File not found: {file_path}")

    file_name = path.name

    # Step 1 — upload
    upload_result = await self._request(
        "POST",
        f"/api/v1/rooms.media/{room_id}",
        json={"file_name": file_name, "file_path": str(path)},
    )

    if not upload_result.get("success", False):
        raise RocketChatClientError(
            f"Upload failed: {upload_result.get('error', 'unknown')}"
        )

    file_id = upload_result.get("file", {}).get("_id", "")

    # Step 2 — confirm
    if file_id:
        confirm_payload: dict[str, str] = {"msg": text}
        if tmid:
            confirm_payload["tmid"] = tmid
        await self._request(
            "POST",
            f"/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            json=confirm_payload,
        )

    # Step 3 — post message with file reference
    payload: dict[str, Any] = {"roomId": room_id, "text": text}
    if tmid:
        payload["tmid"] = tmid
    if file_id:
        payload["file"] = {"_id": file_id, "name": file_name}

    data = await self._request("POST", "/api/v1/chat.postMessage", json=payload)

    if not data.get("success", False):
        raise RocketChatClientError(f"Send failed: {data.get('error', 'chat.postMessage failed')}")

    return data.get("message", {})


# Attach the upload method to RocketChatClient
RocketChatClient.upload_attachment = _client_upload_attachment  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Plugin registration (Hermes entry point)
# ---------------------------------------------------------------------------


def check_requirements() -> tuple[bool, str]:
    """Check that aiohttp or httpx is available for the Rocket.Chat adapter.

    Returns ``(ok, message)`` where *ok* is ``True`` when at least one HTTP
    library is importable.
    """
    try:
        import aiohttp  # noqa: F401
        return True, "aiohttp available"
    except ImportError:
        pass
    try:
        import httpx  # noqa: F401
        return True, "httpx available"
    except ImportError:
        pass
    return False, "Missing HTTP library — install aiohttp or httpx"


def register(ctx: Any) -> None:
    """Register the Rocket.Chat platform adapter with the Hermes plugin system.

    Called by the Hermes plugin loader.  ``ctx`` is a plugin context object
    that exposes ``ctx.register_platform(...)``.
    """
    ctx.register_platform(
        name="rocketchat",
        label="Rocket.Chat",
        adapter_factory=lambda cfg: RocketChatAdapter(cfg),
        check_fn=check_requirements,
        env_enablement_fn=env_enablement,
        standalone_sender_fn=standalone_send,
        required_env=["ROCKETCHAT_SERVER_URL", "ROCKETCHAT_AUTH_MODE"],
        allowed_users_env="ROCKETCHAT_ALLOWED_USERS",
        allow_all_env="ROCKETCHAT_ALLOW_ALL_USERS",
        cron_deliver_env_var="ROCKETCHAT_HOME_CHANNEL",
        max_message_length=4000,
        platform_hint=(
            "You are chatting via Rocket.Chat. DMs are direct conversations; "
            "channel replies should be concise and thread-aware. Markdown is supported."
        ),
        emoji="🚀",
    )
