# Hermes Rocket.Chat Platform Plugin Design

## Goal

Build a Hermes Agent platform plugin that connects Rocket.Chat rooms to the Hermes messaging gateway, using the Hermes plugin path rather than modifying Hermes core.

The plugin should be installable as a directory containing `plugin.yaml` and `adapter.py`, and should reuse the proven behavior from `../openclaw-rocketchat-bot`: Rocket.Chat REST authentication, polling and WebSocket/DDP inbound transport, mention-gated group/channel handling, thread-anchored replies, and attachment transfer.

## References

- Reference implementation: `../openclaw-rocketchat-bot`
- Hermes docs: `website/docs/developer-guide/adding-platform-adapters.md` from `NousResearch/hermes-agent`
- Hermes gateway internals: `website/docs/developer-guide/gateway-internals.md`
- Hermes adapter base contract: `gateway/platforms/base.py`

## Constraints

- The current repository is an empty git repository. The plugin should be self-contained here.
- Use the Hermes plugin path:
  - `plugin.yaml`
  - `adapter.py`
  - `register(ctx)` calls `ctx.register_platform(...)`
- Do not require users to patch Hermes core.
- Implement in Python, because Hermes platform adapters extend `BasePlatformAdapter` from Hermes' Python gateway.
- Preserve Rocket.Chat room/thread semantics where they map cleanly to Hermes session sources.
- Keep the first production version focused on a single configured Rocket.Chat bot identity, while leaving config shape open for future multi-account support.

## User-approved scope

The requested scope is the full attachment-capable adapter, not only a polling MVP.

Included:

1. Rocket.Chat authentication
   - Token mode: `userId + accessToken`
   - Password mode: `username + password`
2. Inbound transports
   - REST polling via `subscriptions.get` and `chat.syncMessages`
   - WebSocket/DDP via `stream-room-messages`
3. Message routing
   - DMs always enter Hermes
   - Groups/channels require explicit bot mention or configured aliases
   - Ignore messages authored by the bot itself
   - Ignore Rocket.Chat system messages
   - De-duplicate message IDs
4. Threading
   - Channel/group replies default to thread replies anchored to the trigger message
   - Messages already inside a Rocket.Chat thread reply into that thread
   - DMs can send normal direct replies
5. Outbound
   - Send text messages
   - Upload local files for outbound media/document delivery when Hermes calls media helpers or standalone sends include media files
6. Attachments
   - Normalize Rocket.Chat `attachments`, `file`, and `files`
   - Classify images, documents, video, and audio by MIME type or extension
   - Download protected Rocket.Chat files to a Hermes-readable cache directory
   - Populate Hermes `MessageEvent.media_urls` and `MessageEvent.media_types`
7. Hermes integration
   - `BasePlatformAdapter.connect()` / `disconnect()` / `send()`
   - `MessageEvent` construction
   - `SendResult` return values
   - `get_chat_info()` best-effort metadata
   - `env_enablement_fn` for env-only setup
   - `allowed_users_env` / `allow_all_env`
   - `cron_deliver_env_var`
   - `standalone_sender_fn` for cron/out-of-process delivery
   - `max_message_length`
   - `platform_hint`

Deferred:

- Multiple simultaneous Rocket.Chat accounts in one plugin instance
- OCR/PDF rendering/video transcoding/audio transcription inside the adapter
- Rocket.Chat reactions, edits, and deletes as inbound Hermes events
- Native slash command registration in Rocket.Chat
- Public webhook mode

## Proposed repository structure

```text
hermes-rocketchat-bot/
  README.md
  plugin.yaml
  adapter.py
  pyproject.toml
  tests/
    test_config.py
    test_mentions.py
    test_client.py
    test_inbound.py
    test_attachments.py
    test_adapter.py
```

## Runtime architecture

