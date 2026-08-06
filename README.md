# Hermes Rocket.Chat Plugin

A self-contained Hermes Agent platform plugin that connects Rocket.Chat rooms
to the Hermes messaging gateway.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## What it does

- **Inbound:** Listens to Rocket.Chat DMs and channel mentions via REST polling
  or WebSocket/DDP.
- **Routing:** Routes incoming messages through Hermes AI agents with
  mention-gated channel access.
- **Outbound:** Posts agent replies back to Rocket.Chat with thread awareness.
- **Attachments:** Downloads, classifies, and forwards Rocket.Chat attachments
  to Hermes; uploads outbound media files from Hermes into Rocket.Chat rooms.

## Installation

### Option A: Git install (recommended)

If the repository is hosted on GitHub/GitLab:

```bash
hermes plugins install <owner/repo>
# or with full URL
hermes plugins install https://github.com/<owner>/hermes-rocketchat-bot.git
```

### Option B: Manual copy

```bash
mkdir -p ~/.hermes/plugins
cp -r hermes-rocketchat-bot ~/.hermes/plugins/rocketchat
```

### Option C: Symlink (development)

```bash
mkdir -p ~/.hermes/plugins
ln -s "$(pwd)/hermes-rocketchat-bot" ~/.hermes/plugins/rocketchat
```

### Verify installation

```bash
hermes plugins list
```

The plugin name `rocketchat-platform` should appear in the list.

### Dependencies

The adapter uses **aiohttp** or **httpx** for HTTP and WebSocket communication.
At least one must be available in the Hermes runtime:

```bash
pip install aiohttp
# or
pip install httpx
```

For development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

The plugin reads settings from environment variables (typically set in
`~/.hermes/.env`).  Run `hermes config` to enter values interactively.

### Token authentication (recommended)

```bash
ROCKETCHAT_SERVER_URL=https://chat.example.com
ROCKETCHAT_AUTH_MODE=token
ROCKETCHAT_USER_ID=your-bot-user-id
ROCKETCHAT_ACCESS_TOKEN=your-bot-access-token
```

Generate a personal access token in Rocket.Chat:
**Account → My Account → Security → Personal Access Tokens**.

### Password authentication

```bash
ROCKETCHAT_SERVER_URL=https://chat.example.com
ROCKETCHAT_AUTH_MODE=password
ROCKETCHAT_USERNAME=hermesbot
ROCKETCHAT_PASSWORD=your-bot-password
```

### Transport

Choose how the bot receives messages:

```bash
# WebSocket / DDP (real-time, lower latency)
ROCKETCHAT_TRANSPORT=websocket

# REST polling (simpler, no persistent connection — the default)
ROCKETCHAT_TRANSPORT=polling
ROCKETCHAT_POLL_INTERVAL_SECONDS=3
```

### Reconnect & heartbeat (WebSocket)

The WebSocket transport detects dropped connections and reconnects
automatically, so the bot comes back online when the Rocket.Chat server
restarts or becomes reachable again after downtime (e.g. overnight).

- **Heartbeat:** if no frame arrives within `ROCKETCHAT_RECEIVE_TIMEOUT`
  seconds (default `60`), the transport sends a DDP ping. If no response
  arrives within `ROCKETCHAT_PING_TIMEOUT` seconds (default `10`), the
  connection is treated as dead and reconnected. This catches silently
dropped connections (server powered off without a TCP FIN) that would
  otherwise leave the bot hung forever.
- **Backoff:** reconnects use exponential backoff with jitter, starting at
  `ROCKETCHAT_RECONNECT_INITIAL_DELAY` (default `1`s) and capped at
  `ROCKETCHAT_RECONNECT_MAX_DELAY` (default `60`s). A server down for hours
  is retried at most once a minute, so the log is not flooded and the server
  is not hammered.
- **Re-authentication:** if the resume token is rejected (e.g. after a
  server restart invalidated it), the client re-runs login automatically.
