#!/usr/bin/env bash
# Restore OpenPoolPro from a nightly backup generation.
#
#   scripts/restore.sh [options] [SNAPSHOT|YYYY-MM-DD]
#
# A generation is a PAIR of files in DATA_DIR/backups:
#   app-<date>.db          the SQLite snapshot (VACUUM INTO, self-contained)
#   assets-<date>.tar.gz   photos/, invoices/, contracts/, branding/
# Restoring only the DB brings back rows whose Photo.filename and
# Contract.pdf_filename point at files that are not on disk, so this script
# restores both halves together by default.
#
# WHY THIS IS NOT `cp app-2026-07-01.db data/app.db`:
# the live DB runs in WAL mode. Dropping a snapshot in place while the old
# app.db-wal / app.db-shm are still there lets SQLite replay frames from the
# PREVIOUS database over the restored one on the next open — a silent,
# unrecoverable corruption that looks fine until the first missing row. The
# sidecars must go, and the result must be verified before anything starts.
#
# Options:
#   -y, --yes            do not prompt (for scripted/disaster use)
#       --data-dir DIR   data directory (default: $DATA_DIR, else ./data)
#       --container NAME container that must not be running (default: openpoolpro)
#       --skip-assets    restore the DB only, leave photos/invoices/contracts as-is
#   -h, --help           this text
#
# With no SNAPSHOT the newest app-*.db in DATA_DIR/backups is used. A bare date
# (2026-07-01) is resolved inside that directory; any other value is a path.
#
# Nothing is ever deleted: the existing DB, its WAL/SHM sidecars, and any asset
# directories being replaced are moved aside to *.pre-restore-<timestamp>.
set -euo pipefail

DATA_DIR="${DATA_DIR:-./data}"
CONTAINER="${RESTORE_CONTAINER:-openpoolpro}"
ASSUME_YES=0
SKIP_ASSETS=0
SNAPSHOT=""

die() { printf 'restore: %s\n' "$1" >&2; exit 1; }

# The header comment block IS the help text — one place to keep current.
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)      ASSUME_YES=1; shift ;;
    --data-dir)    DATA_DIR="${2:?--data-dir needs a path}"; shift 2 ;;
    --container)   CONTAINER="${2?--container needs a name (pass '' to disable the check)}"; shift 2 ;;
    --skip-assets) SKIP_ASSETS=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    -*)            usage >&2; die "unknown option: $1" ;;
    *)
      [ -z "$SNAPSHOT" ] || die "only one snapshot may be given"
      SNAPSHOT="$1"; shift ;;
  esac
done

[ -d "$DATA_DIR" ] || die "data directory not found: $DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"
BACKUP_DIR="$DATA_DIR/backups"
DB="$DATA_DIR/app.db"
TS="$(date +%Y%m%d-%H%M%S)"

# --- resolve the snapshot ----------------------------------------------------
if [ -z "$SNAPSHOT" ]; then
  shopt -s nullglob
  candidates=("$BACKUP_DIR"/app-*.db)
  shopt -u nullglob
  [ "${#candidates[@]}" -gt 0 ] || die "no app-*.db snapshots in $BACKUP_DIR"
  # Names are date-stamped, and glob expansion is sorted, so the last is newest.
  SNAPSHOT="${candidates[$(( ${#candidates[@]} - 1 ))]}"
elif [ ! -f "$SNAPSHOT" ] && [ -f "$BACKUP_DIR/app-$SNAPSHOT.db" ]; then
  SNAPSHOT="$BACKUP_DIR/app-$SNAPSHOT.db"
fi
[ -f "$SNAPSHOT" ] || die "snapshot not found: $SNAPSHOT"
SNAPSHOT="$(cd "$(dirname "$SNAPSHOT")" && pwd)/$(basename "$SNAPSHOT")"

stamp="$(basename "$SNAPSHOT")"; stamp="${stamp#app-}"; stamp="${stamp%.db}"
ASSETS="$(dirname "$SNAPSHOT")/assets-${stamp}.tar.gz"
[ -f "$ASSETS" ] || ASSETS=""

# --- integrity ---------------------------------------------------------------
integrity_result() {
  # First line of PRAGMA integrity_check: "ok", or whatever went wrong. Errors
  # are folded into the RESULT rather than the exit status so a file that is not
  # a database at all reaches the caller's "!= ok" branch with a readable
  # message instead of killing the script through set -e.
  local out=""
  if command -v sqlite3 >/dev/null 2>&1; then
    out="$(sqlite3 "$1" 'PRAGMA integrity_check;' 2>&1 || true)"
  elif command -v python3 >/dev/null 2>&1; then
    out="$(python3 - "$1" <<'PY' 2>&1 || true
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    print(con.execute("PRAGMA integrity_check").fetchone()[0])
    con.close()
except Exception as e:
    print(f"{type(e).__name__}: {e}")
PY
)"
  else
    die "need sqlite3 or python3 to verify the snapshot; refusing to restore blind"
  fi
  # First line via expansion, not `| head`: under pipefail a closed pipe would
  # turn a mere long report into a hard exit.
  printf '%s\n' "${out%%$'\n'*}"
}

# --- refuse to fight a running app ------------------------------------------
if [ -n "$CONTAINER" ] && command -v docker >/dev/null 2>&1; then
  # Collect first, match second. `docker ps | grep -q` would fail OPEN: grep
  # exits on the first match, docker takes SIGPIPE, and pipefail then reports
  # the pipeline as failed — i.e. "not running" — exactly when it IS running.
  running="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
  if grep -qxF -- "$CONTAINER" <<<"$running"; then
    die "container '$CONTAINER' is still running. Stop it first (docker compose stop),
     then re-run. Replacing the DB under a live writer corrupts both."
  fi
