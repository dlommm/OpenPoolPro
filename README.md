<p align="center">
  <img src="app/static/brand/openpoolpro-logo.png" alt="OpenPoolPro" width="380">
</p>

<p align="center"><em>Self-hosted pool-service management — routes, water chemistry, invoicing, and crew.</em></p>

---

Self-hosted pool-route app. Phase 0: customers, properties, pools, routes,
visits with chemistry costs and dose suggestions, jobs, analytics with income breakdowns and rate management, admin console with
dashboard and global search, company branding, branded PDF invoices with
month generation and batch zip, monthly QuickBooks Online CSV export, a
customer-360 timeline (visits, jobs, photos, comm log, quotes) with tags and
reactivation, seasonal service holds, per-property checklists, recurring
maintenance rules with an auto-job engine, equipment warranty alerts,
before/after/issue photo tags, quotes that convert to jobs, and chemical
inventory with stock levels, purchase logging feeding catalog costs,
auto-decrement from visit usage, and low-stock alerts.

## Run

    cp .env.example .env   # set APP_PASSWORD and SECRET_KEY
    docker compose up -d
    # Put a TLS reverse proxy in front — the compose file binds the app to
    # 127.0.0.1:8000 ON PURPOSE (the app trusts X-Forwarded-* from the proxy),
    # so it is NOT reachable from other machines directly. Point the proxy at
    # 127.0.0.1:8000 and set FORWARDED_ALLOW_IPS to the proxy's address.
    # First login is owner / your APP_PASSWORD
    # manage crew under Settings → Manage crew
    # change APP_PASSWORD in .env + restart to reset the owner login (recovery)
    # on iPhone: Share → Add to Home Screen

All data lives in ./data (SQLite DB, photos, nightly backups). Back up that
folder.

## Develop

    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt
    python scripts/build_css.py
    APP_PASSWORD=dev COOKIE_SECURE=0 uvicorn app.main:create_app --factory --reload
    ruff check app/ tests/ scripts/   # same gate CI runs
    pytest -n auto

## Security

- **Two-factor authentication (TOTP):** any user can turn on 2FA from
  `/account` — scan the QR code (or enter the manual key) into an
  authenticator app, confirm a 6-digit code, and 10 one-time backup codes
  are shown once (save/print them). Once enabled, login requires a code
  from the app (or an unused backup code) after the password step.
  Disabling 2FA or regenerating backup codes requires entering a current
  code. Admins can reset a crew member's 2FA from Settings → Manage crew
  if they lose their device.
- **Backup codes:** 10 single-use codes generated at enrollment (and again
  on regenerate), stored only as salted hashes — never in plaintext. Each
  works once; use one and it's consumed.
- **Login rate limiting:** 5 failed attempts (by username *and* by IP,
  whichever trips first) locks that key out for 5 minutes. Locked-out,
  bad-password, and unknown-username all return the identical response so
  an attacker can't tell which is which.
- **Audit log:** sensitive actions (login/lockout, password changes, 2FA
  enable/disable/reset, backup-code use, user create/disable/permission
  changes, rate changes, exports, CSV imports) are recorded with a
  timestamp, the acting user, and details. Admins can review and filter
  the trail at `/audit`.
- **Logout everywhere:** from `/account`, end every other signed-in
  session for your account in one click (also available per-user to
  admins from Settings → Manage crew). Password changes and 2FA changes
  do the same automatically for every *other* session, while keeping the
  device that made the change signed in.
- **Cookies:** session cookies are HttpOnly + SameSite=Lax always, and
  marked Secure (HTTPS-only) **by default** — correct for the documented
  reverse-proxy-with-TLS setup. For a deliberately plain-HTTP local/LAN
  deployment set `COOKIE_SECURE=0` in `.env` (without it, logins won't
  stick over HTTP); the app logs a warning at boot when it's off.
