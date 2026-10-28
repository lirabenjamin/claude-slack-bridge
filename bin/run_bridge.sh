#!/usr/bin/env bash
# Wrapper for launchd: rotates bridge logs before start if they've grown large,
# then execs the bridge so launchd keeps tracking the same PID.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$ROOT/logs_archive"
MAX_BYTES=$((20*1024*1024))  # 20MB
mkdir -p "$ARCHIVE"
TS=$(date +%Y%m%d_%H%M%S)

for f in bridge.log bridge.stdout.log bridge.stderr.log; do
  path="$ROOT/$f"
  if [ -f "$path" ]; then
    size=$(stat -f%z "$path" 2>/dev/null || echo 0)
    if [ "$size" -gt "$MAX_BYTES" ]; then
      mv "$path" "$ARCHIVE/${f}.${TS}"
      gzip "$ARCHIVE/${f}.${TS}" &
    fi
  fi
done

exec "$ROOT/.venv/bin/python" "$ROOT/bridge.py"
