"""Tests for Rocket.Chat DDP WebSocket inbound transport."""

import asyncio
import json

import pytest  # type: ignore[reportMissingImports]

from adapter import WebSocketTransport, _ws_url


# ---------------------------------------------------------------------------
# Fake WebSocket for testing
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Simulates a DDP WebSocket connection with canned server frames."""

    def __init__(self, server_frames=None):
        self.sent_frames: list[str] = []
        self._server_frames: list[str] = list(server_frames or [])
        self._closed = False
        self._recv_index = 0

    async def send(self, data: str):
        self.sent_frames.append(data)

    async def recv(self):
        if self._recv_index < len(self._server_frames):
            frame = self._server_frames[self._recv_index]
            self._recv_index += 1
            return frame
        # No more frames — signal closed connection immediately
        raise ConnectionError("connection closed")

    async def close(self):
        self._closed = True

    @property
    def closed(self):
        return self._closed


class FakeAiohttpMessage:
    """Minimal aiohttp WSMessage shape for receive()."""

    def __init__(self, data: str):
        self.data = data


class FakeAiohttpWebSocket:
    """Simulates aiohttp.ClientWebSocketResponse's send/receive API."""

    def __init__(self, server_frames=None):
        self.sent_frames: list[str] = []
        self._server_frames: list[str] = list(server_frames or [])
        self._closed = False
        self._recv_index = 0

    async def send_str(self, data: str):
        self.sent_frames.append(data)

    async def receive(self):
        if self._recv_index < len(self._server_frames):
            frame = self._server_frames[self._recv_index]
            self._recv_index += 1
            return FakeAiohttpMessage(frame)
        raise ConnectionError("connection closed")

    async def close(self):
        self._closed = True


# ---------------------------------------------------------------------------
# Fake client for WebSocket tests
# ---------------------------------------------------------------------------


class FakeWSClient:
    """Fake REST client used by WebSocket transport to list subscriptions."""

    def __init__(self, subscriptions=None):
        self._subscriptions = subscriptions or []
        self.subscriptions_calls: list[dict] = []
        self._user_id = "bot1"
        self._access_token = "tok"
        self.server_url = "https://chat.example.com"

    async def list_subscriptions(self):
        self.subscriptions_calls.append({})
        return self._subscriptions


# ---------------------------------------------------------------------------
# DDP frame helpers
# ---------------------------------------------------------------------------


def _decode_frame(frame: str) -> dict:
    return json.JSONDecoder().decode(frame)


def _connected_frame(session="test-session-1"):
    return json.dumps({"msg": "connected", "session": session})


def _ping_frame():
    return json.dumps({"msg": "ping"})


def _login_result(user_id="bot-user-id"):
    return json.dumps({
        "msg": "result",
        "id": "1",
        "result": {"id": user_id, "token": "tok", "tokenExpires": {"$date": 9999999999999}},
    })


def _sub_ready(sub_id="sub-room-1"):
    return json.dumps({"msg": "ready", "subs": [sub_id]})


def _room_message(room_id="room-1", msg_id="msg-1", text="hello", sender_id="alice", sender_name="alice"):
    return json.dumps({
        "msg": "changed",
        "collection": "stream-room-messages",
        "id": room_id,
        "fields": {
            "eventName": room_id,
            "args": [
                {
                    "_id": msg_id,
                    "rid": room_id,
                    "msg": text,
                    "u": {"_id": sender_id, "username": sender_name},
                    "ts": "2024-01-01T00:00:00.000Z",
                }
            ],
        },
    })


# ---------------------------------------------------------------------------
# WebSocket URL conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "server_url, expected_ws",
    [
        ("https://chat.example.com", "wss://chat.example.com/websocket"),
        ("http://chat.example.com", "ws://chat.example.com/websocket"),
        ("https://chat.example.com/", "wss://chat.example.com/websocket"),
    ],
)
def test_ws_url_conversion(server_url, expected_ws):
    assert _ws_url(server_url) == expected_ws


# ---------------------------------------------------------------------------
# Helper: run transport for a short time, then stop
# ---------------------------------------------------------------------------


async def _run_and_stop(transport, sleep_s=0.05):
    """Start the transport, let it run briefly, then stop and collect results."""
    await transport.start()
    await asyncio.sleep(sleep_s)
    await transport.stop()


def _factory(ws):
    """Return an async callable factory that yields *ws* when awaited."""

    async def _inner():
        return ws

    return _inner


