"""Rocket.Chat platform adapter for Hermes Agent gateway.

Provides a self-contained Hermes platform plugin that connects Rocket.Chat
rooms to the Hermes messaging gateway via REST polling or WebSocket/DDP.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

_KT = TypeVar("_KT")
_VT = TypeVar("_VT")


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
    always_respond_rooms: list[str] = field(default_factory=list)
    ignore_other_user_mentions: bool = False
    force_thread: bool = False
    home_channel: str = ""
    media_cache_dir: str = ""
    allowed_users: list[str] = field(default_factory=list)
    allow_all: bool = False
    max_message_length: int = 4000
    # Reconnect / heartbeat tuning (WebSocket transport)
    receive_timeout: float = 60.0
    ping_timeout: float = 10.0
    subscription_refresh_seconds: float = 300.0
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    reconnect_max_attempts: int = 0  # 0 = unlimited
    reconnect_jitter: float = 0.25
    # Inbound dedup (WebSocket transport) — suppress replayed messages after reconnect
    dedup_enabled: bool = True
    dedup_ttl_hours: float = 168.0  # 7 days
    dedup_store_path: str = ""


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
        return str(
            extra.get(key) or os.environ.get(f"ROCKETCHAT_{key.upper()}", default)
        )

    cfg = RocketChatConfig(
        server_url=_get("server_url"),
        auth_mode=_get("auth_mode", "token"),
        user_id=_get("user_id"),
        access_token=_get("access_token"),
        username=_get("username"),
        password=_get("password"),
        transport=_get("transport", "polling"),
        poll_interval_seconds=_parse_float_safe(
            extra.get("poll_interval_seconds")
            or os.environ.get("ROCKETCHAT_POLL_INTERVAL_SECONDS", "3"),
            3.0,
        ),
        mention_names=_parse_csv(
            extra.get("mention_names") or os.environ.get("ROCKETCHAT_MENTION_NAMES", "")
        ),
        always_respond_rooms=_parse_csv(
            extra.get("always_respond_rooms")
            or os.environ.get("ROCKETCHAT_ALWAYS_RESPOND_ROOMS", "")
        ),
        ignore_other_user_mentions=_parse_bool(
            extra.get("ignore_other_user_mentions")
            or os.environ.get("ROCKETCHAT_IGNORE_OTHER_USER_MENTIONS")
        ),
        force_thread=_parse_bool(
            extra.get("force_thread") or os.environ.get("ROCKETCHAT_FORCE_THREAD")
        ),
        home_channel=_get("home_channel"),
        media_cache_dir=extra.get("media_cache_dir")
        or os.environ.get("ROCKETCHAT_MEDIA_CACHE_DIR", ""),
        allowed_users=_parse_csv(
            extra.get("allowed_users") or os.environ.get("ROCKETCHAT_ALLOWED_USERS", "")
        ),
        allow_all=_parse_bool(
            extra.get("allow_all") or os.environ.get("ROCKETCHAT_ALLOW_ALL_USERS")
        ),
        max_message_length=_parse_int_safe(
            extra.get("max_message_length")
            or os.environ.get("ROCKETCHAT_MAX_MESSAGE_LENGTH", "4000"),
            4000,
        ),
        receive_timeout=_parse_float_safe(
            extra.get("receive_timeout")
            or os.environ.get("ROCKETCHAT_RECEIVE_TIMEOUT", "60"),
            60.0,
        ),
        ping_timeout=_parse_float_safe(
            extra.get("ping_timeout")
            or os.environ.get("ROCKETCHAT_PING_TIMEOUT", "10"),
            10.0,
        ),
        subscription_refresh_seconds=_parse_float_safe(
            extra.get("subscription_refresh_seconds")
            or os.environ.get("ROCKETCHAT_SUBSCRIPTION_REFRESH_SECONDS", "300"),
            300.0,
        ),
        reconnect_initial_delay=_parse_float_safe(
            extra.get("reconnect_initial_delay")
            or os.environ.get("ROCKETCHAT_RECONNECT_INITIAL_DELAY", "1"),
            1.0,
        ),
        reconnect_max_delay=_parse_float_safe(
            extra.get("reconnect_max_delay")
            or os.environ.get("ROCKETCHAT_RECONNECT_MAX_DELAY", "60"),
            60.0,
        ),
        reconnect_max_attempts=_parse_int_safe(
            extra.get("reconnect_max_attempts")
            or os.environ.get("ROCKETCHAT_RECONNECT_MAX_ATTEMPTS", "0"),
            0,
        ),
        reconnect_jitter=_parse_float_safe(
            extra.get("reconnect_jitter")
            or os.environ.get("ROCKETCHAT_RECONNECT_JITTER", "0.25"),
            0.25,
        ),
        dedup_enabled=_parse_bool(
            extra.get("dedup_enabled")
            or os.environ.get("ROCKETCHAT_DEDUP_ENABLED", "true")
        ),
        dedup_ttl_hours=_parse_float_safe(
            extra.get("dedup_ttl_hours")
            or os.environ.get("ROCKETCHAT_DEDUP_TTL_HOURS", "168"),
            168.0,
        ),
        dedup_store_path=str(
            extra.get("dedup_store_path")
            or os.environ.get("ROCKETCHAT_DEDUP_STORE_PATH", "")
        ),
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


def _mention_username(mention: Any) -> str:
    """Normalize a Rocket.Chat mention entry to a username string.

    Rocket.Chat delivers ``mentions`` as objects (``{"username": ...}``) in
    production and as plain strings in some test paths; accept both.
    """
    if isinstance(mention, dict):
        return str(mention.get("username") or mention.get("name") or "")
    return str(mention)


def should_handle_message(
    room_type: str,
    text: str,
    mentions: list[Any],
    bot_user_id: str,
    bot_username: str,
    mention_names: list[str] | None = None,
    ignore_other_user_mentions: bool = False,
) -> bool:
    """Decide whether an inbound Rocket.Chat message should be dispatched to Hermes.

    - Direct messages always pass through.
    - Group/channel messages require an explicit mention of the bot username
      or a configured alias, either via Rocket.Chat mention metadata or via
      an @-mention in the message text.
    - When ``ignore_other_user_mentions`` is set, a message that mentions the
      bot *alongside* other users is treated as a mass mention and ignored;
      the bot still responds to a direct @-mention.
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
            # Tolerate a leading '@' in configured aliases
            # (ROCKETCHAT_MENTION_NAMES="@helper").
            triggers.add(alias.lower().lstrip("@"))

    if not triggers:
        return False

    # Check Rocket.Chat mention metadata (array of usernames / user objects)
    bot_mentioned = False
    other_mentioned = False
    for m in mentions:
        key = _mention_username(m).strip().lower()
        if not key:
            continue
        if key in triggers:
            bot_mentioned = True
        else:
            other_mentioned = True

    if bot_mentioned:
        if ignore_other_user_mentions and other_mentioned:
            return False
        return True

    # Check text for @mention patterns
    text_lower = text.lower()
    for trigger in triggers:
        if _has_token_mention(text_lower, trigger):
            # Text-fallback path: honour the mass-mention flag too — when the
            # text mentions other users next to the bot, treat it as a
            # message for everyone, not a direct call.
            if ignore_other_user_mentions and _has_other_mentions(text_lower, triggers):
                return False
            return True

    return False


def _has_other_mentions(text_lower: str, triggers: set[str]) -> bool:
    """Return True when *text_lower* @-mentions someone who is not a trigger."""
    import re

    for token in re.findall(r"@[\w.-]+", text_lower):
        if token[1:] not in triggers:
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


class RocketChatNotFoundError(RocketChatClientError):
    """Raised when a Rocket.Chat REST endpoint is unavailable."""


# Thread reply-context cache lifetimes (seconds).
_REPLY_CACHE_TTL = 300.0
_REPLY_NEGATIVE_TTL = 60.0

THINKING_PLACEHOLDER_TEXT = "💭 Thinking…"


class RocketChatRateLimitError(RocketChatClientError):
    """Raised when Rocket.Chat asks the client to slow down."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: Any) -> float | None:
    """Extract retry-after seconds from a header or Rocket.Chat error text."""
    if value is None:
        return None
    try:
        retry_after = float(value)
        return retry_after if retry_after >= 0 else None
    except (TypeError, ValueError):
        pass

    import re

    match = re.search(r"wait\s+(\d+(?:\.\d+)?)\s+seconds?", str(value), re.I)
    if match:
        return _parse_float_safe(match.group(1), 0.0)
    return None


def _utf16_units(text: str) -> int:
    """Count UTF-16 code units — what Rocket.Chat/JS string length uses.

    Astral characters (emoji, rare CJK) count as 2 units; the plugin's
    ``max_message_length`` budget must be measured in these units or
    astral-heavy messages would exceed the server limit.
    """
    return len(text.encode("utf-16-le")) // 2


def _prefix_within_units(text: str, max_units: int) -> str:
    """Return the longest prefix of *text* whose UTF-16 length is <= *max_units*.

    Always cuts on a code-point boundary (never splits a surrogate pair).
    """
    acc = 0
    for i, ch in enumerate(text):
        acc += 2 if ord(ch) > 0xFFFF else 1
        if acc > max_units:
            return text[:i]
    return text


def _truncate_utf16(text: str, max_units: int) -> str:
    """Truncate *text* to at most *max_units* UTF-16 code units."""
    if _utf16_units(text) <= max_units:
        return text
    return _prefix_within_units(text, max_units)


def _absolutize_media_url(url: str, base: str) -> str:
    """Join a relative Rocket.Chat upload URL onto the server origin.

    Rocket.Chat file objects report ``/file-upload/<rid>/<fid>/<name>``
    relative paths; they must be absolute before any HTTP client will
    fetch them.  Absolute URLs pass through untouched.
    """
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/") and base:
        return f"{str(base).rstrip('/')}{url}"
    return url


def _same_origin(url_a: str, url_b: str) -> bool:
    """Return True when both URLs share scheme, host and port."""
    from urllib.parse import urlsplit

    def _origin(url: str) -> tuple[str, str, int | None]:
        parts = urlsplit(url)
        port = parts.port
        if port is None and parts.scheme == "https":
            port = 443
        elif port is None and parts.scheme == "http":
            port = 80
        return (parts.scheme, (parts.hostname or "").lower(), port)

    left, right = _origin(url_a), _origin(url_b)
    return left[0] in ("http", "https") and left == right


async def _host_is_public(host: str) -> bool:
    """Resolve *host* and verify none of its addresses is private.

    SSRF guard for download targets chosen from untrusted message content:
    blocks loopback, link-local, private (RFC1918), and otherwise
    non-global addresses.  Resolution failure fails closed.
    """
    import ipaddress
    import socket

    try:
        infos = await asyncio.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    for info in infos[:8]:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not address.is_global:
            return False
    return True


async def _resolve_with_timeout(value: Any, timeout: float) -> Any:
    """Await *value* (if awaitable) under a hard timeout."""
    import inspect

    if inspect.isawaitable(value):
        return await asyncio.wait_for(value, timeout=timeout)
    return value


def _decode_ddp_frame(frame: str) -> dict[str, Any] | None:
    """Decode one DDP JSON frame, returning None for malformed frames."""
    from contextlib import suppress

    with suppress(json.JSONDecodeError):
        data = json.JSONDecoder().decode(frame)
        return data if isinstance(data, dict) else None
    return None


async def _maybe_await(value: Any) -> Any:
    """Await *value* when it is awaitable, otherwise return it directly."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _ws_send_text(ws: Any, frame: str) -> None:
    """Send a text frame across websockets and aiohttp websocket shapes."""
    send = getattr(ws, "send", None)
    if send is not None:
        await _maybe_await(send(frame))
        return

    send_str = getattr(ws, "send_str", None)
    if send_str is not None:
        await _maybe_await(send_str(frame))
        return

    raise RocketChatClientError("WebSocket object does not support text send")