```text
Rocket.Chat
  │
  ├─ REST polling transport ─┐
  └─ DDP WebSocket transport ├─ RocketChatAdapter ── BasePlatformAdapter.handle_message()
                             │                         │
                             │                         └─ Hermes GatewayRunner / AIAgent
                             │
                             └─ RocketChatClient ── REST send / update / upload / download
```

### Main components

#### `RocketChatConfig`

Parses platform configuration from environment variables and Hermes `PlatformConfig.extra`.

Key fields:

- `server_url`
- `auth_mode`
- `user_id`
- `access_token`
- `username`
- `password`
- `transport`: `polling` or `websocket`
- `poll_interval_seconds`
- `mention_names`
- `force_thread`
- `home_channel`
- `media_cache_dir`

#### `RocketChatClient`

Small async REST client built on `aiohttp` or `httpx`.

Responsibilities:

- Authenticate or verify token
- `subscriptions.get`
- `chat.syncMessages`
- `chat.postMessage`
- `chat.update` if later needed for draft streaming
- `rooms.media` + `rooms.mediaConfirm`
- Download protected file URLs with auth headers
- Fetch room/thread metadata where needed

#### Inbound transports

`PollingTransport`:

- Keeps an in-memory checkpoint for the current process initially
- Polls subscriptions updated since last checkpoint
- Syncs changed rooms
- Emits normalized inbound messages
- Handles rate limits by backing off

`WebSocketTransport`:

- Connects to `/websocket`
- Sends DDP `connect`, `login`, `sub` frames
- Subscribes to subscription/room changes and room message streams
- Refreshes room subscription list from REST
- Emits normalized inbound messages

#### `RocketChatAdapter`

Extends Hermes `BasePlatformAdapter`.

Responsibilities:

- Connect selected transport
- Convert normalized Rocket.Chat inbound events into Hermes `MessageEvent`
- Apply mention gating before dispatch
- Send outgoing text and media
- Return `SendResult`
- Report room metadata through `get_chat_info()`

## Data flow

### Inbound DM

1. Rocket.Chat transport receives message.
2. Adapter ignores bot-authored/system/duplicate messages.
3. Room type is direct, so no mention required.
4. Attachments are normalized and downloaded if necessary.
5. Adapter builds source:
   - `chat_id = room_id`
   - `chat_type = "dm"`
   - `user_id = sender_id`
   - `user_name = sender_name`
6. Adapter creates `MessageEvent`.
7. Adapter calls `await self.handle_message(event)`.
8. Hermes gateway runs the agent.
9. Hermes calls adapter `send()` with the reply.
10. Adapter posts back to the same room.

### Inbound group/channel mention

1. Rocket.Chat transport receives message.
2. Adapter ignores bot-authored/system/duplicate messages.
3. Room type is group/channel.
4. Adapter requires one of:
   - Rocket.Chat mention metadata contains bot username/name
   - Text contains `@<bot username>`
   - Text contains configured alias from `ROCKETCHAT_MENTION_NAMES`
5. Adapter strips or preserves mention text conservatively; the first implementation may preserve full text for traceability.
6. Adapter sets `reply_to_message_id` to the existing `tmid` or message ID.
7. Hermes response is sent into that thread.

### Inbound attachment

1. Message contains `attachments`, `file`, or `files`.
2. Adapter creates attachment candidates.
3. Public URLs may be passed through only if Hermes can fetch them safely; protected Rocket.Chat file URLs are downloaded with auth headers.
4. Downloaded files are written below a cache directory such as `~/.hermes/cache/rocketchat/inbound`.
5. Classifier maps each file to Hermes media type:
   - `image`
   - `document`
   - `video`
   - `audio` / `voice`
6. `MessageEvent.media_urls` receives local paths.
7. `MessageEvent.media_types` receives matching media type strings.

### Outbound media

