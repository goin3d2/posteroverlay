#!/bin/sh
# Run as PUID:PGID (Unraid default nobody:users = 99:100) so files in /config belong to the user.
set -e
mkdir -p "$CONFIG_DIR"
if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" "$CONFIG_DIR" 2>/dev/null || true
  exec gosu "$PUID:$PGID" uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
