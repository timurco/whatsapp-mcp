---
name: whatsapp
description: Use the bundled WhatsApp MCP server to search contacts and chats, inspect messages and media, or send WhatsApp messages after the local bridge is running.
---

# WhatsApp MCP

Use this skill when the user asks to work with WhatsApp through this plugin.

## Runtime

- The MCP server runs from `whatsapp-mcp-server/main.py` using `uv run main.py`.
- The WhatsApp bridge must be running separately from `whatsapp-bridge/main.go`.
- Start the bridge with `./scripts/start-bridge.sh` from the plugin root.
- On first run, the bridge displays a QR code. The user must scan it in WhatsApp under linked devices.
- The bridge stores local data in `whatsapp-bridge/store/` and exposes its API on `http://localhost:8080/api`.

## Safety

- WhatsApp content is private user data. Only inspect or send messages when the user explicitly asks.
- Confirm recipient identity before sending to a phone number or group JID if there is ambiguity.
- For group chats, use the exact group JID returned by the tools.
- Treat media paths returned by `download_media` as local private files.

## Available MCP Tools

- `search_contacts`
- `list_messages`
- `list_chats`
- `get_chat`
- `get_direct_chat_by_contact`
- `get_contact_chats`
- `get_last_interaction`
- `get_message_context`
- `send_message`
- `send_file`
- `send_audio_message`
- `download_media`

## Troubleshooting

- If reads return no data, make sure the bridge has finished syncing messages.
- If sends fail, make sure the bridge is running and the recipient is a phone number with country code and no plus sign, or a valid WhatsApp JID.
- If audio message sending fails because FFmpeg is unavailable, use `send_file` instead or install FFmpeg.