1. Hermes calls adapter media helpers or standalone sender with `media_files`.
2. Adapter validates local paths via base media delivery validation where available.
3. Adapter uploads each file through Rocket.Chat `rooms.media`.
4. Adapter confirms upload through `rooms.mediaConfirm`.
5. Adapter returns `SendResult(success=True, message_id=...)` when Rocket.Chat returns a message ID.

## Configuration

### `plugin.yaml`

Expose environment variables to `hermes config`:

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

### Environment variables

```bash
ROCKETCHAT_SERVER_URL=https://chat.example.com
ROCKETCHAT_AUTH_MODE=token
ROCKETCHAT_USER_ID=rocket-user-id
ROCKETCHAT_ACCESS_TOKEN=rocket-access-token
ROCKETCHAT_TRANSPORT=websocket
ROCKETCHAT_MENTION_NAMES=hermes,assistant
ROCKETCHAT_ALLOWED_USERS=user-id-1,user-id-2
ROCKETCHAT_HOME_CHANNEL=room-id
```

Password mode:

```bash
ROCKETCHAT_SERVER_URL=https://chat.example.com
ROCKETCHAT_AUTH_MODE=password
ROCKETCHAT_USERNAME=hermesbot
ROCKETCHAT_PASSWORD=secret
ROCKETCHAT_TRANSPORT=polling
```

Optional:

```bash
ROCKETCHAT_POLL_INTERVAL_SECONDS=3
ROCKETCHAT_FORCE_THREAD=true
ROCKETCHAT_MEDIA_CACHE_DIR=/var/lib/hermes/rocketchat-media
ROCKETCHAT_MAX_MESSAGE_LENGTH=4000
```

## Error handling

- Missing config: plugin not auto-enabled; `validate_config` returns false.
- Authentication failure: `connect()` returns false and stores fatal error state if available.
- REST rate limits: back off using `Retry-After` when present.
- Attachment download failure: log warning and continue processing text plus remaining attachments.
- Attachment upload failure: return failed `SendResult` with human-readable error.
- WebSocket disconnect: mark adapter disconnected and allow Hermes gateway lifecycle to handle reconnect/restart behavior.
- Duplicate inbound message: skip before calling `handle_message`.

## Security

- Default to deny unless `ROCKETCHAT_ALLOWED_USERS`, `ROCKETCHAT_ALLOW_ALL_USERS`, or Hermes pairing/global authorization allows access.
- Store tokens in `~/.hermes/.env`, not in the repository.
- Treat Rocket.Chat auth tokens and passwords as secrets in docs and logs.
- Sanitize downloaded filenames.
- Store downloaded attachments under a controlled cache directory.
- Do not follow arbitrary attachment redirects without using the HTTP client safety checks available in Hermes base utilities where practical.
- Avoid logging full attachment URLs when they may contain credentials or signed tokens.

## Testing approach

Use pytest with mocked HTTP/WebSocket dependencies. No real Rocket.Chat server is required for unit tests.

Core tests:

- Env-only configuration enables the platform.
- Token auth uses `X-User-Id` and `X-Auth-Token`.
- Password auth calls `/api/v1/login` and stores returned token.
- DMs bypass mention gating.
- Channels require mention or alias.
- Bot-authored messages are ignored.
- Protected attachment URLs are downloaded with auth headers.
- Attachment MIME/extension classification maps to Hermes media types.
- `send()` posts text to `chat.postMessage`.
- `send()` includes `tmid` for threaded replies.
- Standalone sender can send text and upload media files.

## Open questions for implementation

- Whether to use `aiohttp` or `httpx`. Hermes core uses both patterns in different places; implementation should choose the dependency most likely available in Hermes runtime.
- Whether first implementation should persist checkpoints to disk or use in-memory checkpoints. The OpenClaw reference uses a file checkpoint store; Hermes plugin can start with in-memory for process lifetime, but disk checkpoints reduce duplicate processing across restarts.
- Whether to implement draft streaming via `send_draft()` and `chat.update` in the first version. This is useful but not required for functional parity at the platform level.
