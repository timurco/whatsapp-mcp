#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BRIDGE_DIR="$PLUGIN_DIR/whatsapp-bridge"

if ! command -v go >/dev/null 2>&1; then
  echo "whatsapp-mcp bridge requires Go. Install Go and retry." >&2
  exit 127
fi

cd "$BRIDGE_DIR"
exec go run main.go