- **Attempt cap:** `ROCKETCHAT_RECONNECT_MAX_ATTEMPTS` (default `0` =
  unlimited) gives up after N failed attempts.

```bash
# Tuning for long overnight downtime (defaults shown)
ROCKETCHAT_RECEIVE_TIMEOUT=60
ROCKETCHAT_PING_TIMEOUT=10
ROCKETCHAT_RECONNECT_INITIAL_DELAY=1
ROCKETCHAT_RECONNECT_MAX_DELAY=60
ROCKETCHAT_RECONNECT_MAX_ATTEMPTS=0   # 0 = retry forever
ROCKETCHAT_RECONNECT_JITTER=0.25      # ±25% randomization
```

### Inbound dedup (WebSocket)

When the WebSocket reconnects (or the gateway restarts), Rocket.Chat's
`stream-room-messages` subscription can replay recent unread messages. Without
dedup this causes the bot to answer the same user message multiple times with
**different** replies (one per replay). The adapter keeps a disk-backed
seen-message-id store so replays are suppressed.

- Enabled by default under the WebSocket transport (`ROCKETCHAT_DEDUP_ENABLED=true`).
- Disabled automatically for polling (it has its own checkpoint dedup).
- Seen ids persist to `$HERMES_HOME/rocketchat_seen_ids.json` and survive
  gateway restarts. Override the path with `ROCKETCHAT_DEDUP_STORE_PATH`.
- Entries expire after `ROCKETCHAT_DEDUP_TTL_HOURS` (default `168` = 7 days)
  so the file does not grow without bound.

This only suppresses exact message-id replays. A user manually re-sending the
same text gets a new message id and is answered normally.

### Security: user allowlist

Control who can talk to the bot.  At least one of the following must be set
before messages are dispatched to Hermes:

```bash
# Allow specific users (comma-separated Rocket.Chat user IDs)
ROCKETCHAT_ALLOWED_USERS=user-id-1,user-id-2

# Allow everyone (use with caution)
ROCKETCHAT_ALLOW_ALL_USERS=true
```

If neither is set the gateway's own pairing / authorization logic applies.

### Mention behaviour

In **channels and groups** the bot only responds when explicitly mentioned.
Configure additional trigger aliases:

```bash
ROCKETCHAT_MENTION_NAMES=hermes,assistant,bot
```

The bot responds to `@hermesbot` (its actual username) plus any alias listed
above.