async def _ws_recv_text(ws: Any) -> str:
    """Receive a text frame across websockets and aiohttp websocket shapes."""
    recv = getattr(ws, "recv", None)
    if recv is not None:
        frame = await _maybe_await(recv())
    else:
        receive = getattr(ws, "receive", None)
        if receive is None:
            raise RocketChatClientError("WebSocket object does not support receive")
        message = await _maybe_await(receive())
        frame = getattr(message, "data", message)

    if isinstance(frame, bytes):
        return frame.decode()
    if isinstance(frame, str):
        return frame
    raise ConnectionError("WebSocket closed without a text frame")


async def _response_text(resp: Any) -> str:
    """Return response text across aiohttp/httpx/test response shapes."""
    text_attr = getattr(resp, "text", None)
    if text_attr is not None:
        text = text_attr() if callable(text_attr) else text_attr
        text = await _maybe_await(text)
        return text if isinstance(text, str) else str(text)

    read_fn = getattr(resp, "aread", None) or getattr(resp, "read", None)
    if read_fn is None:
        return ""
    data = await _maybe_await(read_fn())
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return str(data)


def _multipart_request_kwargs(
    session: Any, file_path: str, filename: str, content_type: str
) -> dict[str, Any]:
    """Build backend-specific multipart request kwargs for *file_path*.

    aiohttp uploads via ``data=aiohttp.FormData()``, httpx via
    ``files=``.  The payload is read into memory so neither backend keeps
    a dangling file handle after this helper returns.
    """
    with open(file_path, "rb") as fh:
        payload = fh.read()
    try:
        import aiohttp  # type: ignore[reportMissingImports]

        if isinstance(session, aiohttp.ClientSession):
            form = aiohttp.FormData()
            form.add_field(
                "file", payload, filename=filename, content_type=content_type
            )
            return {"data": form}
    except ImportError:
        pass
    return {"files": {"file": (filename, payload, content_type)}}


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
        # Cached HTTP session (lazy, shared by _request / download_attachment).
        self._session: Any = None
        # Bounds for inbound attachment downloads (SSRF/DoS hardening).
        self._download_timeout = 30.0
        self._max_download_bytes = 64 * 1024 * 1024
        # Default timeout for every REST call via _request().
        self._request_timeout = 30.0

    @property
    def identity(self) -> RocketChatIdentity | None:
        return self._identity

    @property
    def server_url(self) -> str:
        return self._server_url

    # -- subclasses / tests override this to inject fake HTTP sessions --------

    async def _get_session(self):
        """Return the cached HTTP session (lazy import to keep deps optional).

        One session per client is reused for every REST call and attachment
        download so aiohttp/httpx connection pooling (keep-alive, TLS resumption)
        actually applies — instead of a fresh TCP+TLS handshake per request.
        """
        if (
            self._session is None
            or getattr(self._session, "closed", False)
            or getattr(self._session, "is_closed", False)
        ):
            try:
                import aiohttp  # type: ignore[reportMissingImports]

                self._session = aiohttp.ClientSession()
            except ImportError:
                import httpx  # type: ignore[import-not-found]

                self._session = httpx.AsyncClient()
        return self._session

    async def close(self) -> None:
        """Close the cached HTTP session (idempotent)."""
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            closer = getattr(session, "aclose", None) or getattr(session, "close", None)
            if closer is not None:
                await _maybe_await(closer())
        except Exception:
            pass

    def _timeout_kwargs(self, session: Any, seconds: float) -> dict[str, Any]:
        """Build backend-specific ``timeout=`` kwargs for a request."""
        try:
            import aiohttp  # type: ignore[reportMissingImports]

            if isinstance(session, aiohttp.ClientSession):
                return {"timeout": aiohttp.ClientTimeout(total=seconds)}
        except ImportError:
            pass
        # httpx accepts a plain float.
        return {"timeout": max(0.1, seconds)}

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
        raw: bool = False,
        multipart: dict | None = None,
        send_auth_headers: bool = True,
    ):
        """Make an HTTP request to the Rocket.Chat REST API.

        ``multipart`` (optional dict with ``file_path``/``filename``/
        ``content_type``) turns the request into a real file upload; it
        takes precedence over ``json``.  ``send_auth_headers=False`` skips
        the bot credentials (login flow before a token exists).
        """
        url = f"{self._server_url}{path}"
        default_headers: dict[str, Any] = {}
        if send_auth_headers:
            default_headers = {
                "X-User-Id": self._user_id,
                "X-Auth-Token": self._access_token,
            }
        if headers:
            default_headers.update(headers)

        session = await self._get_session()

        if multipart is not None:
            request_kwargs = _multipart_request_kwargs(
                session,
                multipart["file_path"],
                multipart.get("filename", ""),
                multipart.get("content_type", "application/octet-stream"),
            )
        else:
            request_kwargs = {"json": json, "params": params}
        request_kwargs.update(self._timeout_kwargs(session, self._request_timeout))
        resp = await _maybe_await(
            session.request(method, url, headers=default_headers, **request_kwargs)
        )
        status = getattr(resp, "status", getattr(resp, "status_code", None))
        if raw:
            read_fn = resp.aread if hasattr(resp, "aread") else resp.read
            body = await _maybe_await(read_fn())
            if status is not None and status >= 400:
                raise RocketChatClientError(f"{method} {path} failed: HTTP {status}")
            return body

        if status is not None and status >= 400:
            body = await _response_text(resp)
            message = f"{method} {path} failed: HTTP {status}: {body[:200]}"
            if status == 429:
                headers_obj = getattr(resp, "headers", None)
                retry_header = (
                    headers_obj.get("Retry-After") if headers_obj is not None else None
                )
                retry_after = _parse_retry_after(retry_header or body)
                raise RocketChatRateLimitError(message, retry_after=retry_after)
            if status == 404:
                raise RocketChatNotFoundError(message)
            raise RocketChatClientError(message)

        try:
            return await _maybe_await(resp.json())
        except RocketChatClientError:
            raise
        except Exception as exc:
            raise RocketChatClientError(
                f"{method} {path} returned invalid JSON: {exc}"
            ) from exc

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

        if not data.get("success") or not data.get("_id"):
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
                send_auth_headers=False,  # no credentials exist before login
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

    async def update_message(self, room_id: str, message_id: str, text: str) -> dict:
        """Update an existing chat message via /api/v1/chat.update."""
        data = await self._request(
            "POST",
            "/api/v1/chat.update",
            json={"roomId": room_id, "msgId": message_id, "text": text},
        )

        if not data.get("success", False):
            msg = data.get("error", "chat.update failed")
            raise RocketChatClientError(f"Update failed: {msg}")

        return data.get("message", {})

    async def get_message(self, message_id: str) -> dict:
        """Fetch a single message via /api/v1/chat.getMessage."""
        data = await self._request(
            "GET",
            "/api/v1/chat.getMessage",
            params={"msgId": message_id},
        )

        if not data.get("success", False):
            msg = data.get("error", "chat.getMessage failed")
            raise RocketChatClientError(f"Get message failed: {msg}")

        return data.get("message") or {}

    async def room_info(self, room_id: str) -> dict:
        """Resolve a room by id via /api/v1/rooms.info."""
        data = await self._request(
            "GET",
            "/api/v1/rooms.info",
            params={"roomId": room_id},
        )
        if not data.get("success", False):
            raise RocketChatClientError(
                f"rooms.info failed: {data.get('error', 'unknown')}"
            )
        return data.get("room") or {}

    async def user_info(self, user_id: str = "", username: str = "") -> dict:
        """Resolve a user by id or username via /api/v1/users.info."""
        params: dict[str, str] = {}
        if user_id:
            params["userId"] = user_id
        if username:
            params["username"] = username
        data = await self._request("GET", "/api/v1/users.info", params=params)
        if not data.get("success", False):
            raise RocketChatClientError(
                f"users.info failed: {data.get('error', 'unknown')}"
            )
        return data.get("user") or {}

    async def create_direct_room(self, usernames: str | list[str]) -> str:
        """Create or reuse a direct room via /api/v1/dm.create.

        Returns the room id, or an empty string on a non-success response.
        """
        if isinstance(usernames, list):
            usernames = ",".join(usernames)
        data = await self._request(
            "POST",
            "/api/v1/dm.create",
            json={"usernames": usernames},
        )
        if not data.get("success", False):
            raise RocketChatClientError(
                f"dm.create failed: {data.get('error', 'unknown')}"
            )
        room = data.get("room") or {}
        return str(room.get("_id") or "")

    async def list_subscriptions(self, updated_since=None) -> list[dict]:
        """List subscriptions via /api/v1/subscriptions.get.

        Rocket.Chat returns the full subscription set in one response; the
        endpoint schema accepts only ``updatedSince`` (no count/offset).
        Sending pagination params here makes the server reject the request
        with HTTP 400 "must NOT have additional properties", which the
        transports treat as a connection error and loop forever on.
        """
        params: dict[str, Any] = {}
        if updated_since:
            params["updatedSince"] = updated_since
        data = await self._request(
            "GET", "/api/v1/subscriptions.get", params=params
        )
        return data.get("update", data.get("subscriptions", []))

    async def sync_messages(
        self,
        room_id: str,
        last_update=None,
        room_type: str = "",
    ) -> dict:
        """Sync messages for a room, falling back for older Rocket.Chat servers."""
        payload: dict[str, Any] = {"roomId": room_id}
        if last_update:
            payload["lastUpdate"] = last_update
        try:
            return await self._request(
                "POST", "/api/v1/chat.syncMessages", json=payload
            )
        except RocketChatNotFoundError as exc:
            if "HTTP 404" not in str(exc):
                raise
            if not room_type:
                raise
            return await self.history_messages(
                room_id=room_id,
                room_type=room_type,
                oldest=last_update,
            )
        except RocketChatClientError as exc:
            # Older servers reject chat.syncMessages with a 400 instead of a
            # 404 — same fallback applies.
            if "HTTP 400" not in str(exc):
                raise
            if not room_type:
                raise
            return await self.history_messages(
                room_id=room_id,
                room_type=room_type,
                oldest=last_update,
            )

    async def history_messages(
        self,
        room_id: str,
        room_type: str,
        oldest=None,
    ) -> dict:
        """Read room history via room-type-specific Rocket.Chat REST endpoints."""
        endpoint = {
            "c": "/api/v1/channels.history",
            "p": "/api/v1/groups.history",
            "d": "/api/v1/im.history",
        }.get(room_type)
        if endpoint is None:
            raise RocketChatClientError(
                f"Unsupported Rocket.Chat room type: {room_type}"
            )

        params: dict[str, Any] = {"roomId": room_id, "count": 100}
        if oldest:
            params["oldest"] = oldest
            params["inclusive"] = "false"

        # Page newest-first (the fallback path on servers without
        # chat.syncMessages): up to 5 pages so fast rooms do not drop
        # messages between polls.  Offset pages walk backwards into older
        # history; delivering "updated" oldest-first keeps the dedup
        # checkpoints monotonic.
        messages: list[Any] = []
        removed: list[Any] = []
        for _page in range(5):
            data = await self._request("GET", endpoint, params=params)
            if not isinstance(data, dict):
                break
            page_messages = data.get("messages", []) or []
            messages.extend(page_messages)
            removed.extend(data.get("removed", []) or [])
            if len(page_messages) < 100 or not oldest:
                break
            params["offset"] = int(params.get("offset", 0) or 0) + 100

        return {"updated": list(reversed(messages)), "removed": removed}

    # -- download ------------------------------------------------------------

    async def download_attachment(self, url: str) -> bytes:
        """Download a file attachment with minimal privilege.

        Security posture (SSRF + credential-leak hardening):

        - Relative ``/file-upload/...`` URLs are joined onto the server origin
          (Rocket.Chat file objects report relative paths).
        - Only http(s) targets are fetched.
        - The bot credentials (``X-User-Id``/``X-Auth-Token``) are attached
          ONLY when the target host is the configured server; cross-origin
          targets are fetched without credentials, must be https, and must
          resolve to a public address (private/loopback/link-local blocked).
        - Redirects are followed up to 2 hops, re-running the same checks on
          every hop.
        - A hard timeout (``download_timeout``) and size cap
          (``max_download_bytes``) bound the fetch.
        """
        target = _absolutize_media_url(url, self._server_url)
        session = await self._get_session()

        for _hop in range(3):  # initial request + up to 2 redirect hops
            if not target.startswith(("http://", "https://")):
                raise RocketChatClientError(
                    f"Unsupported attachment URL: {str(url)[:120]}"
                )

            same_origin = _same_origin(target, self._server_url)
            headers: dict[str, Any] = {}
            if same_origin:
                headers = {
                    "X-User-Id": self._user_id,
                    "X-Auth-Token": self._access_token,
                }
            else:
                # Untrusted (attacker-chosen) target: no credentials, https
                # only, no private/loopback addresses.
                if not target.startswith("https://"):
                    raise RocketChatClientError(
                        "Cross-origin attachment downloads require https"
                    )
                if not await _host_is_public(target.split("/")[2].split(":")[0]):
                    raise RocketChatClientError(
                        "Blocked private/loopback attachment host"
                    )

            redirect_kwargs: dict[str, Any] = {}
            try:
                import aiohttp  # type: ignore[reportMissingImports]

                uses_aiohttp = isinstance(session, aiohttp.ClientSession)
            except ImportError:
                uses_aiohttp = False
            redirect_kwargs[
                "allow_redirects" if uses_aiohttp else "follow_redirects"
            ] = False

            resp = await _resolve_with_timeout(
                session.get(target, headers=headers, **redirect_kwargs),
                self._download_timeout,
            )
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get(
                "Location"
            ):
                from urllib.parse import urljoin

                target = urljoin(target, resp.headers["Location"])
                continue
            resp.raise_for_status()
            read_fn = resp.aread if hasattr(resp, "aread") else resp.read
            data = await _resolve_with_timeout(read_fn(), self._download_timeout)
            if len(data) > self._max_download_bytes:
                raise RocketChatClientError(
                    f"Attachment download exceeds size limit "
                    f"({self._max_download_bytes} bytes)"
                )
            return data

        raise RocketChatClientError(f"Too many attachment redirects: {str(url)[:120]}")

    async def upload_attachment(
        self,
        room_id: str,
        file_path: str,
        text: str = "",
        tmid: str = "",
    ) -> dict:
        """Upload a local file to a Rocket.Chat room.

        Uses the three-step Rocket.Chat file-upload flow:
        1. ``POST /api/v1/rooms.media/{roomId}``  — upload the file as real
           multipart form data (the file bytes, not just metadata)
        2. ``POST /api/v1/rooms.mediaConfirm/{roomId}/{fileId}`` — confirm
        3. ``POST /api/v1/chat.postMessage`` — post the text message with file ref
        """
        import mimetypes
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            raise RocketChatClientError(f"File not found: {file_path}")

        file_name = path.name
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        # Step 1 — upload (multipart form data carrying the actual file)
        upload_result = await self._request(
            "POST",
            f"/api/v1/rooms.media/{room_id}",
            multipart={
                "file_path": str(path),
                "filename": file_name,
                "content_type": content_type,
            },
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
            raise RocketChatClientError(
                f"Send failed: {data.get('error', 'chat.postMessage failed')}"
            )

        return data.get("message", {})


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
) -> tuple[list[str], list[str], list[Any | None]]:
    """Download attachments into cache and return media metadata.

    Returns three parallel lists suitable for Hermes ``MessageEvent`` fields:
    ``(media_urls, media_types, media_text_inlined)``.  The inlining flag is
    ``False`` for ``text/*`` attachments (cached but NOT inlined into the
    event ``text`` — the gateway must tell the agent to read the file itself,
    Hermes 00394acfae) and ``None`` for everything else (no text content).
    """
    candidates = attachment_candidates_from_message(message)
    if not candidates:
        return [], [], []

    media_urls: list[str] = []
    media_types: list[str] = []
    media_text_inlined: list[Any | None] = []

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
            # No cache dir: pass the URL through, absolutized against the
            # server origin so downstream consumers can actually fetch it.
            server_url = client.server_url if hasattr(client, "server_url") else ""
            local_path = _absolutize_media_url(candidate.url, server_url)
        else:
            continue

        media_urls.append(local_path)
        media_types.append(media_type)
        mime = (candidate.mime_type or "").lower()
        media_text_inlined.append(False if mime.startswith("text/") else None)

    return media_urls, media_types, media_text_inlined


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

    def oldest(self):
        """Return the oldest checkpoint across rooms (or None if empty).

        Used as the ``updatedSince`` delta for ``subscriptions.get`` so the
        server-side filter covers every room the bot knows about.
        """
        values = [v for v in self._checkpoints.values() if v]
        return min(values) if values else None

    def save(self, room_id: str, updated_at):
        """Store the latest timestamp for a room."""
        self._checkpoints[room_id] = updated_at


