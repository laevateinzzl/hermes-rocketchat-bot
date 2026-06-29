# Hermes Rocket.Chat Plugin Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a self-contained Hermes Agent Rocket.Chat platform plugin with REST/WebSocket inbound, mention-gated routing, threaded replies, and attachment transfer.

**Architecture:** Implement a Python Hermes plugin with `plugin.yaml` and `adapter.py`. `RocketChatAdapter` extends Hermes `BasePlatformAdapter`, uses a small async Rocket.Chat REST client, and selects either polling or DDP WebSocket inbound transport to emit Hermes `MessageEvent` objects.

**Tech Stack:** Python 3.10+, Hermes Agent gateway plugin API, pytest, pytest-asyncio, aiohttp or httpx, standard-library dataclasses/enums/pathlib.

---

## Prerequisites

Before executing this plan:

1. Read `docs/plans/2026-06-29-hermes-rocketchat-plugin-design.md`.
2. Read Hermes docs:
   - `website/docs/developer-guide/adding-platform-adapters.md`
   - `website/docs/developer-guide/gateway-internals.md`
3. Inspect Hermes `gateway/platforms/base.py` for current `BasePlatformAdapter`, `MessageEvent`, `MessageType`, and `SendResult` signatures.
4. Inspect the reference project:
   - `../openclaw-rocketchat-bot/src/client.ts`
   - `../openclaw-rocketchat-bot/src/inbound/polling.ts`
   - `../openclaw-rocketchat-bot/src/inbound/websocket.ts`
   - `../openclaw-rocketchat-bot/src/inbound/attachments.ts`
   - `../openclaw-rocketchat-bot/src/channel.ts`

Use TDD for each behavior. Do not write production adapter code before a failing test exists.

## Task 1: Create project scaffold and plugin metadata

**Files:**

- Create: `README.md`
- Create: `plugin.yaml`
- Create: `pyproject.toml`
- Create: `tests/conftest.py`

**Step 1: Write the failing metadata tests**

Create `tests/test_plugin_metadata.py`:

```python
from pathlib import Path
import yaml


def test_plugin_yaml_declares_hermes_platform_plugin():
    data = yaml.safe_load(Path("plugin.yaml").read_text())

    assert data["kind"] == "platform"
    assert data["label"] == "Rocket.Chat"
    assert data["name"] == "rocketchat-platform"


def test_plugin_yaml_surfaces_required_env_vars():
    data = yaml.safe_load(Path("plugin.yaml").read_text())
    required = {item["name"] if isinstance(item, dict) else item for item in data["requires_env"]}

    assert "ROCKETCHAT_SERVER_URL" in required
    assert "ROCKETCHAT_AUTH_MODE" in required
```

**Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_plugin_metadata.py -v
```

Expected: FAIL because `plugin.yaml` does not exist.

**Step 3: Write minimal metadata**

Create `plugin.yaml` with:

```yaml
name: rocketchat-platform
label: Rocket.Chat
kind: platform
version: 0.1.0
description: Rocket.Chat gateway adapter for Hermes Agent
author: hermes-rocketchat-bot
requires_env:
  - name: ROCKETCHAT_SERVER_URL
    description: Rocket.Chat server URL, e.g. https://chat.example.com
    prompt: Rocket.Chat server URL
    password: false
  - name: ROCKETCHAT_AUTH_MODE
    description: Authentication mode: token or password
    prompt: Auth mode
    password: false
optional_env:
  - name: ROCKETCHAT_USER_ID
    description: Rocket.Chat user ID for token authentication
    prompt: User ID
    password: false
  - name: ROCKETCHAT_ACCESS_TOKEN
    description: Rocket.Chat access token for token authentication
    prompt: Access token
    password: true
  - name: ROCKETCHAT_USERNAME
    description: Rocket.Chat username for password authentication
    prompt: Username
    password: false
  - name: ROCKETCHAT_PASSWORD
    description: Rocket.Chat password for password authentication
    prompt: Password
    password: true
  - name: ROCKETCHAT_TRANSPORT
    description: polling or websocket
    prompt: Transport
    password: false
  - name: ROCKETCHAT_MENTION_NAMES
    description: Comma-separated aliases that trigger the bot in rooms
    prompt: Mention aliases
    password: false
  - name: ROCKETCHAT_ALLOWED_USERS
    description: Comma-separated Rocket.Chat user IDs allowed to talk to Hermes
    prompt: Allowed users
    password: false
  - name: ROCKETCHAT_ALLOW_ALL_USERS
    description: Allow all Rocket.Chat users
    prompt: Allow all users
    password: false
  - name: ROCKETCHAT_HOME_CHANNEL
    description: Default room ID for cron and proactive delivery
    prompt: Home room ID
    password: false
