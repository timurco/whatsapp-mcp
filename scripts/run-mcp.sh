#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$PLUGIN_DIR/whatsapp-mcp-server"

if ! command -v uv >/dev/null 2>&1; then
  echo "whatsapp-mcp requires uv. Install it from https://docs.astral.sh/uv/." >&2
  exit 127
fi

cd "$SERVER_DIR"
exec uv run main.py