class BoundedDict(OrderedDict[_KT, _VT]):
    """OrderedDict with oldest-first eviction past a maximum size.

    Mirrors the cache-bounding pattern Hermes applied to its Slack adapters
    (533e54123 / d42b29579 / 91693f9d4): per-chat tracking structures must
    not grow without bound on a long-running gateway.  Re-setting an existing
    key refreshes its position (LRU-ish) before eviction applies.
    """

    def __init__(self, maxsize: int = 500):
        super().__init__()
        try:
            self.maxsize = max(int(maxsize), 1)
        except (TypeError, ValueError):
            self.maxsize = 500

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)


class PersistentSeenIdStore:
    """Disk-backed seen-message-id store for WebSocket reconnect dedup.

    Rocket.Chat's ``stream-room-messages`` subscription can replay recent
    unread messages when a WebSocket reconnects (even with
    ``useHistory=False``).  The transport's in-memory ``_seen_ids`` set is
    lost on reconnect/restart, so the adapter layer keeps this persistent
    store to suppress messages it has already handled.

    Entries expire after ``ttl_seconds`` (default 7 days) so the file does
    not grow without bound.  Writes are atomic (temp file + rename).
    """

    def __init__(self, path: str = "", ttl_seconds: float = 7 * 24 * 3600):
        self._path = path
        self._ttl = max(1.0, _parse_float_safe(ttl_seconds, 7 * 24 * 3600))
        self._seen: dict[str, float] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        """Load seen ids from disk, dropping expired entries."""
        if not self._path:
            return
        import json

        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        import time

        now = time.time()
        self._seen = {}
        for k, v in data.items():
            ts = _parse_float_safe(v, 0.0)
            if ts and now - ts < self._ttl:
                self._seen[str(k)] = ts

    def contains(self, msg_id: str) -> bool:
        """Return True if *msg_id* was already seen and is still valid."""
        if not msg_id:
            return False
        import time

        ts = self._seen.get(msg_id)
        if ts is None:
            return False
        if time.time() - ts > self._ttl:
            # Expired — drop lazily
            self._seen.pop(msg_id, None)
            self._dirty = True
            return False
        return True

    def mark(self, msg_id: str) -> None:
        """Record *msg_id* as seen now."""
        if not msg_id:
            return
        import time

        self._seen[msg_id] = time.time()
        self._dirty = True

    def flush(self) -> None:
        """Persist the store to disk atomically if there are pending changes."""
        if not self._path or not self._dirty:
            return
        import json
        import os
        import tempfile

        directory = os.path.dirname(self._path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
        # Prune expired entries before writing
        import time

        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}
        try:
            fd, tmp = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._seen, fh)
            os.replace(tmp, self._path)
            self._dirty = False
        except OSError:
            pass


