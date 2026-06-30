# Hermes Rocket.Chat Plugin

A self-contained Hermes Agent platform plugin that connects Rocket.Chat rooms
to the Hermes messaging gateway.

**Status:** Implementation in progress.

## What it does

- Listens to Rocket.Chat DMs and channel mentions via REST polling or
  WebSocket/DDP.
- Routes incoming messages through Hermes AI agents.
- Posts agent replies back to Rocket.Chat with thread awareness.
- Downloads, classifies, and forwards Rocket.Chat attachments.
- Uploads outbound media files from Hermes into Rocket.Chat rooms.

## Quick start (coming soon)

```bash
# Install the plugin
cp -r hermes-rocketchat-bot ~/.hermes/plugins/rocketchat

# Configure environment
export ROCKETCHAT_SERVER_URL=https://chat.example.com
export ROCKETCHAT_AUTH_MODE=token
export ROCKETCHAT_USER_ID=your-bot-user-id
export ROCKETCHAT_ACCESS_TOKEN=your-bot-access-token
export ROCKETCHAT_TRANSPORT=websocket

# Run Hermes
hermes gateway
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -v
```
