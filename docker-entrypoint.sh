#!/bin/sh
# Run the app as an unprivileged user. The container starts as root only long
# enough to make the bind-mounted DATA_DIR writable by that user, then drops
# privileges via gosu for the actual (network-facing) app process — so an app
# RCE is not root-in-container. PUID/PGID default to Unraid's 99:100 (nobody:
# users), the ownership its appdata shares use, and can be overridden.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"

if ! getent group "$PGID" >/dev/null 2>&1; then
  groupadd -g "$PGID" appgrp
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin appusr
fi

mkdir -p "$DATA_DIR"
# Best-effort: on some hosts the mount owner can't be changed; the app still
# runs, and a genuinely unwritable volume surfaces via the /health DB check.
chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true

exec gosu "$PUID:$PGID" "$@"