**Per-room override:** rooms listed in `ROCKETCHAT_ALWAYS_RESPOND_ROOMS`
(comma-separated room IDs) receive every message without needing a mention
(mirrors Slack's `require_mention_channels`):

```bash
ROCKETCHAT_ALWAYS_RESPOND_ROOMS=room-id-a,room-id-b
```

**Mass mentions:** with `ROCKETCHAT_IGNORE_OTHER_USER_MENTIONS=true`, the bot
stays quiet when it is mentioned *alongside* other users (a message for
everyone) and still answers a direct `@hermesbot` mention.

**Direct messages** always reach the bot — no mention required.

### Thread behaviour

- Channel/group replies are posted as **thread replies** anchored to the
  triggering message.
- If the triggering message is already inside a Rocket.Chat thread the reply
  stays in that thread.
- Thread replies backfill the **parent message context** (text + author) into
  the Hermes event, so the agent knows what it is replying to; replies to the
  bot's own messages are flagged.
- DMs can receive normal (non-threaded) replies.

### Cron / proactive delivery

Set a home room for cron-triggered messages:

```bash
ROCKETCHAT_HOME_CHANNEL=GENERAL_ROOM_ID
```

A cron target may also be a **Rocket.Chat username or user ID**: the adapter
resolves it to (or reuses) the user's direct room via `dm.create`, so
proactive delivery can reach a person directly (mirrors Hermes Slack
user-to-DM resolution).

### Optional settings

```bash
ROCKETCHAT_FORCE_THREAD=true          # always reply in threads
ROCKETCHAT_MEDIA_CACHE_DIR=/var/lib/hermes/rocketchat-media
ROCKETCHAT_MAX_MESSAGE_LENGTH=4000    # max chars per message (longer replies are split)
```

## Live streaming, tool status & long replies

- **Streaming previews** — the adapter implements `edit_message`, so Hermes'
  stream consumer grows replies in real time (one message that fills up as
  the agent writes) instead of waiting for the whole answer. The 💭 thinking
  placeholder becomes the first editable preview, so users never see a
  placeholder *and* a duplicate reply.
- **Live tool status** — `supports_status_text` is enabled: while the agent
  runs tools, the thinking placeholder shows the current activity
  ("💭 Thinking… is running pytest…") and updates as the tool changes.
- **Long replies are split, not truncated** — `splits_long_messages` is
  enabled and `send()` chunks oversized content at paragraph/line boundaries
  into multiple messages in the same room/thread (each ≤
  `ROCKETCHAT_MAX_MESSAGE_LENGTH`). The delivery router therefore delivers
  full output without gateway-level truncation.
- **Code blocks** — `supports_code_blocks` is enabled; Rocket.Chat renders
  markdown fenced code blocks natively.

## Running

Once configured, start the Hermes gateway:

```bash
hermes gateway
```

The plugin auto-enables when `ROCKETCHAT_SERVER_URL` and authentication
credentials are present in the environment.

## Attachment handling

### Inbound

- Rocket.Chat `attachments`, `file`, and `files` fields are normalized into
  attachment candidates.
- Images, documents, video, and audio are classified by MIME type (with
  extension fallback).
- Protected Rocket.Chat file URLs are downloaded with auth headers to a
  configurable cache directory.
- `MessageEvent.media_urls` and `MessageEvent.media_types` are populated for
  Hermes agents.

### Outbound

Hermes' `MEDIA:` delivery contract is fully supported: every outbound media
send (`send_image_file`, `send_document`, `send_video`, `send_voice`,
`send_animation`, and batched `send_multiple_images`) uploads the file to
Rocket.Chat via the `rooms.media` → `rooms.mediaConfirm` → `chat.postMessage`
flow, so file attachments from the agent arrive as real downloads — no
"couldn't deliver" fallbacks.

- Local files, `file://` URIs (what the gateway passes for MEDIA delivery),
  and http(s) image URLs are all accepted.
- http(s) images are downloaded through Hermes' SSRF-guarded image cache
  before upload, so redirects to private/internal addresses are blocked.
- Thread metadata (`tmid`) is forwarded with every media message.

## Development

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
python -m pytest -v

# Lint
python -m ruff check .
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Plugin does not auto-enable | `ROCKETCHAT_SERVER_URL` or auth vars missing |
| Messages not received | Token expired, wrong transport, or WebSocket blocked |
| Bot offline after server downtime | WebSocket hung on a dead connection — ensure `ROCKETCHAT_RECEIVE_TIMEOUT` is set (default 60s) so the heartbeat detects the drop |
| Bot never reconnects | Check `ROCKETCHAT_RECONNECT_MAX_ATTEMPTS` isn't set too low; 0 means unlimited |
| Bot ignores channel messages | Mention alias not configured; use `ROCKETCHAT_MENTION_NAMES` |
| Attachments not forwarded | `ROCKETCHAT_MEDIA_CACHE_DIR` not set or not writable |
| "Not connected" errors | Check server URL is reachable and credentials are valid |

## Architecture

```
Rocket.Chat
  │
  ├─ REST polling transport ─┐
  └─ DDP WebSocket transport ├─ RocketChatAdapter ── BasePlatformAdapter.handle_message()
                              │                         │
                              │                         └─ Hermes GatewayRunner / AIAgent
                              │
                              └─ RocketChatClient ── REST send / upload / download
```