fi
if [ -e "$DB" ]; then
  # A held file descriptor means a writer we cannot see (uvicorn on the host, a
  # forgotten sqlite3 shell). Either would keep writing into the file we are
  # about to move aside.
  if command -v lsof >/dev/null 2>&1; then
    if lsof -- "$DB" >/dev/null 2>&1; then
      die "$DB is open by another process (lsof). Stop the app first."
    fi
  elif command -v fuser >/dev/null 2>&1; then
    if fuser -s "$DB" 2>/dev/null; then
      die "$DB is open by another process (fuser). Stop the app first."
    fi
  fi
fi

# --- confirm -----------------------------------------------------------------
printf 'Restore plan\n'
printf '  data dir : %s\n' "$DATA_DIR"
printf '  snapshot : %s\n' "$SNAPSHOT"
if [ "$SKIP_ASSETS" -eq 1 ]; then
  printf '  assets   : skipped (--skip-assets)\n'
elif [ -n "$ASSETS" ]; then
  printf '  assets   : %s\n' "$ASSETS"
else
  printf '  assets   : NONE FOUND for %s — photos/contracts will NOT be restored\n' "$stamp"
fi
printf '  aside as : *.pre-restore-%s (nothing is deleted)\n' "$TS"
if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Proceed? type yes: '
  # EOF (piped/cron invocation) must abort loudly rather than die on set -e with
  # no explanation of why nothing happened.
  read -r reply || die "no answer on stdin — re-run with --yes for non-interactive use"
  [ "$reply" = "yes" ] || die "aborted by operator"
fi

# Verify the snapshot BEFORE touching the live DB: a corrupt snapshot must cost
# nothing, not leave the operator with two broken databases.
res="$(integrity_result "$SNAPSHOT")"
[ "$res" = "ok" ] || die "snapshot failed integrity_check: $res"
printf 'ok  snapshot integrity_check: ok\n'

# --- move the live DB aside (never delete) -----------------------------------
PRE_DB=""
for suffix in "" "-wal" "-shm"; do
  f="${DB}${suffix}"
  if [ -e "$f" ]; then
    mv -- "$f" "${f}.pre-restore-${TS}"
    printf 'ok  moved aside: %s -> %s\n' "$f" "${f}.pre-restore-${TS}"
    [ -n "$suffix" ] || PRE_DB="${f}.pre-restore-${TS}"
  fi
done
# The -wal/-shm of the OLD database are now out of the live path, which is the
# whole point: SQLite must open the restored file with no journal to replay.

# --- install the snapshot ----------------------------------------------------
tmp="${DB}.restore-${TS}"
cp -- "$SNAPSHOT" "$tmp"
mv -- "$tmp" "$DB"
printf 'ok  restored: %s -> %s\n' "$SNAPSHOT" "$DB"

res="$(integrity_result "$DB")"
if [ "$res" != "ok" ]; then
  rm -f -- "$DB"
  if [ -n "$PRE_DB" ]; then
    mv -- "$PRE_DB" "$DB"
    printf 'rolled back: previous database returned to %s\n' "$DB" >&2
  fi
  die "restored file failed integrity_check: $res (copy problem or bad media)"
fi
printf 'ok  restored database integrity_check: ok\n'

# --- assets ------------------------------------------------------------------
if [ "$SKIP_ASSETS" -ne 1 ] && [ -n "$ASSETS" ]; then
  # Read the member list once and validate it before extracting: tar would
  # happily follow an absolute or ../ path out of DATA_DIR.
  members="$(tar -tzf "$ASSETS")" || die "asset archive is unreadable: $ASSETS"
  if grep -qE '^(/|\.\./)' <<<"$members"; then
    die "asset archive contains paths outside the data dir; refusing to extract"
  fi
  tops="$(awk -F/ 'NF{print $1}' <<<"$members" | sort -u)"
  # read, not word-splitting: a directory name with a space must not turn into
  # two half-names and mv the wrong thing.
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    if [ -e "$DATA_DIR/$d" ]; then
      mv -- "$DATA_DIR/$d" "$DATA_DIR/${d}.pre-restore-${TS}"
      printf 'ok  moved aside: %s -> %s\n' "$DATA_DIR/$d" "$DATA_DIR/${d}.pre-restore-${TS}"
    fi
  done <<<"$tops"
  tar -xzf "$ASSETS" -C "$DATA_DIR"
  printf 'ok  extracted assets: %s -> %s (%s)\n' "$ASSETS" "$DATA_DIR" "$(tr '\n' ' ' <<<"$tops")"
fi

# --- summary -----------------------------------------------------------------
printf '\nRestore complete.\n'
printf '  database : %s (from %s)\n' "$DB" "$(basename "$SNAPSHOT")"
printf '  wal/shm  : removed from the live path (kept as *.pre-restore-%s)\n' "$TS"
if [ "$SKIP_ASSETS" -ne 1 ] && [ -n "$ASSETS" ]; then
  printf '  assets   : %s\n' "$(basename "$ASSETS")"
else
  printf '  assets   : not restored\n'
fi
printf '\nNext:\n'
printf '  1. If you ran this as root on the host, hand the files back to the app user:\n'
printf '       chown -R <PUID>:<PGID> %s\n' "$DATA_DIR"
printf '  2. Start the app:  docker compose up -d\n'
printf '  3. Spot-check a customer, a visit photo, and a signed contract PDF.\n'
printf '  4. Once satisfied, delete the *.pre-restore-%s leftovers in %s.\n' "$TS" "$DATA_DIR"