# ---------------------------------------------------------------------------
# Handshake and login tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_sends_connect_on_open():
    """On connection, the transport should send DDP connect frame."""
    ws = FakeWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
        ]
    )
    client = FakeWSClient()

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )

    await _run_and_stop(transport)

    # Should have sent a connect frame
    assert len(ws.sent_frames) >= 1
    connect_msg = _decode_frame(ws.sent_frames[0])
    assert connect_msg["msg"] == "connect"
    assert connect_msg["version"] == "1"


@pytest.mark.asyncio
async def test_websocket_supports_aiohttp_websocket_api():
    """Default aiohttp websockets use send_str()/receive(), not send()/recv()."""
    ws = FakeAiohttpWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
        ]
    )
    client = FakeWSClient()

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )

    await _run_and_stop(transport)

    assert len(ws.sent_frames) >= 2
    assert _decode_frame(ws.sent_frames[0])["msg"] == "connect"
    assert _decode_frame(ws.sent_frames[1])["method"] == "login"


@pytest.mark.asyncio
async def test_websocket_sends_login_after_connected():
    """After receiving 'connected', the transport should send a login method."""
    ws = FakeWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
        ]
    )
    client = FakeWSClient()

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )

    await _run_and_stop(transport)

    # Second frame should be login
    assert len(ws.sent_frames) >= 2
    login_msg = _decode_frame(ws.sent_frames[1])
    assert login_msg["msg"] == "method"
    assert login_msg["method"] == "login"


@pytest.mark.asyncio
async def test_websocket_pong_on_ping():
    """Transport should respond to ping with pong."""
    ws = FakeWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
            _ping_frame(),
            _ping_frame(),
        ]
    )
    client = FakeWSClient()

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )

    await _run_and_stop(transport, sleep_s=0.1)

    # Check that at least one frame is a pong
    pongs = [f for f in ws.sent_frames if _decode_frame(f)["msg"] == "pong"]
    assert len(pongs) >= 1


# ---------------------------------------------------------------------------
# Subscription tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_subscribes_to_rooms_after_login():
    """After successful login, transport should subscribe to room message streams."""
    ws = FakeWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
        ]
    )
    client = FakeWSClient(
        subscriptions=[
            {"rid": "room-a", "_id": "room-a", "t": "d", "name": "alice"},
            {"rid": "room-b", "_id": "room-b", "t": "c", "name": "general"},
        ]
    )

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )

    await _run_and_stop(transport)

    # Should have subscribed to room streams
    sub_frames = [_decode_frame(f) for f in ws.sent_frames if _decode_frame(f).get("msg") == "sub"]
    assert len(sub_frames) >= 1
    # Each sub should target stream-room-messages
    for sf in sub_frames:
        assert sf["name"] == "stream-room-messages"


# ---------------------------------------------------------------------------
# Message conversion tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_converts_room_message_to_inbound_event():
    """A 'changed' frame with a room message should be converted to an inbound event."""
    received_events = []

    async def on_message(event):
        received_events.append(event)

    ws = FakeWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
            _room_message(
                room_id="room-1",
                msg_id="msg-hello",
                text="hello bot",
                sender_id="alice",
                sender_name="alice",
            ),
        ]
    )
    client = FakeWSClient(
        subscriptions=[
            {"rid": "room-1", "_id": "room-1", "t": "d", "name": "alice"},
        ]
    )

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )
    transport.set_on_message(on_message)

    await _run_and_stop(transport, sleep_s=0.1)

    assert len(received_events) >= 1
    event = received_events[0]
    assert event["_id"] == "msg-hello"
    assert event["msg"] == "hello bot"
    assert event["rid"] == "room-1"


@pytest.mark.asyncio
async def test_websocket_deduplicates_messages():
    """The transport should not emit the same message ID twice."""
    received_events = []

    async def on_message(event):
        received_events.append(event)

    ws = FakeWebSocket(
        server_frames=[
            _connected_frame(),
            _login_result(),
            _room_message(room_id="room-1", msg_id="dup-msg", text="hi"),
            _room_message(room_id="room-1", msg_id="dup-msg", text="hi again"),
        ]
    )
    client = FakeWSClient(
        subscriptions=[
            {"rid": "room-1", "_id": "room-1", "t": "d", "name": "alice"},
        ]
    )

    transport = WebSocketTransport(
        client=client,
        ws_url="wss://chat.example.com/websocket",
        ws_factory=_factory(ws),
    )
    transport.set_on_message(on_message)

    await _run_and_stop(transport, sleep_s=0.1)

    assert len(received_events) == 1
