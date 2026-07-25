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

# Refuse to "drop" privileges to root — the whole point of this script is that
# the network-facing process is unprivileged.
if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
  echo "Refusing to run the app as root: set PUID/PGID to a non-zero user." >&2
  exit 1
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
  groupadd -g "$PGID" appgrp
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin appusr
fi

mkdir -p "$DATA_DIR"
# Best-effort: on some hosts the mount owner can't be changed; the app still
# runs, and a genuinely unwritable volume surfaces via the /health DB check.
#
# -h (do not dereference) matters: DATA_DIR is writable by the app user, so
# without it a symlink planted under /data would have its TARGET chowned by
# root on the next restart — turning any app-level file write into a path to
# owning arbitrary host-visible files.
chown -Rh "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true

exec gosu "$PUID:$PGID" "$@"
