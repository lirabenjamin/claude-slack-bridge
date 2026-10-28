#!/usr/bin/env bash
# Wrapper for launchd: rotates cloudflared log before start if it's grown large,
# then execs cloudflared with --protocol http2 (QUIC is blocked on some networks).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$ROOT/logs_archive"
MAX_BYTES=$((20*1024*1024))  # 20MB
mkdir -p "$ARCHIVE"
TS=$(date +%Y%m%d_%H%M%S)

path="$ROOT/cloudflared.log"
if [ -f "$path" ]; then
  size=$(stat -f%z "$path" 2>/dev/null || echo 0)
  if [ "$size" -gt "$MAX_BYTES" ]; then
    mv "$path" "$ARCHIVE/cloudflared.log.${TS}"
    gzip "$ARCHIVE/cloudflared.log.${TS}" &
  fi
fi

CLOUDFLARED="${CLOUDFLARED:-$(command -v cloudflared)}"
exec "$CLOUDFLARED" tunnel --no-autoupdate --protocol http2 --url "http://localhost:${PIP_PORT:-8765}"
