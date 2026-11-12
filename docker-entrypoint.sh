#!/bin/sh
# Run as PUID:PGID (Unraid default nobody:users = 99:100) so files written to
# the /data appdata folder are owned like every other Unraid container's.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA="${PLEXLANG_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  chown -R "$PUID:$PGID" "$DATA" 2>/dev/null || echo "Warning: could not set owner of $DATA" >&2
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