class PollingTransport:
    """Poll Rocket.Chat REST API for new messages.

    Keeps in-memory checkpoints and polls subscriptions.get / chat.syncMessages
    on a configurable interval.
    """

    def __init__(self, client, poll_interval: float = 3.0, on_auth_failure: Any = None):
        self._client = client
        self.poll_interval = poll_interval
        self.checkpoint_store = InMemoryCheckpointStore()
        self._seen_ids: BoundedDict[str, float] = BoundedDict(maxsize=100_000)
        self._running = False
        self._task: Any = None
        self._on_message: Any = None
        self._on_auth_failure: Any = on_auth_failure

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

    def _sleep_after_error(self, error: Exception) -> float:
        """Return the next polling delay after an error."""
        retry_after = getattr(error, "retry_after", None)
        if retry_after is None:
            return self.poll_interval
        return max(
            self.poll_interval, _parse_float_safe(retry_after, self.poll_interval)
        )

    async def _poll_loop(self):
        """Repeatedly poll while running."""
        import asyncio
        import logging

        log = logging.getLogger(__name__)

        while self._running:
            try:
                events = await self.poll_once()
                if self._on_message:
                    for event in events:
                        await self._on_message(event)
            except RocketChatRateLimitError as exc:
                delay = self._sleep_after_error(exc)
                log.warning(
                    "Rocket.Chat polling rate limited; backing off for %.1f seconds",
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except Exception as exc:
                # Definitive auth failure (401/invalid token): the credential
                # may have rotated server-side — try re-authenticating once
                # before giving up; a still-invalid credential is reported as
                # a fatal error so the gateway exits instead of logging 401s
                # forever with no recovery path.
                if _is_auth_failure_message(str(exc)):
                    log.warning("Polling auth failure (%s); re-authenticating", exc)
                    try:
                        await self._client.initialize()
                        log.info("Re-authentication succeeded")
                    except Exception as reauth_exc:
                        log.exception("Re-authentication failed")
                        callback = self._on_auth_failure
                        if callable(callback):
                            try:
                                callback(str(reauth_exc))
                            except Exception:
                                pass
                        await asyncio.sleep(self.poll_interval)
                        continue
                else:
                    log.exception("Polling error")
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> list[dict]:
        """Execute one poll cycle and return new inbound events."""
        import time

        events: list[dict] = []

        # 1. Get subscriptions updated since last check
        updates: list[tuple[str, dict]] = []
        oldest_checkpoint = self.checkpoint_store.oldest()
        subscriptions = await self._client.list_subscriptions(
            updated_since=oldest_checkpoint
        )
        for sub in subscriptions or []:
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

            data = await self._client.sync_messages(
                room_id,
                last_update=last_str,
                room_type=sub.get("t", ""),
            )
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

                self._seen_ids[msg_id] = time.monotonic()
                events.append(msg)

            # Advance checkpoint to subscription _updatedAt
            updated_at = sub.get("_updatedAt", "")
            if updated_at:
                self.checkpoint_store.save(room_id, updated_at)

        return events


# ---------------------------------------------------------------------------
# Hermes integration stubs (fallback when Hermes is not installed)
# ---------------------------------------------------------------------------

# Resolve the Hermes gateway so we can import from ``gateway.platforms.base``
# the same way built-in platform plugins do (e.g. Telegram).
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_HERMES_AGENT = _Path.home() / ".hermes" / "hermes-agent"
if str(_HERMES_AGENT) not in _sys.path:
    _sys.path.insert(0, str(_HERMES_AGENT))

try:
    from gateway.config import Platform  # type: ignore[import-not-found]  # noqa: F401
    from gateway.platforms.base import (  # type: ignore[import-not-found]
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
        cache_image_from_url,
        transcode_to_ogg_opus,
    )
except ImportError:
    # Test-friendly stubs that mirror the Hermes base adapter interface

    async def cache_image_from_url(url: str, ext: str = ".jpg") -> str:
        """Hermes image-cache downloader stub (unavailable without Hermes)."""
        raise RocketChatClientError("Image download unavailable (Hermes not installed)")

    def transcode_to_ogg_opus(path: str, **kwargs: Any) -> str | None:
        """Hermes voice-transcode helper stub (unavailable without Hermes)."""
        return None

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
        # Per-attachment text-inlining contract (Hermes 00394acfae): False
        # means the text/* attachment was cached but NOT inlined into
        # ``text``, so the gateway tells the agent to read the file itself.
        media_text_inlined: list[Any | None] = field(default_factory=list)
        reply_to_message_id: str = ""
        reply_to_text: str = ""
        reply_to_author_id: str = ""
        reply_to_author_name: str = ""
        reply_to_is_own_message: bool = False
        raw_payload: dict[str, Any] | None = None
        platform: str = "rocketchat"

    @dataclass
    class SendResult:
        """Hermes SendResult stub."""

        success: bool = False
        message_id: str = ""
        error: str = ""

    class BasePlatformAdapter:
        """Minimal Hermes BasePlatformAdapter stub for isolated testing.

        Accepts the same ``config`` and ``platform`` values as the real base
        class while remaining convenient for isolated tests.
        """

        def __init__(self, config: Any = None, platform: Any = None):
            self.config = config
            self.platform = platform
            self._connected = False
            self._message_handler: Any = None
            self._status_text: dict[str, str] = {}
            self._fatal_error_code: str | None = None
            self._fatal_error_message: str | None = None
            self._fatal_error_retryable = True

        @property
        def is_connected(self) -> bool:
            return self._connected

        @property
        def has_fatal_error(self) -> bool:
            return self._fatal_error_message is not None

        @property
        def fatal_error_message(self) -> str | None:
            return self._fatal_error_message

        @property
        def fatal_error_code(self) -> str | None:
            return self._fatal_error_code

        @property
        def fatal_error_retryable(self) -> bool:
            return self._fatal_error_retryable

        def _set_fatal_error(
            self, code: str, message: str, *, retryable: bool = True
        ) -> None:
            self._fatal_error_code = code
            self._fatal_error_message = message
            self._fatal_error_retryable = retryable

        def set_message_handler(self, handler: Any) -> None:
            self._message_handler = handler

        def set_status_text(self, chat_id: str, text: str | None) -> None:
            """Set or clear the live working-state phrase for a chat."""
            if text:
                self._status_text[str(chat_id)] = text
            else:
                self._status_text.pop(str(chat_id), None)

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
            if self._message_handler is not None:
                await self._message_handler(event)

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
    if url.startswith("http://"):
        return url.replace("http://", "ws://", 1) + "/websocket"
    # Scheme-less config (e.g. "chat.example.com:3000"): assume http.
    return "ws://" + url + "/websocket"


class WebSocketTransport:
    """DDP WebSocket inbound transport for Rocket.Chat.

    Connects to Rocket.Chat's WebSocket endpoint, performs the DDP handshake
    (connect, login, subscribe), and emits normalized inbound message events.

    Reconnect strategy
    ------------------
    On disconnection the transport reconnects with exponential backoff plus
    jitter, so a server that is down for hours does not flood the logs or
    hammer the endpoint.  When the handshake reports an auth/token error the
    client is re-authenticated before the next attempt, so a server restart
    that invalidates the resume token is recovered automatically.

    A receive timeout + DDP ping heartbeat lets the transport detect a
    silently dropped connection (e.g. the server was powered off without
    sending a TCP FIN) instead of blocking forever on ``receive()``.

    Parameters
    ----------
    client:
        A ``RocketChatClient`` instance (or compatible) for REST calls such as
        listing subscriptions and re-authentication.
    ws_url:
        The WebSocket endpoint URL.  Defaults to ``_ws_url(client.server_url)``.
    ws_factory:
        Optional async callable that returns a WebSocket connection object.
        When *None* (the default) a real aiohttp ``ws_connect`` is used.
    receive_timeout:
        Seconds to wait for a frame before sending a DDP ping.  Default 60.
    ping_timeout:
        Seconds to wait for any frame after sending a ping before declaring
        the connection dead.  Default 10.
    reconnect_initial_delay:
        Initial reconnect delay in seconds.  Default 1.
    reconnect_max_delay:
        Cap for the exponential backoff in seconds.  Default 60.
    reconnect_max_attempts:
        Maximum reconnect attempts before giving up.  ``0`` means unlimited.
    reconnect_jitter:
        Jitter fraction (0..0.5) applied to each delay.  Default 0.25.
    """

    def __init__(
        self,
        client: Any,
        ws_url: str = "",
        ws_factory: Any = None,
        *,
        receive_timeout: float = 60.0,
        ping_timeout: float = 10.0,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
        reconnect_max_attempts: int = 0,
        reconnect_jitter: float = 0.25,
        on_auth_failure: Any = None,
        subscription_refresh_seconds: float = 300.0,
    ):
        self._client = client
        self.ws_url = ws_url or _ws_url(client.server_url)
        self._ws_factory = ws_factory or self._default_ws_factory
        self._on_message: Any = None
        self._on_status: Any = None
        self._on_auth_failure: Any = on_auth_failure
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        # Bounded in-memory dedup / room / subscription registries so a
        # long-running gateway stays flat (BoundedDict evicts oldest first).
        self._seen_ids: BoundedDict[str, float] = BoundedDict(maxsize=100_000)
        self._sub_ids: BoundedDict[str, float] = BoundedDict(maxsize=4_000)
        # Cache room type from subscriptions so we can tag inbound events
        self._room_types: BoundedDict[str, str] = BoundedDict(maxsize=5_000)
        # Rooms we have subscribed stream-room-messages for on the CURRENT
        # connection (reset on reconnect so bootstrap re-subscribes).
        self._subscribed_rooms: set[str] = set()
        # Periodic subscription refresh so rooms/DMs created after connect
        # are picked up without a reconnect.
        self._subscription_refresh_seconds = max(
            10.0, _parse_float_safe(subscription_refresh_seconds, 300.0)
        )
        self._subscription_refresh_task: asyncio.Task[Any] | None = None
        # Heartbeat / reconnect tuning (defensive: config may already be parsed)
        self._receive_timeout = max(0.01, _parse_float_safe(receive_timeout, 60.0))
        self._ping_timeout = max(0.01, _parse_float_safe(ping_timeout, 10.0))
        self._initial_delay = max(0.01, _parse_float_safe(reconnect_initial_delay, 1.0))
        self._max_delay = max(
            self._initial_delay, _parse_float_safe(reconnect_max_delay, 60.0)
        )
        self._max_attempts = max(0, _parse_int_safe(reconnect_max_attempts, 0))
        self._jitter = max(0.0, min(0.5, _parse_float_safe(reconnect_jitter, 0.25)))
        # Reconnect bookkeeping (reset on a successful handshake)
        self._reconnect_attempts = 0
        # aiohttp session owned by the default factory, closed on reconnect/stop
        self._http_session: Any = None

    # -- public API -----------------------------------------------------------

    def set_on_message(self, callback: Any) -> None:
        """Register an async callback ``callback(event: dict)`` for inbound messages."""
        self._on_message = callback

    def set_on_status(self, callback: Any) -> None:
        """Register an async callback ``callback(status: str, detail: dict)``.

        Status is one of ``"connected"``, ``"reconnecting"``, ``"stopped"``,
        or ``"failed"``.  ``detail`` carries attempt count / delay / reason.
        """
        self._on_status = callback

    async def start(self) -> None:
        """Begin the WebSocket receive loop in the background."""
        self._running = True
        self._reconnect_attempts = 0
        self._task = asyncio.create_task(self._receive_loop())

    async def stop(self) -> None:
        """Stop the WebSocket loop and release any aiohttp session."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError as _exc:
                pass
            except Exception as _exc:
                pass
        await self._close_http_session()
        self._emit_status("stopped", {"reason": "stop requested"})

    # -- status helper --------------------------------------------------------

    def _emit_status(self, status: str, detail: dict[str, Any] | None = None) -> None:
        """Best-effort status notification to the registered callback."""
        if self._on_status is None:
            return
        try:
            self._on_status(status, detail or {})
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Error in on_status callback")

    # -- backoff helper -------------------------------------------------------

    def _next_reconnect_delay(self) -> float:
        """Compute the next reconnect delay using exponential backoff + jitter."""
        import random

        base = self._initial_delay * (2**self._reconnect_attempts)
        base = min(base, self._max_delay)
        jitter = base * self._jitter
        return base + random.uniform(-jitter, jitter)

    # -- WebSocket factory ----------------------------------------------------

    async def _default_ws_factory(self) -> Any:
        """Create a real aiohttp WebSocket connection.

        Closes any previously created ``ClientSession`` first so that repeated
        reconnects do not leak sessions / file descriptors.
        """
        try:
            import aiohttp  # type: ignore[reportMissingImports]
        except ImportError:
            raise RuntimeError(
                "WebSocket transport requires aiohttp. Install with: pip install aiohttp"
            )

        # Close the previous session before opening a new one to avoid fd leaks
        await self._close_http_session()
        session = aiohttp.ClientSession()
        self._http_session = session
        return await session.ws_connect(self.ws_url)

    async def _close_http_session(self) -> None:
        """Close the aiohttp session owned by the default factory, if any."""
        session = self._http_session
        self._http_session = None
        if session is None:
            return
        try:
            await session.close()
        except Exception:
            pass

    # -- receive loop ---------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Main receive loop with heartbeat + exponential-backoff reconnect."""
        import logging

        log = logging.getLogger(__name__)

        while self._running:
            ws = None
            try:
                ws = await self._ws_factory()
                await self._handshake(ws)
                await self._bootstrap_subscriptions(ws)
                self._subscription_refresh_task = asyncio.create_task(
                    self._subscription_refresh_loop(ws)
                )

                # Successful connect: reset backoff and notify
                self._reconnect_attempts = 0
                self._emit_status("connected", {})

                await self._read_loop(ws)

            except asyncio.CancelledError as _exc:
                raise
            except Exception as exc:
                await self._handle_connection_error(exc, log)
            finally:
                refresh_task = self._subscription_refresh_task
                self._subscription_refresh_task = None
                if refresh_task is not None:
                    refresh_task.cancel()
                    try:
                        await refresh_task
                    except (asyncio.CancelledError, Exception):
                        pass
                self._subscribed_rooms.clear()
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception as _close_exc:
                        pass
                # Always release the session tied to this attempt
                await self._close_http_session()

    async def _read_loop(self, ws: Any) -> None:
        """Read frames with a heartbeat timeout.

        If no frame arrives within ``receive_timeout``, send a DDP ping and
        wait up to ``ping_timeout`` for any response.  No response means the
        connection is silently dead → raise ``ConnectionError`` to trigger
        reconnect.
        """
        while self._running:
            try:
                frame = await asyncio.wait_for(
                    _ws_recv_text(ws), timeout=self._receive_timeout
                )
            except asyncio.TimeoutError as _timeout:
                # No traffic for a while — probe the connection with a ping
                await _ws_send_text(ws, json.dumps({"msg": "ping"}))
                try:
                    frame = await asyncio.wait_for(
                        _ws_recv_text(ws), timeout=self._ping_timeout
                    )
                except asyncio.TimeoutError as _ping_timeout:
                    raise ConnectionError(
                        f"WebSocket unresponsive: no frame within "
                        f"{self._receive_timeout}s + ping {self._ping_timeout}s"
                    )
                # A real frame may have landed during the probe — dispatch it
                # instead of discarding it.
                await self._handle_frame(frame, ws)
                continue
            await self._handle_frame(frame, ws)

    async def _handle_connection_error(self, exc: Exception, log: Any) -> None:
        """Handle a connection/auth error between reconnect attempts.

        Extracted from ``_receive_loop`` so boolean logic lives outside the
        ``except`` body (lint: no-boolean-in-except).  Re-authenticates on
        auth errors, applies backoff, and emits status.  Stops the loop when
        the max-attempt cap is reached.
        """
        # Auth errors: re-authenticate before the next attempt so a server
        # restart that invalidated the resume token recovers.
        if self._is_auth_error(exc):
            log.warning("WebSocket auth error (%s); re-authenticating", exc)
            await self._reauthenticate()
        if not self._running:
            return
        self._reconnect_attempts += 1
        reached_cap = (
            self._max_attempts > 0 and self._reconnect_attempts > self._max_attempts
        )
        if reached_cap:
            log.error(
                "WebSocket reconnect gave up after %d attempts",
                self._reconnect_attempts,
            )
            self._emit_status(
                "failed", {"attempts": self._reconnect_attempts, "reason": str(exc)}
            )
            self._running = False
            return
        delay = self._next_reconnect_delay()
        log.warning(
            "WebSocket error (%s); reconnecting in %.1fs (attempt %d)",
            exc,
            delay,
            self._reconnect_attempts,
        )
        self._emit_status(
            "reconnecting",
            {"attempt": self._reconnect_attempts, "delay": delay, "reason": str(exc)},
        )
        await asyncio.sleep(delay)

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """Heuristic: does this error indicate the resume token is invalid?"""
        text = str(exc).lower()
        return (
            "auth" in text
            or "401" in text
            or "unauthorized" in text
            or "token" in text
            or "resume" in text
        )

    async def _reauthenticate(self) -> None:
        """Re-run client initialization to obtain a fresh auth token."""
        try:
            await self._client.initialize()
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Re-authentication failed; will retry on next reconnect"
            )
            # Surface the failure to the adapter so a permanently invalid
            # credential can be reported as a fatal (non-retryable) error.
            callback = self._on_auth_failure
            if callable(callback):
                try:
                    callback(str(exc))
                except Exception:
                    pass

    # -- DDP handshake --------------------------------------------------------

    async def _handshake(self, ws: Any) -> None:
        """Perform DDP ``connect`` → ``connected`` → ``login``."""

        # 1. Send connect
        await _ws_send_text(
            ws,
            json.dumps(
                {
                    "msg": "connect",
                    "version": "1",
                    "support": ["1", "pre2", "pre1"],
                }
            ),
        )

        # 2. Wait for "connected"
        while self._running:
            frame = await asyncio.wait_for(_ws_recv_text(ws), self._receive_timeout)
            msg = _decode_ddp_frame(frame)
            if msg is None:
                continue

            if msg.get("msg") == "connected":
                break
            if msg.get("msg") == "failed":
                raise RocketChatClientError(
                    f"DDP handshake failed (version mismatch): {str(msg)[:200]}"
                )
            if msg.get("msg") == "ping":
                await _ws_send_text(ws, json.dumps({"msg": "pong"}))

        # 3. Send login
        await _ws_send_text(
            ws,
            json.dumps(
                {
                    "msg": "method",
                    "method": "login",
                    "params": [{"resume": self._client._access_token}],
                    "id": "1",
                }
            ),
        )

        # 4. Wait for login result
        while self._running:
            frame = await asyncio.wait_for(_ws_recv_text(ws), self._receive_timeout)
            msg = _decode_ddp_frame(frame)
            if msg is None:
                continue

            if msg.get("msg") == "result" and msg.get("id") == "1":
                if msg.get("error"):
                    # Resume tokens die (server restart, token rotation).
                    # Surface it as an auth error so the normal re-auth /
                    # fatal-error plumbing runs instead of a silent dead socket.
                    error = msg.get("error") or {}
                    detail = (
                        error.get("message") or error.get("error") or str(error)[:200]
                        if isinstance(error, dict)
                        else str(error)[:200]
                    )
                    raise RocketChatClientError(
                        f"DDP login failed (resume token rejected): {detail}"
                    )
                break
            if msg.get("msg") == "ping":
                await _ws_send_text(ws, json.dumps({"msg": "pong"}))

    # -- subscriptions --------------------------------------------------------

    async def _subscription_refresh_loop(self, ws: Any) -> None:
        """Periodically subscribe to rooms/DMs created after connect."""
        import logging

        log = logging.getLogger(__name__)
        while self._running and not getattr(ws, "closed", False):
            await asyncio.sleep(self._subscription_refresh_seconds)
            try:
                await self._refresh_subscriptions(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning(
                    "Rocket.Chat subscription refresh failed; will retry later",
                    exc_info=True,
                )

    async def _refresh_subscriptions(self, ws: Any) -> None:
        """Fetch subscriptions and subscribe to any new rooms.

        Also refreshes the cached room-type map used to tag inbound events.
        """
        import time

        subscriptions = await self._client.list_subscriptions()
        for sub in subscriptions or []:
            room_id = sub.get("rid") or sub.get("_id", "")
            if not room_id or room_id in self._subscribed_rooms:
                continue

            # Cache room type
            room_type = sub.get("t", "")
            if room_type:
                self._room_types[room_id] = room_type

            sub_id = f"sub-{room_id}"
            self._sub_ids[sub_id] = time.monotonic()
            self._subscribed_rooms.add(room_id)
            await _ws_send_text(
                ws,
                json.dumps(
                    {
                        "msg": "sub",
                        "id": sub_id,
                        "name": "stream-room-messages",
                        "params": [room_id, False],
                    }
                ),
            )

    async def _bootstrap_subscriptions(self, ws: Any) -> None:
        """Subscribe to ``stream-room-messages`` for every joined room."""
        await self._refresh_subscriptions(ws)

    # -- frame dispatch -------------------------------------------------------

    async def _handle_frame(self, frame: str, ws: Any) -> None:
        """Dispatch a single DDP frame."""
        msg = _decode_ddp_frame(frame)
        if msg is None:
            return
        msg_type = msg.get("msg", "")

        if msg_type == "ping":
            await _ws_send_text(ws, json.dumps({"msg": "pong"}))
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

        # Deduplicate (bounded: oldest seen ids are evicted first)
        import time

        if msg_id and msg_id in self._seen_ids:
            return
        if msg_id:
            self._seen_ids[msg_id] = time.monotonic()

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


def _resolve_hermes_platform() -> Any:
    """Return Hermes' dynamic Platform value when available."""
    platform_cls = globals().get("Platform")
    if platform_cls is None:
        return "rocketchat"
    try:
        return platform_cls("rocketchat")
    except Exception:
        return "rocketchat"


class RocketChatAdapter(BasePlatformAdapter):  # type: ignore[reportGeneralTypeIssues]
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

    # The thinking placeholder is a real message that can be edited, so the
    # gateway's live per-tool status phrases are rendered into it.
    supports_status_text: bool = True

    # Rocket.Chat renders markdown, including fenced code blocks, so the
    # gateway may present tool progress as a bare fenced block.
    supports_code_blocks: bool = True

    # The adapter splits oversized content natively in send(); the delivery
    # router then skips its own gateway-level truncation.
    splits_long_messages: bool = True

    def __init__(self, config: Any = None):
        self._message_handler: Any = None
        self._client: RocketChatClient | None = None
        self._transport: Any = None
        self._room_info: BoundedDict[str, dict[str, Any]] = BoundedDict(maxsize=200)
        self._typing_placeholders: BoundedDict[str, str] = BoundedDict(maxsize=500)
        # Live stream-preview markers: placeholder key → message id of a
        # message that is currently being edited by the Hermes stream
        # consumer.  While a preview is live, send_typing() must NOT create a
        # second "Thinking…" bubble — the growing preview is the indicator.
        self._stream_previews: BoundedDict[str, str] = BoundedDict(maxsize=200)
        # Last status phrase rendered into each placeholder (key → text), so
        # repeated typing refreshes only edit the placeholder when the live
        # status actually changed.
        self._last_status_text: BoundedDict[str, str] = BoundedDict(maxsize=500)
        # Thread parent-message cache for reply-context backfill (message id →
        # normalized context dict), bounded so long-running gateways stay flat.
        # Thread parent-message cache for reply-context backfill (message id →
        # (timestamp, context-or-None)), bounded so long-running gateways
        # stay flat; None marks a negative (deleted parent) lookup.
        self._reply_cache: BoundedDict[str, tuple[float, dict[str, Any] | None]] = (
            BoundedDict(maxsize=200)
        )
        self._cfg: RocketChatConfig | None = None
        self._seen_id_store: PersistentSeenIdStore | None = None
        # Monotonic timestamp of the last seen-id disk flush (throttled).
        self._last_dedup_flush = 0.0

        super().__init__(config=config, platform=_resolve_hermes_platform())

        # Parse our own Rocket.Chat configuration
        if config is not None:
            if hasattr(config, "extra"):
                extra: dict[str, Any] = dict(getattr(config, "extra", {}))
                self._cfg = parse_config(extra)
            elif isinstance(config, RocketChatConfig):
                self._cfg = config
            elif isinstance(config, dict):
                self._cfg = parse_config(config)

        # Build the persistent seen-id store for WebSocket dedup.  Done here
        # so the store is ready before ``connect`` runs and survives reconnects
        # within the adapter's lifetime.
        self._init_seen_id_store()

    # -- lifecycle ------------------------------------------------------------

    def _init_seen_id_store(self) -> None:
        """Create the persistent seen-id store for WebSocket inbound dedup.

        Only active under the WebSocket transport; polling has its own
        checkpoint-based dedup and never replays.  The store path defaults to
        ``<HERMES_HOME>/rocketchat_seen_ids.json`` so it survives gateway
        restarts.
        """
        cfg = self._cfg
        if cfg is None or not cfg.dedup_enabled:
            self._seen_id_store = None
            return
        if cfg.transport.lower() != "websocket":
            self._seen_id_store = None
            return
        path = cfg.dedup_store_path
        if not path:
            hermes_home = os.environ.get("HERMES_HOME", "")
            if hermes_home:
                path = os.path.join(hermes_home, "rocketchat_seen_ids.json")
            else:
                path = os.path.join(
                    os.path.expanduser("~"), ".hermes", "rocketchat_seen_ids.json"
                )
        self._seen_id_store = PersistentSeenIdStore(
            path=path,
            ttl_seconds=max(1.0, cfg.dedup_ttl_hours) * 3600,
        )

    def set_message_handler(self, handler: Any) -> None:
        """Store the Hermes message handler for inbound dispatch."""
        self._message_handler = handler

    def _on_transport_status(self, status: str, detail: dict[str, Any]) -> None:
        """Log WebSocket transport status changes for observability."""
        import logging

        log = logging.getLogger(__name__)
        if status == "connected":
            log.info("Rocket.Chat WebSocket connected")
        elif status == "reconnecting":
            log.warning(
                "Rocket.Chat WebSocket reconnecting (attempt %s, delay %.1fs): %s",
                detail.get("attempt"),
                detail.get("delay", 0.0),
                detail.get("reason", ""),
            )
        elif status == "failed":
            log.error(
                "Rocket.Chat WebSocket reconnect failed after %s attempts: %s",
                detail.get("attempts"),
                detail.get("reason", ""),
            )
        elif status == "stopped":
            log.info("Rocket.Chat WebSocket transport stopped")

    async def connect(self, *args: Any, **kwargs: Any) -> bool:
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
            # A definitive credential problem (401/403, invalid token/response)
            # can never succeed by retrying — report it as non-retryable so the
            # gateway exits instead of reconnecting forever.  Transient network
            # failures stay retryable and return False without fatal metadata.
            if _is_auth_failure_message(str(exc)):
                self._mark_auth_fatal(str(exc))
            return False

        # Choose transport
        transport_type = self._cfg.transport.lower()

        if transport_type == "websocket":
            self._transport = WebSocketTransport(
                client=self._client,
                receive_timeout=self._cfg.receive_timeout,
                ping_timeout=self._cfg.ping_timeout,
                reconnect_initial_delay=self._cfg.reconnect_initial_delay,
                reconnect_max_delay=self._cfg.reconnect_max_delay,
                reconnect_max_attempts=self._cfg.reconnect_max_attempts,
                reconnect_jitter=self._cfg.reconnect_jitter,
                on_auth_failure=self._mark_auth_fatal,
                subscription_refresh_seconds=self._cfg.subscription_refresh_seconds,
            )
            # Surface connection status in the adapter log for observability
            self._transport.set_on_status(self._on_transport_status)
        else:
            self._transport = PollingTransport(
                client=self._client,
                poll_interval=self._cfg.poll_interval_seconds,
                on_auth_failure=self._mark_auth_fatal,
            )

        # Wire inbound callback
        self._transport.set_on_message(self._on_inbound)

        # Start transport
        await self._transport.start()
        self._connected = True
        self._running = True

        # Hermes plugin-native handler boundary (272f4e4abe): invoke factories
        # registered by other plugins via ``ctx.register_platform_handler(
        # "rocketchat", ...)``.  Rocket.Chat has no separate native SDK app
        # object, so we pass ``None``; the base isolates per-factory failures.
        wire = getattr(self, "_wire_plugin_handlers", None)
        if callable(wire):
            try:
                wire(None)
            except Exception:
                log.exception("[rocketchat] plugin handler wiring failed")

        return True

    async def disconnect(self) -> None:
        """Stop the transport and disconnect from Rocket.Chat."""
        if self._transport is not None:
            await self._transport.stop()
            self._transport = None
        # Force-persist any pending seen-ids (the periodic flush is throttled).
        if self._seen_id_store is not None:
            try:
                self._seen_id_store.flush()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Failed to flush seen-id store on disconnect"
                )
        if self._client is not None:
            closer = getattr(self._client, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    pass
        self._connected = False
        self._running = False

    # -- send -----------------------------------------------------------------

    def _metadata_thread_id(self, metadata: dict[str, Any] | None) -> str:
        """Return the explicit Hermes/Rocket.Chat thread id from metadata."""
        if isinstance(metadata, dict):
            return str(metadata.get("thread_id") or "")
        return ""

    def _bot_user_id(self) -> str:
        """Return the authenticated bot user id, or an empty string."""
        if self._client is not None and self._client.identity is not None:
            return str(getattr(self._client.identity, "user_id", "") or "")
        return ""

    def _typing_placeholder_key(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Build a stable placeholder key for a room or room thread."""
        return f"{chat_id}\u0000{self._metadata_thread_id(metadata)}"

    def _should_consume_typing_placeholder(
        self,
        key: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Return True when a send may take over the thinking placeholder.

        Any existing placeholder is consumable — including on final
        (non-streamed) sends — so a "💭 Thinking…" bubble never survives as
        a ghost next to the real reply.  The one exclusion is a live stream
        preview for the same (chat, thread): the growing preview IS the
        indicator and must not be replaced.
        """
        if key in self._stream_previews:
            return False
        return key in self._typing_placeholders

    def _render_status_phrase(self, chat_id: str) -> str:
        """Return the placeholder text for a chat, embedding any live status.

        The gateway feeds per-tool phrases ("is running pytest…") via
        ``set_status_text``; when set, they are appended to the thinking
        placeholder so channel users see what the bot is doing.
        """
        store = getattr(self, "_status_text", None)
        phrase = (store or {}).get(str(chat_id))
        if phrase:
            return f"{THINKING_PLACEHOLDER_TEXT} {phrase}"
        return THINKING_PLACEHOLDER_TEXT

    async def send_typing(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create one visible thinking placeholder for the active turn.

        Renders the current live status phrase; a later refresh edits the
        placeholder only when the phrase changed.  Skipped while a stream
        preview is being edited for the same (chat, thread): the growing
        preview already tells the user the bot is working.
        """
        if not self._connected or self._client is None:
            return

        key = self._typing_placeholder_key(chat_id, metadata)
        if key in self._stream_previews:
            return

        status_text = self._render_status_phrase(chat_id)

        existing = self._typing_placeholders.get(key)
        if existing:
            # Refresh the placeholder only when the live status changed.
            if self._last_status_text.get(key) != status_text:
                try:
                    await self._client.update_message(
                        room_id=chat_id,
                        message_id=existing,
                        text=status_text,
                    )
                    self._last_status_text[key] = status_text
                except RocketChatClientError:
                    # Keep the placeholder; the final send still edits it.
                    pass
            return

        try:
            result = await self._client.post_message(
                room_id=chat_id,
                text=status_text,
                tmid=self._metadata_thread_id(metadata),
            )
        except RocketChatClientError:
            import logging

            logging.getLogger(__name__).warning(
                "[rocketchat] send_typing failed to create thinking placeholder",
                exc_info=True,
            )
            return
        message_id = str(result.get("_id") or "")
        if message_id:
            self._typing_placeholders[key] = message_id
            self._last_status_text[key] = status_text

    async def stop_typing(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Clear stream-preview markers so the next turn can start fresh.

        The placeholder message itself is kept: the final send edits it
        (Hermes calls stop_typing after the agent finishes but before the
        final response is delivered).

        With ``metadata`` the cleanup is scoped to the exact (chat, thread);
        without it (the base signature), all threads of the chat are cleared
        for backward compatibility.
        """
        if metadata is not None:
            key = self._typing_placeholder_key(chat_id, metadata)
            self._stream_previews.pop(key, None)
            return
        prefix = f"{chat_id}\u0000"
        for key in [k for k in self._stream_previews if k.startswith(prefix)]:
            self._stream_previews.pop(key, None)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str = "",
        media_files: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a text message (and optionally media files) to a Rocket.Chat room.

        Returns a ``SendResult`` indicating success or failure.  Transient
        failures (not connected, network errors a reconnect can cure) return
        the ``send_path_degraded`` error code so Hermes' delivery ledger
        replays them once the gateway reconnects (Hermes 8e1db41041).
        """
        if not self._connected or self._client is None:
            return SendResult(
                success=False,
                error="send_path_degraded",
            )

        try:
            # Chunk oversized content at paragraph/line boundaries instead of
            # hard-truncating it (splits_long_messages = True).  Every chunk
            # is posted into the same room/thread; the first chunk consumes
            # the thinking placeholder when present.
            text = content
            max_len = self._cfg.max_message_length if self._cfg else 4000
            # 0 / negative means "no limit" (Hermes registry semantics) and
            # must never reach the chunker (which would loop at max_len=0).
            if max_len > 0 and _utf16_units(text) > max_len:
                chunks = self._split_long_text(text, max_len)
            else:
                chunks = [text]
            if not text:
                # Media-only send: never post an empty message.
                chunks = []

            tmid = self._resolve_tmid(chat_id, reply_to, metadata)

            placeholder_key = self._typing_placeholder_key(chat_id, metadata)
            placeholder_id = self._typing_placeholders.get(placeholder_key)

            first_message_id = ""
            posted_any = False
            for index, chunk in enumerate(chunks):
                if (
                    index == 0
                    and placeholder_id
                    and self._should_consume_typing_placeholder(
                        placeholder_key, metadata
                    )
                ):
                    try:
                        result = await self._client.update_message(
                            room_id=chat_id,
                            message_id=placeholder_id,
                            text=chunk,
                        )
                    except RocketChatClientError:
                        # The placeholder vanished (edited/deleted elsewhere):
                        # drop the dead key and fall back to a fresh post so
                        # the reply still arrives.
                        self._typing_placeholders.pop(placeholder_key, None)
                        self._last_status_text.pop(placeholder_key, None)
                        result = await self._client.post_message(
                            room_id=chat_id,
                            text=chunk,
                            tmid=tmid,
                        )
                    self._typing_placeholders.pop(placeholder_key, None)
                    # First chunk of a streamed reply: remember the message id
                    # so edit_message() edits it and send_typing() stays quiet.
                    if metadata and metadata.get("expect_edits"):
                        self._stream_previews[placeholder_key] = result.get(
                            "_id", placeholder_id
                        )
                    first_message_id = result.get("_id", placeholder_id)
                else:
                    result = await self._client.post_message(
                        room_id=chat_id,
                        text=chunk,
                        tmid=tmid,
                    )
                    if not first_message_id:
                        first_message_id = result.get("_id", "")
                posted_any = True

            # Legacy media_files shim: deliver file attachments natively too
            # (Hermes' MEDIA: contract uses the dedicated send_* methods, but
            # direct callers may pass media_files to send()).
            for media_path in media_files or []:
                media_result = await self._send_media_file(
                    chat_id,
                    media_path,
                    reply_to=reply_to,
                    metadata=metadata,
                )
                if media_result.success:
                    if not first_message_id:
                        first_message_id = media_result.message_id
                    continue
                if first_message_id:
                    # Text already delivered; media failed: report a final
                    # partial failure (never the replayable code).
                    return SendResult(
                        success=False,
                        error=media_result.error,
                        message_id=first_message_id,
                    )
                return media_result

            return SendResult(
                success=True,
                message_id=first_message_id,
            )
        except RocketChatClientError as exc:
            if posted_any:
                # Partial delivery: report a FINAL failure carrying the last
                # posted chunk id.  The `send_path_degraded` replay code must
                # NOT be used here — the ledger would re-send the whole
                # message and duplicate the already-posted prefix.
                return SendResult(
                    success=False,
                    error=str(exc),
                    message_id=first_message_id,
                )
            if isinstance(exc, RocketChatRateLimitError) or _is_transient_client_error(
                str(exc)
            ):
                return SendResult(
                    success=False,
                    error="send_path_degraded",
                )
            return SendResult(
                success=False,
                error=str(exc),
            )
        except Exception as exc:
            if posted_any:
                return SendResult(
                    success=False,
                    error=f"Unexpected error: {exc}",
                    message_id=first_message_id,
                )
            return SendResult(
                success=False,
                error=f"Unexpected error: {exc}",
            )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Edit a previously sent message via Rocket.Chat ``chat.update``.

        Implemented so the Hermes stream consumer can grow a reply in place
        (live preview) instead of falling back to the non-streaming path.
        ``finalize`` is a no-op for Rocket.Chat (an edit is an edit); when
        True we clear the matching stream-preview marker so the next turn's
        send_typing() may create a fresh thinking placeholder.
        """
        if not self._connected or self._client is None:
            return SendResult(
                success=False,
                error="Adapter is not connected",
            )

        try:
            text = content
            max_len = self._cfg.max_message_length if self._cfg else 4000
            if max_len > 0:
                # Stream edits use the same UTF-16 budget as send() so the
                # final content is never server-truncated mid-word.
                text = _truncate_utf16(text, max_len)

            result = await self._client.update_message(
                room_id=chat_id,
                message_id=message_id,
                text=text,
            )

            if finalize:
                for key, preview_id in list(self._stream_previews.items()):
                    if preview_id == message_id:
                        self._stream_previews.pop(key, None)

            return SendResult(
                success=True,
                message_id=result.get("_id", message_id),
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

    def _resolve_tmid(
        self,
        chat_id: str,
        reply_to: str,
        metadata: dict[str, Any] | None,
    ) -> str:
        """Resolve the Rocket.Chat thread id (tmid) for an outbound send.

        Explicit Rocket.Chat/Hermes thread metadata wins.  Hermes' generic
        gateway reply anchor passes the triggering message id as reply_to for
        every platform; in Rocket.Chat that would hide normal replies inside
        threads.  Only use reply_to as tmid for direct adapter callers that
        did not provide gateway metadata.
        """
        metadata_thread_id = self._metadata_thread_id(metadata)
        if metadata_thread_id:
            return metadata_thread_id
        # ROCKETCHAT_FORCE_THREAD: always anchor replies into the triggering
        # message's thread.
        if self._cfg is not None and self._cfg.force_thread:
            return reply_to or ""
        if metadata is None:
            return reply_to or ""
        return ""

    @staticmethod
    def _split_long_text(text: str, max_len: int) -> list[str]:
        """Split *text* into chunks whose UTF-16 length fits *max_len*.

        Rocket.Chat limits message size in UTF-16 code units (JS string
        length — astral characters like emoji count as 2), so the budget is
        measured with ``_utf16_units``, never ``len()``.  Splits at paragraph
        (``\\n\\n``) boundaries first, then line (``\\n``) boundaries, then
        hard-splits any remaining single line (preferring a word boundary
        near the limit).  The concatenation ``"".join(chunks)`` always equals
        the input exactly — nothing is truncated or inserted.
        """
        if max_len <= 0:
            return [text]
        max_len = max(1, int(max_len))
        if _utf16_units(text) <= max_len:
            return [text]

        chunks: list[str] = []
        current = ""
        paragraphs = text.split("\n\n")
        n_paras = len(paragraphs)

        def _flush() -> None:
            nonlocal current
            if current:
                chunks.append(current)
                current = ""

        for pi, para in enumerate(paragraphs):
            sep = "\n\n" if pi < n_paras - 1 else ""
            if _utf16_units(current + para + sep) <= max_len:
                current += para + sep
                continue
            _flush()
            # Paragraph (with separator) alone may still fit after flushing.
            if _utf16_units(para + sep) <= max_len:
                current = para + sep
                continue
            # Paragraph too long: split by lines.
            lines = para.split("\n")
            n_lines = len(lines)
            for li, line in enumerate(lines):
                lsep = "\n" if li < n_lines - 1 else sep
                if _utf16_units(current + line + lsep) <= max_len:
                    current += line + lsep
                    continue
                _flush()
                if _utf16_units(line + lsep) <= max_len:
                    current = line + lsep
                    continue
                # Single line too long: hard-split at a word boundary inside
                # the UTF-16 budget.
                remaining = line + lsep
                while _utf16_units(remaining) > max_len:
                    piece = _prefix_within_units(remaining, max_len)
                    space = piece.rfind(" ")
                    if space > max_len // 2:
                        piece = piece[:space]
                    remaining = remaining[len(piece) :]
                    if not piece:
                        # Degenerate guard: never loop on a zero-width piece.
                        piece = remaining[:1]
                        remaining = remaining[1:]
                    chunks.append(piece)
                current = remaining

        _flush()
        return chunks or [text]

    # -- native outbound media delivery (Hermes MEDIA: contract) ---------------

    async def _send_media_file(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: str = "",
        reply_to: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Upload a local file via ``rooms.media`` and post it with a file ref.

        Shared implementation for every native media send (images, documents,
        video, audio, voice).  Returns a ``SendResult`` on success or failure;
        never leaks the host file path into chat.
        """
        if not self._connected or self._client is None:
            return SendResult(
                success=False,
                error="send_path_degraded",
            )

        try:
            tmid = self._resolve_tmid(chat_id, reply_to, metadata)
            result = await self._client.upload_attachment(
                room_id=chat_id,
                file_path=str(file_path),
                text=caption,
                tmid=tmid,
            )
            return SendResult(
                success=True,
                message_id=str(result.get("_id", "")),
            )
        except RocketChatClientError as exc:
            if isinstance(exc, RocketChatRateLimitError) or _is_transient_client_error(
                str(exc)
            ):
                return SendResult(
                    success=False,
                    error="send_path_degraded",
                )
            return SendResult(
                success=False,
                error=str(exc),
            )
        except Exception as exc:
            return SendResult(
                success=False,
                error=f"Unexpected error: {exc}",
            )

    @staticmethod
    def _media_path_from_source(src: str, default_ext: str = ".jpg") -> str:
        """Resolve an image/media source to a local file path.

        Accepts local paths, ``file://`` URIs (as the gateway passes for
        MEDIA delivery), and http(s) URLs.  Returns ``""`` for unsupported
        sources so callers can produce a clean failure.
        """
        if not src:
            return ""
        if src.startswith("file://"):
            from urllib.parse import unquote

            return unquote(src[7:])
        if src.startswith(("http://", "https://")):
            lower = src.lower().split("?")[0]
            for known in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
                if lower.endswith(known):
                    return src  # marker: still a URL; caller downloads
            return src
        if os.path.exists(src):
            return src
        return ""

    async def _download_media_url(self, url: str, ext: str = ".jpg") -> str:
        """Download a media URL to the Hermes image cache (SSRF-guarded).

        Delegates to Hermes' ``cache_image_from_url`` which validates the
        target and re-checks every redirect (blocks redirects to private
        addresses).  Raises ``RocketChatClientError`` on failure.
        """
        try:
            return await cache_image_from_url(url, ext)
        except RocketChatClientError:
            raise
        except Exception as exc:
            raise RocketChatClientError(f"Failed to download media: {exc}") from exc

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a local image file natively (rooms.media upload)."""
        return await self._send_media_file(
            chat_id,
            image_path,
            caption=caption or "",
            reply_to=reply_to or "",
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a document/file natively (rooms.media upload)."""
        return await self._send_media_file(
            chat_id,
            file_path,
            caption=caption or "",
            reply_to=reply_to or "",
            metadata=metadata,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a video file natively (rooms.media upload)."""
        return await self._send_media_file(
            chat_id,
            video_path,
            caption=caption or "",
            reply_to=reply_to or "",
            metadata=metadata,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        is_voice: bool = False,
        **kwargs: Any,
    ) -> SendResult:
        """Send an audio file natively (rooms.media upload).

        Hermes calls this with ``is_voice=True`` when the agent explicitly
        requested a voice bubble (ccc367dce0).  Rocket.Chat plays MP3/OGG
        voice messages natively, but the shared Hermes transcode helper is
        used best-effort to Ogg/Opus so mobile voice bubbles work for any
        source format; on transcode failure the original file is uploaded.
        """
        src = str(audio_path)
        if is_voice:
            ext = os.path.splitext(src)[1].lower()
            if ext not in (".ogg", ".opus"):
                import logging

                log = logging.getLogger(__name__)
                try:
                    converted = transcode_to_ogg_opus(src)
                except Exception:
                    converted = None
                if converted:
                    log.info(
                        "[rocketchat] transcoded voice to Ogg/Opus: %s -> %s",
                        src,
                        converted,
                    )
                    src = converted
                else:
                    log.warning(
                        "[rocketchat] voice transcode unavailable/failed; "
                        "uploading original: %s",
                        src,
                    )
        return await self._send_media_file(
            chat_id,
            src,
            caption=caption or "",
            reply_to=reply_to or "",
            metadata=metadata,
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send an image (URL, file:// URI, or local path) natively.

        http(s) sources are downloaded through Hermes' SSRF-guarded image
        cache (``cache_image_from_url``) before upload, so redirects to
        private addresses are blocked (mirrors Hermes f54e8706f).
        """
        src = self._media_path_from_source(image_url)
        if not src:
            return SendResult(
                success=False,
                error=f"Unsupported image source: {str(image_url)[:80]}",
            )
        if src.startswith(("http://", "https://")):
            try:
                ext = ".jpg"
                lower = src.lower().split("?")[0]
                for known in (".png", ".jpeg", ".gif", ".webp", ".svg"):
                    if lower.endswith(known):
                        ext = known
                        break
                src = await self._download_media_url(src, ext)
            except RocketChatClientError as exc:
                return SendResult(success=False, error=str(exc))
        return await self._send_media_file(
            chat_id,
            src,
            caption=caption or "",
            reply_to=reply_to or "",
            metadata=metadata,
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send an animated GIF natively (uploaded as a file)."""
        return await self.send_image(
            chat_id=chat_id,
            image_url=animation_url,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_multiple_images(
        self,
        chat_id: str,
        images: list[tuple[str, str]],
        metadata: dict[str, Any] | None = None,
        human_delay: float = 0.0,
    ) -> list[SendResult]:
        """Send a batch of images; each is uploaded natively in order.

        Unsupported sources yield a failed ``SendResult`` without aborting the
        remaining batch.
        """
        results: list[SendResult] = []
        for image_src, alt_text in images or []:
            if human_delay > 0:
                await asyncio.sleep(human_delay)
            try:
                results.append(
                    await self.send_image(
                        chat_id=chat_id,
                        image_url=image_src,
                        caption=alt_text or None,
                        metadata=metadata,
                    )
                )
            except Exception as exc:
                results.append(
                    SendResult(success=False, error=f"Unexpected error: {exc}")
                )
        return results

    # -- fatal error reporting -------------------------------------------------

    def _mark_auth_fatal(self, message: str) -> None:
        """Report a non-retryable authentication failure to the gateway.

        Uses the Hermes ``_set_fatal_error`` seam when the installed base
        supports it; degrades to a no-op on older Hermes versions (the
        adapter still fails closed by returning False from connect()).
        """
        if getattr(self, "has_fatal_error", False):
            return
        setter = getattr(self, "_set_fatal_error", None)
        if callable(setter):
            try:
                setter(
                    "AUTH_FAILED",
                    f"Rocket.Chat authentication failed: {message}",
                    retryable=False,
                )
            except Exception:
                pass

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
            room_type = str(event.get("_room_type") or "")

        # Room-type fallback: when the transport could not tag the room
        # (e.g. a DM created after the WebSocket subscribed), resolve it via
        # rooms.info so DMs are not mis-gated as channels.  Cached room
        # metadata doubles as get_chat_info()'s data source.
        if not room_type and self._client is not None:
            rid = str(event.get("rid") or "")
            if rid:
                room = self._room_info.get(rid)
                if room is None:
                    try:
                        room = await self._client.room_info(rid)
                        self._room_info[rid] = (
                            dict(room) if isinstance(room, dict) else {}
                        )
                    except (RocketChatClientError, AttributeError):
                        self._room_info[rid] = {}
                room_type = str(((room or {}).get("t", "")) or "")

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

        # Persistent dedup (WebSocket): suppress messages replayed by the
        # server after a reconnect/restart.  The transport's in-memory
        # _seen_ids is lost on reconnect, so this disk-backed store is the
        # authoritative guard against duplicate inbound delivery.
        msg_id = str(event.get("_id") or "")
        if self._seen_id_store is not None:
            if self._seen_id_store.contains(msg_id):
                import logging

                logging.getLogger(__name__).debug(
                    "Rocket.Chat inbound dedup: skipping already-seen msg_id=%s", msg_id
                )
                return
            if msg_id:
                self._seen_id_store.mark(msg_id)
                # Throttled persistence: a full-file write per message would
                # dominate a busy room; flush at most every 2 seconds and
                # force-persist on disconnect().
                import time as _time

                now = _time.monotonic()
                if now - self._last_dedup_flush >= 2.0:
                    self._seen_id_store.flush()
                    self._last_dedup_flush = now

        # Mention gating for non-DM rooms
        if chat_type != "dm":
            # Per-room override: configured rooms respond to every message
            # without needing a mention (mirrors Slack require_mention_channels).
            room_id = str(event.get("rid") or "")
            always_respond = bool(
                self._cfg and room_id in (self._cfg.always_respond_rooms or [])
            )
            if not always_respond:
                mention_names = self._cfg.mention_names if self._cfg else []
                ignore_other = bool(self._cfg and self._cfg.ignore_other_user_mentions)
                text = event.get("msg", "")
                mentions = event.get("mentions", [])

                if not should_handle_message(
                    room_type=room_type,
                    text=text,
                    mentions=mentions,
                    bot_user_id=bot_user_id,
                    bot_username=bot_username,
                    mention_names=mention_names,
                    ignore_other_user_mentions=ignore_other,
                ):
                    return

        # Resolve attachments
        media_urls: list[str] = []
        media_types: list[str] = []
        media_text_inlined: list[Any | None] = []
        if self._cfg and self._cfg.media_cache_dir:
            media_urls, media_types, media_text_inlined = await resolve_message_media(
                event, self._client, self._cfg.media_cache_dir
            )
        else:
            media_urls, media_types, media_text_inlined = await resolve_message_media(
                event, self._client
            )

        # Determine reply target
        reply_to = event.get("tmid", "")

        # Reply-context backfill: fetch the parent message of a thread reply
        # (cached per thread) so Hermes gets reply_to_text / author context.
        reply_context: dict[str, Any] = {}
        if reply_to:
            reply_context = await self._get_reply_context(reply_to)

        # Build source
        source = self._build_event_source(
            chat_id=event.get("rid", ""),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_name,
            room_type=room_type or "",
            room_name=event.get("rn", ""),
            thread_id=reply_to or "",
            message_id=event.get("_id", ""),
        )

        # Create Hermes MessageEvent
        message_event = self._build_message_event(
            source=source,
            raw_event=event,
            text=event.get("msg", ""),
            media_urls=media_urls,
            media_types=media_types,
            media_text_inlined=media_text_inlined,
            reply_to=reply_to,
            reply_context=reply_context,
        )

        await self.handle_message(message_event)

    # -- helpers --------------------------------------------------------------

    def _build_event_source(
        self,
        chat_id: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        room_type: str = "",
        room_name: str = "",
        thread_id: str = "",
        message_id: str = "",
    ) -> Any:
        """Build a real Hermes SessionSource when available."""
        try:
            return super().build_source(
                chat_id=chat_id,
                chat_name=room_name or chat_id,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                thread_id=thread_id,
                message_id=message_id,
            )
        except Exception:
            return self.build_source(
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                room_type=room_type,
                room_name=room_name,
            )

    async def _get_reply_context(self, message_id: str) -> dict[str, Any]:
        """Fetch (and cache) the parent message context of a thread reply.

        Returns a normalized dict with ``text``, ``author_id`` and
        ``author_name`` keys, or ``{}`` when the parent is unavailable
        (deleted, permission denied) — inbound delivery never blocks on it.

        Caching: positive entries live ``_REPLY_CACHE_TTL``; failed lookups
        are cached as negatives for ``_REPLY_NEGATIVE_TTL`` so deleted
        parents do not trigger a ``getMessage`` 404 on every reply.
        """
        import time as _time

        if not message_id or self._client is None:
            return {}
        cached = self._reply_cache.get(message_id)
        if cached is not None:
            ts, value = cached
            ttl = _REPLY_NEGATIVE_TTL if value is None else _REPLY_CACHE_TTL
            if _time.time() - ts < ttl:
                return {} if value is None else value
        try:
            parent = await self._client.get_message(message_id)
        except (RocketChatClientError, AttributeError):
            self._reply_cache[message_id] = (_time.time(), None)
            return {}
        author = parent.get("u") or {}
        context = {
            "text": str(parent.get("msg") or ""),
            "author_id": str(author.get("_id") or ""),
            "author_name": str(author.get("username") or ""),
        }
        self._reply_cache[message_id] = (_time.time(), context)
        return context

    def _build_message_event(
        self,
        source: Any,
        raw_event: dict[str, Any],
        text: str,
        media_urls: list[str],
        media_types: list[str],
        reply_to: str,
        reply_context: dict[str, Any] | None = None,
        media_text_inlined: list[Any | None] | None = None,
    ) -> MessageEvent:
        """Build a MessageEvent for both current Hermes and local stubs."""
        ctx = reply_context or {}
        inlined = media_text_inlined or []
        if isinstance(source, dict):
            return MessageEvent(
                chat_id=source["chat_id"],
                chat_type=source["chat_type"],
                user_id=source["user_id"],
                user_name=source["user_name"],
                text=text,
                media_urls=media_urls,
                media_types=media_types,
                media_text_inlined=inlined,
                reply_to_message_id=reply_to,
                reply_to_text=ctx.get("text", ""),
                reply_to_author_id=ctx.get("author_id", ""),
                reply_to_author_name=ctx.get("author_name", ""),
                reply_to_is_own_message=bool(
                    ctx.get("author_id") and ctx.get("author_id") == self._bot_user_id()
                ),
                raw_payload=raw_event,
                platform="rocketchat",
            )

        message_event_cls: Any = MessageEvent
        ctx = reply_context or {}
        message_event = message_event_cls(
            text=text,
            message_type=self._message_type_for_media(media_types),
            source=source,
            raw_message=raw_event,
            message_id=raw_event.get("_id", ""),
            media_urls=media_urls,
            media_types=media_types,
            media_text_inlined=inlined,
            reply_to_message_id=reply_to,
            reply_to_text=ctx.get("text", ""),
            reply_to_author_id=ctx.get("author_id", ""),
            reply_to_author_name=ctx.get("author_name", ""),
            reply_to_is_own_message=bool(
                ctx.get("author_id") and ctx.get("author_id") == self._bot_user_id()
            ),
        )

        # Compatibility for tests and older call sites that read flattened
        # source fields directly from MessageEvent.
        setattr(message_event, "chat_id", getattr(source, "chat_id", ""))
        setattr(message_event, "chat_type", getattr(source, "chat_type", ""))
        setattr(message_event, "user_id", getattr(source, "user_id", ""))
        setattr(message_event, "user_name", getattr(source, "user_name", ""))
        setattr(message_event, "raw_payload", raw_event)
        setattr(message_event, "platform", "rocketchat")
        return message_event

    def _message_type_for_media(self, media_types: list[str]) -> Any:
        """Choose the closest Hermes MessageType for inbound media."""
        if not media_types:
            return MessageType.TEXT
        if any(mt == "image" for mt in media_types):
            return getattr(
                MessageType, "PHOTO", getattr(MessageType, "MEDIA", MessageType.TEXT)
            )
        if any(mt == "video" for mt in media_types):
            return getattr(
                MessageType, "VIDEO", getattr(MessageType, "MEDIA", MessageType.TEXT)
            )
        if any(mt == "audio" for mt in media_types):
            return getattr(
                MessageType, "AUDIO", getattr(MessageType, "MEDIA", MessageType.TEXT)
            )
        return getattr(
            MessageType, "DOCUMENT", getattr(MessageType, "MEDIA", MessageType.TEXT)
        )

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


def _is_auth_failure_message(message: str) -> bool:
    """Return True when an error string indicates a definitive auth failure.

    Matches HTTP 401/403 responses and Rocket.Chat's invalid-token/credential
    phrasing.  Transient failures (timeouts, connection refused) never match,
    so the gateway keeps them retryable.
    """
    lowered = str(message).lower()
    markers = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid token",
        "invalid response",
        "invalid-user",
        "invalid user",
    )
    return any(marker in lowered for marker in markers)


def _is_transient_client_error(message: str) -> bool:
    """Return True when an error string looks like a transient network failure.

    Deployed at outbound send time: a reconnect can cure these, so the
    adapter returns the ``send_path_degraded`` error code Hermes' delivery
    ledger replays after the adapter reconnects (Hermes 8e1db41041).  Auth
    failures (handled by ``_is_auth_failure_message``) and other definitive
    errors stay as-is so they fail visibly.
    """
    lowered = str(message).lower()
    markers = (
        "timeout",
        "timed out",
        "connection",
        "not connected",
        "connection refused",
        "connection reset",
        "closed",
        "eof",
        "read of closed",
    )
    return any(marker in lowered for marker in markers)


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


# Delivery-target resolution cache for standalone sends (target → room id).
# Bounded so a long-lived process (gateway) stays flat.
_delivery_room_cache: BoundedDict[str, str] = BoundedDict(maxsize=200)


async def resolve_delivery_target(client: Any, target: str) -> str:
    """Resolve a standalone delivery target to a Rocket.Chat room id.

    A real room id passes through untouched (one ``rooms.info`` probe).
    Otherwise the target is treated as a user id or username: resolved via
    ``users.info`` and delivered to the direct room from ``dm.create`` —
    mirrors Hermes' Slack fix c7b9dfa96 (resolve user IDs to DM channels in
    standalone cron delivery).  When nothing resolves, the original target is
    returned so the caller's send fails with a clear platform error.
    """
    target = str(target or "")
    if not target:
        return target

    cached = _delivery_room_cache.get(target)
    if cached is not None:
        return cached

    # 1. Already a room id?
    try:
        room = await client.room_info(target)
        if room.get("_id"):
            _delivery_room_cache[target] = target
            return target
    except RocketChatClientError:
        pass

    # 2. A user id, then a username → direct room.
    username = ""
    try:
        user = await client.user_info(user_id=target)
        username = str(user.get("username") or "")
    except RocketChatClientError:
        pass
    if not username:
        try:
            user = await client.user_info(username=target)
            username = str(user.get("username") or "")
        except RocketChatClientError:
            pass

    if username:
        try:
            room_id = await client.create_direct_room(username)
            if room_id:
                _delivery_room_cache[target] = room_id
                return room_id
        except RocketChatClientError:
            pass

    return target


async def standalone_send(
    pconfig: dict[str, Any],
    chat_id: str,
    message: str,
    media_files: list[str] | None = None,
    *,
    thread_id: str | None = None,
    force_document: bool = False,
    _client_factory: Any = None,
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
    thread_id:
        Optional Rocket.Chat thread id; forwarded as ``tmid`` so deliveries
        land inside a thread (Hermes standalone-sender contract).
    force_document:
        Accepted for Hermes contract parity (``tools/send_message_tool``
        passes it).  Rocket.Chat's ``rooms.media`` flow auto-detects the
        attachment type server-side, so no separate document path exists.

    Returns
    -------
    dict
        ``{"success": bool, "message_id": str, "error": str}`` — compatible
        with the Hermes standalone-sender contract.
    """
    import logging

    log = logging.getLogger(__name__)

    client: Any = None
    try:
        cfg = parse_config(pconfig)

        if _client_factory is not None:
            client = _client_factory()
        else:
            client = RocketChatClient(
                server_url=cfg.server_url,
                user_id=cfg.user_id,
                access_token=cfg.access_token,
                username=cfg.username,
                password=cfg.password,
            )
        await client.initialize()

        # Resolve the delivery target to a room id: room ids pass through;
        # user ids / usernames resolve to a direct room (dm.create).
        room_id = await resolve_delivery_target(client, chat_id)

        tmid = str(thread_id or "")
        message_id = ""

        # Upload media files if any
        if media_files:
            for file_path in media_files:
                upload_attachment: Any = getattr(client, "upload_attachment")
                uploaded = await upload_attachment(
                    room_id=room_id,
                    file_path=file_path,
                    text=message,
                    tmid=tmid,
                )
                message_id = uploaded.get("_id", message_id)

        # Post text message (even when media was uploaded — may serve as caption)
        if message:
            result = await client.post_message(room_id=room_id, text=message, tmid=tmid)
            message_id = result.get("_id", message_id)

        return {"success": True, "message_id": message_id}

    except RocketChatClientError as exc:
        log.error("standalone_send client error: %s", exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        log.exception("standalone_send unexpected error")
        return {"success": False, "error": str(exc)}
    finally:
        # Release the HTTP session of self-created clients (injected test
        # clients stay owned by the caller).
        if _client_factory is None and client is not None:
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# File upload support (on RocketChatClient)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plugin registration (Hermes entry point)
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Check that aiohttp or httpx is available for the Rocket.Chat adapter.

    Hermes platform registry expects a plain boolean from ``check_fn``.
    """
    try:
        import aiohttp  # type: ignore[reportMissingImports]  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        import httpx  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        pass
    return False


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
        max_message_length=parse_config().max_message_length,
        platform_hint=(
            "You are chatting via Rocket.Chat. DMs are direct conversations; "
            "channel replies should be concise and thread-aware. Markdown is supported."
        ),
        emoji="🚀",
    )
