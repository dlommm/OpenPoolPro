# Coastline Pools Planner

Self-hosted pool-route app. Phase 0: customers, properties, pools, routes,
visits with chemistry costs and dose suggestions, jobs, analytics with income breakdowns and rate management, admin console with
dashboard and global search, company branding, branded PDF invoices with month generation and batch zip, monthly QuickBooks
Online CSV export.

## Run

    cp .env.example .env   # set APP_PASSWORD and SECRET_KEY
    docker compose up -d
    # open http://<host>:8000 — first login is owner / your APP_PASSWORD
    # manage crew under Settings → Manage crew
    # change APP_PASSWORD in .env + restart to reset the owner login (recovery)
    # on iPhone: Share → Add to Home Screen

All data lives in ./data (SQLite DB, photos, nightly backups). Back up that
folder.

## Develop

    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python scripts/build_css.py
    APP_PASSWORD=dev uvicorn app.main:create_app --factory --reload
    pytest

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
- **Cookies:** session cookies are HttpOnly + SameSite=Lax always. Set
  `COOKIE_SECURE=1` in `.env` when the app is served over HTTPS (e.g.
  behind a reverse proxy with TLS) so cookies are also marked Secure —
  leave it `0` for plain-HTTP local/LAN use.
- **Response headers:** every response carries `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, and a
  restrictive `Content-Security-Policy`.
- **Upgrading a live instance:** this release changes the pending-2FA
  cookie's purpose check, so any in-flight 2FA login started just before
  the upgrade will have its pending cookie rejected; the 5-minute pending
  TTL self-heals, and affected users just restart the login.

## Visual smoke check

Proves every screen renders with the design system applied (catches stale
service-worker CSS):

    APP_URL=http://localhost:8000 APP_PASSWORD=... python scripts/visual_check.py

Requires `pip install playwright && playwright install chromium`.