```

Create `pyproject.toml` with pytest dependencies and basic formatting settings.

Create a short `README.md` explaining that implementation is in progress.

**Step 4: Run test to verify it passes**

Run:

```bash
python -m pytest tests/test_plugin_metadata.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add README.md plugin.yaml pyproject.toml tests/test_plugin_metadata.py tests/conftest.py
git commit -m "chore: scaffold Rocket.Chat Hermes plugin"
```

## Task 2: Implement configuration parsing

**Files:**

- Create: `adapter.py`
- Create: `tests/test_config.py`

**Step 1: Write failing config tests**

```python
from adapter import RocketChatConfig, parse_config, env_enablement


def test_token_config_from_env(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_TRANSPORT", "websocket")
    monkeypatch.setenv("ROCKETCHAT_MENTION_NAMES", "hermes,assistant")

    cfg = parse_config({})

    assert cfg.server_url == "https://chat.example.com"
    assert cfg.auth_mode == "token"
    assert cfg.user_id == "u1"
    assert cfg.access_token == "tok"
    assert cfg.transport == "websocket"
    assert cfg.mention_names == ["hermes", "assistant"]


def test_env_enablement_returns_seed_when_minimally_configured(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_HOME_CHANNEL", "room1")

    seed = env_enablement()

    assert seed["server_url"] == "https://chat.example.com"
    assert seed["home_channel"]["chat_id"] == "room1"
```

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_config.py -v
```

Expected: FAIL because `adapter.py` does not export these symbols.

**Step 3: Implement minimal config**

In `adapter.py`:

- Define `RocketChatConfig` dataclass.
- Implement `parse_bool`, `parse_csv`, `parse_config(extra)`.
- Use env values first, then `extra` fallback.
- Validate:
  - `server_url` required
  - token mode requires `user_id` and `access_token`
  - password mode requires `username` and `password`
  - transport defaults to `polling`
- Implement `env_enablement()` returning `None` unless minimal auth config exists.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_config.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_config.py
git commit -m "feat: parse Rocket.Chat plugin configuration"
```

## Task 3: Implement mention gating and message filtering

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_mentions.py`

**Step 1: Write failing tests**

```python
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
```

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_mentions.py -v
```

Expected: FAIL because `should_handle_message` is missing.

**Step 3: Implement minimal gating**

Implement `should_handle_message(...)`:

- Return `True` for `direct`.
- For `group` and `channel`, return true if:
  - any mention equals bot username or configured alias
  - text contains `@<bot_username>` or `@<alias>`
- Normalize case and trim whitespace.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_mentions.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_mentions.py
git commit -m "feat: gate Rocket.Chat channel messages by mention"
```

## Task 4: Implement Rocket.Chat REST client authentication and send

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_client.py`

**Step 1: Write failing tests**

Use a fake async HTTP client object that records calls.

Tests:

- Token auth calls `/api/v1/me` with `X-User-Id` and `X-Auth-Token`.
- Password auth calls `/api/v1/login` and stores returned `authToken`.
- `post_message(room_id, text, tmid=...)` calls `/api/v1/chat.postMessage` with `roomId`, `text`, and `tmid`.

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_client.py -v
```

Expected: FAIL because `RocketChatClient` is missing.

**Step 3: Implement minimal REST client**

In `adapter.py`:

- Define `RocketChatClientError`.
- Define `RocketChatIdentity` dataclass.
- Define `RocketChatClient` with:
  - `initialize()`
  - `_login_password()`
  - `_verify_token()`
  - `post_message()`
  - auth header construction
  - JSON error handling

Prefer dependency injection for HTTP calls so tests do not hit the network.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_client.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_client.py
git commit -m "feat: add Rocket.Chat REST client"
```

## Task 5: Implement attachment normalization and download

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_attachments.py`

**Step 1: Write failing tests**

Cover:

- `image/png` classifies as `image`.
- `.pdf` without MIME classifies as `document`.
- `video/mp4` classifies as `video`.
- `audio/ogg` classifies as `audio`.
- `file`, `files`, and `attachments` records are normalized into candidates.
- Protected file URLs are downloaded through `RocketChatClient.download_attachment()` and produce local paths.

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_attachments.py -v
```

Expected: FAIL because attachment helpers are missing.

**Step 3: Implement minimal attachment helpers**

In `adapter.py`:

- Define `AttachmentCandidate` dataclass.
- Implement `attachment_candidates_from_message(message)`.
- Implement `classify_attachment(candidate)`.
- Implement safe filename sanitization.
- Implement `download_attachment_to_cache(url, filename)` in `RocketChatClient`.
- Implement `resolve_message_media(message, client)` returning `(media_urls, media_types)`.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_attachments.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_attachments.py
git commit -m "feat: normalize Rocket.Chat attachments"
```

## Task 6: Implement polling transport

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_polling.py`

**Step 1: Write failing tests**

Test that `PollingTransport.poll_once()`:

- Initializes checkpoint on first run without replaying old messages.
- Calls `list_subscriptions(updated_since)`.
- Calls `sync_messages(room_id, updated_since)` for changed rooms.
- Skips bot messages and system messages.
- Emits normalized inbound events for user messages.
- Marks emitted message IDs seen.

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_polling.py -v
```

Expected: FAIL because polling transport is missing.

**Step 3: Implement minimal polling transport**

In `adapter.py`:

- Define `InMemoryCheckpointStore`.
- Define `PollingTransport` with `start()`, `stop()`, `poll_once()`.
- Use `asyncio.create_task` loop for repeated polling.
- Keep first version simple: no disk persistence unless explicitly needed during implementation.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_polling.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_polling.py
git commit -m "feat: add Rocket.Chat polling inbound transport"
```

## Task 7: Implement DDP WebSocket transport

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_websocket.py`

**Step 1: Write failing tests**

Use a fake WebSocket object.

Test that transport:

- Sends DDP `connect` on open.
- Sends login frame after `connected`.
- Subscribes to room streams after login.
- Converts `stream-room-messages` changed frames into inbound events.
- De-duplicates in-flight messages.

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_websocket.py -v
```

Expected: FAIL because WebSocket transport is missing.

**Step 3: Implement minimal WebSocket transport**

In `adapter.py`:

- Define `WebSocketTransport`.
- Convert `https://host` to `wss://host/websocket` and `http://host` to `ws://host/websocket`.
- Handle DDP frames:
  - `ping` → `pong`
  - `connected` → login
  - login result → bootstrap subscriptions
  - `changed` room message → inbound event
- Use REST `list_subscriptions(None)` to discover rooms.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_websocket.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_websocket.py
git commit -m "feat: add Rocket.Chat WebSocket inbound transport"
```

## Task 8: Implement Hermes adapter class

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_adapter.py`

**Step 1: Write failing tests**

Test that `RocketChatAdapter`:

- Extends or behaves like `BasePlatformAdapter`.
- `connect()` initializes the client and starts selected transport.
- `disconnect()` stops transport.
- Inbound events become Hermes `MessageEvent` objects.
- DMs call `handle_message` without mention.
- Channel messages without mention do not call `handle_message`.
- `send(chat_id, content, reply_to=...)` calls `client.post_message` with `tmid`.
- `get_chat_info(chat_id)` returns a dict with name/type best effort.

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_adapter.py -v
```

Expected: FAIL because `RocketChatAdapter` is incomplete.

**Step 3: Implement adapter**

In `adapter.py`:

- Import Hermes types inside try/except for testability:
  - `BasePlatformAdapter`
  - `MessageEvent`
  - `MessageType`
  - `SendResult`
  - `Platform`
- In test fallback, provide small compatible stubs only when Hermes is unavailable.
- Implement `RocketChatAdapter.__init__(config)` reading `config.extra`.
- Implement `connect()`:
  - parse config
  - create client
  - initialize identity
  - choose transport
  - start transport
  - mark connected
- Implement inbound callback:
  - apply mention gating
  - resolve attachments
  - build source using `self.build_source(...)`
  - create `MessageEvent`
  - call `await self.handle_message(event)`
- Implement `send()` returning `SendResult`.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_adapter.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_adapter.py
git commit -m "feat: connect Rocket.Chat adapter to Hermes gateway"
```

## Task 9: Implement outbound file upload and standalone sender

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_standalone_sender.py`

**Step 1: Write failing tests**

Test that:

- `RocketChatClient.upload_attachment(room_id, file_path, text, tmid)` calls `rooms.media` and `rooms.mediaConfirm`.
- `standalone_send(pconfig, chat_id, message, media_files=[...])` uploads files.
- `standalone_send(pconfig, chat_id, message)` sends text.
- Errors return `{"error": "..."}` instead of raising.

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_standalone_sender.py -v
```

Expected: FAIL because upload and standalone sender are missing.

**Step 3: Implement upload and standalone sender**

In `adapter.py`:

- Add `RocketChatClient.upload_attachment()`.
- Add async `standalone_send(...)` matching Hermes plugin hook docs.
- In adapter `send()`, if metadata or content includes media paths and Hermes exposes them, upload them after/beside text.
- Keep media upload order deterministic.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_standalone_sender.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_standalone_sender.py
git commit -m "feat: support Rocket.Chat file delivery"
```

## Task 10: Register the Hermes plugin

**Files:**

- Modify: `adapter.py`
- Create: `tests/test_register.py`

**Step 1: Write failing tests**

Create fake registry context:

```python
from adapter import register


class FakeContext:
    def __init__(self):
        self.platforms = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


def test_register_declares_platform_hooks():
    ctx = FakeContext()

    register(ctx)

    platform = ctx.platforms[0]
    assert platform["name"] == "rocketchat"
    assert platform["label"] == "Rocket.Chat"
    assert platform["allowed_users_env"] == "ROCKETCHAT_ALLOWED_USERS"
    assert platform["allow_all_env"] == "ROCKETCHAT_ALLOW_ALL_USERS"
    assert platform["cron_deliver_env_var"] == "ROCKETCHAT_HOME_CHANNEL"
    assert callable(platform["adapter_factory"])
    assert callable(platform["env_enablement_fn"])
    assert callable(platform["standalone_sender_fn"])
```

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_register.py -v
```

Expected: FAIL because `register` does not register all hooks.

**Step 3: Implement `register(ctx)`**

In `adapter.py`:

```python
def register(ctx):
    ctx.register_platform(
        name="rocketchat",
        label="Rocket.Chat",
        adapter_factory=lambda cfg: RocketChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["ROCKETCHAT_SERVER_URL", "ROCKETCHAT_AUTH_MODE"],
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="ROCKETCHAT_HOME_CHANNEL",
        allowed_users_env="ROCKETCHAT_ALLOWED_USERS",
        allow_all_env="ROCKETCHAT_ALLOW_ALL_USERS",
        standalone_sender_fn=standalone_send,
        max_message_length=4000,
        platform_hint=(
            "You are chatting via Rocket.Chat. DMs are direct conversations; "
            "channel replies should be concise and thread-aware. Markdown is supported."
        ),
        emoji="🚀",
    )
```

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_register.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add adapter.py tests/test_register.py
git commit -m "feat: register Rocket.Chat as Hermes platform plugin"
```

## Task 11: Write user documentation

**Files:**

- Modify: `README.md`

**Step 1: Write failing README checks**

Create or extend `tests/test_readme.py`:

```python
from pathlib import Path


def test_readme_documents_install_and_config():
    text = Path("README.md").read_text()

    assert "~/.hermes/plugins/rocketchat" in text
    assert "ROCKETCHAT_SERVER_URL" in text
    assert "ROCKETCHAT_AUTH_MODE" in text
    assert "ROCKETCHAT_ALLOWED_USERS" in text
    assert "hermes gateway" in text
```

**Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_readme.py -v
```

Expected: FAIL until README contains complete docs.

**Step 3: Expand README**

Document:

- What the plugin does
- Installation:
  - copy/symlink repo to `~/.hermes/plugins/rocketchat`
- Rocket.Chat bot account prerequisites
- Token mode config
- Password mode config
- WebSocket vs polling
- Mention behavior
- Thread behavior
- Attachments
- Security/allowlist
- Troubleshooting
- Development commands

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_readme.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add README.md tests/test_readme.py
git commit -m "docs: document Rocket.Chat Hermes plugin setup"
```

## Task 12: Final verification

**Files:**

- All project files

**Step 1: Run full test suite**

```bash
python -m pytest -v
```

Expected: all tests pass.

**Step 2: Run syntax/type checks**

At minimum:

```bash
python -m compileall adapter.py tests
```

Expected: command exits 0.

If tooling is added:

```bash
python -m ruff check .
python -m pyright
```

Expected: no errors.

**Step 3: Run local smoke check**

Use a temporary fake Hermes plugin context or a small script to import `adapter.py` and call `register(ctx)`.

Expected:

- Import succeeds.
- Exactly one platform is registered.
- Platform name is `rocketchat`.

**Step 4: Review git diff**

```bash
git status --short
git diff --stat
```

Expected: only intentional project files are changed.

**Step 5: Commit any final cleanup**

```bash
git add .
git commit -m "test: verify Rocket.Chat Hermes plugin"
```

## Completion criteria

The implementation is complete only when:

- `plugin.yaml` exists and declares a Hermes platform plugin.
- `adapter.py` exports `register(ctx)`.
- `register(ctx)` calls `ctx.register_platform(...)` with adapter, env enablement, allowlist, cron, and standalone sender hooks.
- Token and password authentication are tested.
- Polling and WebSocket inbound transports are tested.
- Mention gating is tested.
- Attachment classification/download is tested.
- Text send and file upload are tested.
- README explains installation and configuration.
- Fresh verification commands pass.
