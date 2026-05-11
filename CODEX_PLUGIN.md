# WhatsApp MCP Codex Plugin

This directory packages `https://github.com/timurco/whatsapp-mcp` as a Codex plugin.

## Contents

- `.codex-plugin/plugin.json` - Codex plugin manifest.
- `.mcp.json` - MCP server declaration for Codex.
- `skills/whatsapp/SKILL.md` - Codex usage guidance for WhatsApp operations.
- `scripts/run-mcp.sh` - Starts the Python MCP server with `uv`.
- `scripts/start-bridge.sh` - Starts the Go WhatsApp bridge.
- `whatsapp-bridge/` and `whatsapp-mcp-server/` - Upstream project sources.

## Prerequisites

- Go
- `uv`
- Python 3.11+
- FFmpeg, optional for WhatsApp voice message conversion

## First Run

From the plugin root:

```bash
./scripts/start-bridge.sh
```

Scan the QR code shown by the bridge in WhatsApp linked devices. Keep the bridge running while using the MCP tools.

Codex starts the MCP server through `.mcp.json`:

```bash
/bin/bash ./scripts/run-mcp.sh
```

The bridge stores local WhatsApp data in `whatsapp-bridge/store/`.
