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

**Direct messages** always reach the bot — no mention required.

### Thread behaviour

- Channel/group replies are posted as **thread replies** anchored to the
  triggering message.
- If the triggering message is already inside a Rocket.Chat thread the reply
  stays in that thread.
- DMs can receive normal (non-threaded) replies.

### Cron / proactive delivery

Set a home room for cron-triggered messages:

```bash
ROCKETCHAT_HOME_CHANNEL=GENERAL_ROOM_ID
```

### Optional settings

```bash
ROCKETCHAT_FORCE_THREAD=true          # always reply in threads
ROCKETCHAT_MEDIA_CACHE_DIR=/var/lib/hermes/rocketchat-media
ROCKETCHAT_MAX_MESSAGE_LENGTH=4000    # truncate long messages
```

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

When Hermes calls the adapter with `media_files`, each file is uploaded to
Rocket.Chat via the `rooms.media` → `rooms.mediaConfirm` flow.

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