- **Response headers:** every response carries `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, and a
  restrictive `Content-Security-Policy`; when `COOKIE_SECURE` is on (the
  default), `Strict-Transport-Security` pins browsers to HTTPS as well.
- **Upgrading a live instance:** this release changes the pending-2FA
  cookie's purpose check, so any in-flight 2FA login started just before
  the upgrade will have its pending cookie rejected; the 5-minute pending
  TTL self-heals, and affected users just restart the login.

## Backup & restore

Every night at 01:00 (business timezone) the app writes one **generation**
into `data/backups/`:

    app-YYYY-MM-DD.db        SQLite snapshot (VACUUM INTO — consistent, no WAL needed)
    assets-YYYY-MM-DD.tar.gz photos/, invoices/, contracts/, branding/

The two halves belong together: the database stores *filenames*, so a
DB-only restore brings back visits whose photos and customer-signed
contract PDFs are missing. Both are kept for 14 generations and pruned in
step. If the box is off at 01:00 (night reboot, deploy), the next start
takes the missed snapshot instead of skipping the night.

Separately, any boot whose migration is about to actually change the
database first writes a DB-only `pre-migrate-<timestamp>.db` snapshot
(newest 3 kept) — the same-day pre-image for the upgrade itself, since the
nightly can be almost a day old by then. Restore one like any snapshot by
passing its path to `scripts/restore.sh` (with `--skip-assets`).

The 14 generations sit on the same disk as the live database — set
`BACKUP_SYNC_CMD` to also ship each one off the box, e.g.
`rclone copy "$BACKUP_FILE" remote:pool-backups && rclone copy "$BACKUP_ASSETS_FILE" remote:pool-backups`.
It runs with `BACKUP_DIR`, `BACKUP_FILE`, and `BACKUP_ASSETS_FILE` in its
environment. Only the failure *type* is ever recorded, so a command with a
password in it never lands in the database.

### RESTORE

Do these in order. **Never copy a snapshot over a running app** — the live
`app.db-wal` will replay frames from the *old* database over the restored
one and silently corrupt it. `scripts/restore.sh` handles this; use it.

1. **Stop the app.** Nothing may hold `data/app.db` open.

       docker compose stop            # or: docker stop openpoolpro (Unraid)

2. **Pick a generation.** They are listed newest-first in Settings →
   Backups, or on disk:

       ls -1 data/backups

3. **Run the restore.** With no arguments it takes the newest generation
   and prompts before anything destructive:

       ./scripts/restore.sh                          # newest
       ./scripts/restore.sh 2026-07-01               # a specific date
       ./scripts/restore.sh --yes --data-dir /mnt/cache/appdata/openpoolpro
       ./scripts/restore.sh --help                   # all options

   It refuses to run while the app is up, verifies the snapshot with
   `PRAGMA integrity_check` *before* touching anything, moves the existing
   `app.db` **and its `-wal`/`-shm` sidecars** aside to
   `*.pre-restore-<timestamp>` (never deletes them), installs the snapshot,
   re-checks integrity — rolling back if it fails — then restores the asset
   archive, moving any replaced `photos/` `invoices/` `contracts/`
   `branding/` aside the same way. It prints every step it took.

   If the container name is not `openpoolpro`, pass `--container <name>` so
   the "is it still running?" guard checks the right one.

4. **Fix ownership** if you ran it as root on the host (Unraid does):

       chown -R 99:100 /mnt/cache/appdata/openpoolpro   # your PUID:PGID

5. **Start the app and verify.**

       docker compose up -d

   Open a customer, a visit photo, and a signed contract PDF before you
   trust the restore.

6. **Clean up** the `*.pre-restore-<timestamp>` leftovers once you are
   satisfied — until then they are your way back.

Restoring from a copy pulled off-box works the same way: put
`app-<date>.db` and `assets-<date>.tar.gz` in a directory together and
pass the path to the `.db` file.

## Email notifications

Customers who opt in (a per-customer "Email visit notifications" checkbox
on the customer form) get a plain-text email when their visit is marked
done — business name, property, date, completed task labels, technician
name. Never a dollar amount or chemical amount. Sending goes through a
durable outbox: every notification is written as a row in the database
first, then a background job flushes it via SMTP every 5 minutes (or
immediately from Settings → Email → "Flush now" / "Send test email"). A
temporary SMTP outage just delays delivery — failed sends retry
automatically up to 5 attempts, and the last error is visible per-row in
Settings.

Configure via environment variables. Leaving `SMTP_HOST` unset disables the
feature entirely (outbox rows are still written, but nothing ever gets
flushed/sent):

| Variable        | Default              | Notes                                    |
|------------------|----------------------|-------------------------------------------|
| `SMTP_HOST`     | *(empty)*            | Empty = sending disabled.                 |
| `SMTP_PORT`     | `587`                |                                            |
| `SMTP_USER`     | *(empty)*            | Login step is skipped when empty.         |
| `SMTP_PASS`     | *(empty)*            |                                            |
| `SMTP_FROM`     | value of `SMTP_USER` | From address on outgoing mail.            |
| `SMTP_STARTTLS` | `1`                  | Set to `0` to send without STARTTLS.      |

Settings → Email shows the configured host only (credentials never render
anywhere), the last 50 outbox rows with status/attempts/last error, and
buttons to send a test email or flush the outbox on demand.

## Offline (PWA)

Installed to the Home Screen, the app keeps working with a weak or absent
signal:

- Every page you've already opened is cached on the device. The service
  worker is network-first — a fresh copy always wins the moment you have
  signal, so the cache is purely an offline fallback, never stale-serves
  over a live connection. Opening a page you've never visited while
  offline lands on a small self-contained "You're offline" page instead of
  a browser error.
- Completing or skipping a visit while offline is queued on the device,
  not lost: you land back on your cached Today page and a waiting-count
  badge appears next to the menu (and in the desktop sidebar) — that badge
  is the offline confirmation. (The "Saved offline — will sync" banner is
  server-rendered, so it appears only when the save happens with signal.) The queue is replayed automatically, oldest first, as soon as
  the device reconnects or the app is reopened/foregrounded — including
  the completion email that visit queues once it actually reaches the
  server.
- Photo uploads are **not** queued offline — those still need a live
  connection at the time.
- iOS Safari has no Background Sync API, so the queue is flushed on
  reconnect (`online` event) and on every app open, not silently in the
  background while the app is closed — reopen the app once you're back on
  signal to sync.

## Visual smoke check

Proves every screen renders with the design system applied (catches stale
service-worker CSS):

    APP_URL=http://localhost:8000 APP_PASSWORD=... python scripts/visual_check.py

Requires `pip install playwright && playwright install chromium`.
