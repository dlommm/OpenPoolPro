<p align="center">
  <img src="app/static/brand/openpoolpro-logo.png" alt="OpenPoolPro" width="380">
</p>

<p align="center"><em>Self-hosted pool-service management — routes, water chemistry, invoicing, and crew.</em></p>

<p align="center"><a href="https://dlommm.github.io/OpenPoolPro/">Website</a> · <a href="https://hub.docker.com/r/dendlomm/openpoolpro">Docker Hub</a> · <a href="https://github.com/dlommm/OpenPoolPro/releases">Releases</a></p>

---

Self-hosted pool-route app for small pool-service companies, built to run on
one box (Docker + SQLite) and to be used from an iPhone in the field.

- **Field:** Today and route sheets, visits with water chemistry, dose
  suggestions and chemical costs, per-property checklists, before/after/issue
  photos, and an offline-capable PWA that queues work without signal.
- **Office:** customers, properties, pools and spas, recurring routes and
  service holds, jobs and quotes, maintenance rules with an auto-job engine,
  equipment warranty alerts, chemical inventory, contracts with e-signing, and a
  customer-360 timeline.
- **Billing:** branded PDF invoices, QuickBooks Online (reviewed billing and CSV
  export), sales tax, accounts receivable, and payment follow-ups.
- **Admin:** crew and permissions, 2FA, analytics and rate management, route
  planning, customer portals, water alerts, an operations dashboard, email (SMTP
  or Office 365), and nightly backups with optional off-box sync.

## Run

    cp .env.example .env   # set SECRET_KEY (openssl rand -hex 32)
    docker compose up -d
    # (builds from source; to run the published image instead use
    #  deploy/docker-compose.yml, which pulls dendlomm/openpoolpro:latest)
    # Put a TLS reverse proxy in front — the compose file binds the app to
    # 127.0.0.1:8000 ON PURPOSE (the app trusts X-Forwarded-* from the proxy),
    # so it is NOT reachable from other machines directly. Point the proxy at
    # 127.0.0.1:8000 and set FORWARDED_ALLOW_IPS to the proxy's address.
    # Open the app: first-time setup asks for the setup code from
    #   docker compose logs app | grep "Setup code"
    # then creates your owner account (your name, username and password).
    # Forgot the owner password? Print a one-time reset link:
    #   docker compose exec app python -m app.manage reset-owner [--clear-2fa]
    # on iPhone: Share → Add to Home Screen

All data lives in ./data (SQLite DB, photos, nightly backups). Back up that
folder.

**Upgrading from a version that used `APP_PASSWORD`:** your `owner` account keeps
working with its current password. The first time you sign in after the upgrade,
you're asked to add your name, choose your own username and set a new password.
After that `APP_PASSWORD` is never read, so remove it from `.env`.

### Deploy the published image (Unraid)

CI publishes `:latest` on every merge to `main`, plus an immutable `:vX.Y.Z` per
release, to both `ghcr.io/dlommm/openpoolpro` and Docker Hub
(`dendlomm/openpoolpro`). `docker-compose.unraid.yml` is a ready-made stack
for Unraid's Docker Compose Manager — read the SETUP notes at the top of the file
(cache-only appdata share, required `SECRET_KEY`), then:

    docker compose -f docker-compose.unraid.yml pull
    docker compose -f docker-compose.unraid.yml up -d

## Develop

    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt
    python scripts/build_css.py
    COOKIE_SECURE=0 uvicorn app.main:create_app --factory --reload
    # the setup code is printed in the log on first start
    ruff check app/ tests/ scripts/   # same gate CI runs
    pytest -n auto

CI runs on GitHub Actions for every pull request and push to `main`: lint,
tests (90% coverage floor), a browser smoke check, a Docker build-and-boot check,
a dependency audit, and a repo-hygiene check. Merges to `main` publish the image
and cut a release automatically from conventional commit prefixes (`feat:`,
`fix:`, `feat!:`). See [CI/CD & releases](docs/ci-cd.md).

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
- **Require 2FA (for an internet-exposed login):** Settings → System →
  Login security has a "Require 2FA for all staff" switch. With it on, any
  staff member without 2FA is sent to `/account` to enroll (and can reach
  nothing else) the next time they use the app. The card lists who still
  needs to enroll.
- **Request throttle:** each public client IP is capped at
  `REQUEST_RATE_LIMIT_PER_MIN` requests/minute (default 300; `0` disables),
  returning `429` over the limit — blunts single-source floods, scrapers,
  and brute-force sweeps. Only *public* IPs are throttled, so a
  misconfigured `FORWARDED_ALLOW_IPS` fails open (no throttle) rather than
  locking every user out. This is **not** DDoS protection: a distributed
  flood must be absorbed upstream — put Cloudflare (or another edge/CDN) in
  front of your domain, or use Pangolin/Traefik rate-limit middleware or
  Crowdsec. `/health` is never throttled.
- **Credentials encrypted at rest:** the settings rows that hold secrets —
  SMTP password, Office 365 refresh token, QuickBooks client secret and
  refresh token, Geoapify key — are stored AES-256-GCM encrypted, with the
  key derived from `SECRET_KEY` (which lives in the environment, *not* in
  `DATA_DIR`). Nightly backups copy the database but never the environment,
  so a leaked or misconfigured off-box backup no longer yields working
  tokens. This does **not** protect against an attacker who already has the
  host — they can read `SECRET_KEY` too. Existing plaintext values are
  encrypted automatically on first boot after upgrading.
  *If you upgraded an existing install: backups taken before the upgrade
  still contain the old plaintext secrets — rotate them (reconnect
  QuickBooks/Office 365, change the SMTP password) or purge those snapshots.*
  Rotating `SECRET_KEY` makes stored credentials unreadable; the app treats
  them as unset and you re-enter them in Settings.
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
  nosniff`, `X-Frame-Options: SAMEORIGIN` (so the app can frame its own
  contract PDFs while other sites can't frame it — reinforced by CSP
  `frame-ancestors 'self'`), `Referrer-Policy: same-origin`, and a
  nonce-based `Content-Security-Policy` (no `'unsafe-inline'` for scripts);
  when `COOKIE_SECURE` is on (the default), `Strict-Transport-Security`
  pins browsers to HTTPS as well.
- **Upgrading a live instance:** this release changes the pending-2FA
  cookie's purpose check, so any in-flight 2FA login started just before
  the upgrade will have its pending cookie rejected; the 5-minute pending
  TTL self-heals, and affected users just restart the login.

## Reverse proxy & SSO (Pangolin, Traefik, nginx…)

The app is designed to run behind a TLS-terminating reverse proxy that is the
*only* way in — bind the container to loopback or leave its port unpublished
so nothing reaches it except through the proxy.

**Real client IP.** The app reads `X-Forwarded-For`/`X-Real-IP`, but only from
a trusted source — set `FORWARDED_ALLOW_IPS` to the address (or CIDR) the
proxy connects *from*. For Pangolin/Traefik in Docker that's the proxy
container's network, e.g. `FORWARDED_ALLOW_IPS=172.18.0.0/16` (a CIDR is
accepted). To verify: open **Settings → System → Connection check** — it
shows the **public (client) IP** and the forwarded headers for your current
request. That public IP is what gets recorded on e-signature certificates
(the signer's apparent internet address) and is taken from the leftmost
`X-Forwarded-For` entry, so it survives a multi-hop chain (Pangolin tunnel →
Traefik) even if a hop is missing from `FORWARDED_ALLOW_IPS`. The separate
"trusted peer" value is what the login/​sign rate limiters key on — a
security control, so it uses only the IP uvicorn resolved against
`FORWARDED_ALLOW_IPS`. If either shows an internal `172.x`/`10.x` address
instead of a real one, widen `FORWARDED_ALLOW_IPS` to cover every proxy hop.

**Auto-login via a trusted header (SSO).** If your proxy already
authenticates staff (Pangolin resource auth, Authelia, authentik, Cloudflare
Access…), OpenPoolPro can skip its own login screen for those requests:

1. Set a long random shared secret in the app's environment:
   `PROXY_AUTH_SECRET=$(openssl rand -hex 32)`.
2. Configure the proxy to inject that value as a header on requests it has
   authenticated. In **Pangolin**, on the resource → *Additional Headers* (or
   the underlying Traefik `headers` middleware), add
   `X-OpenPool-Auth: <the same secret>`. The header name is configurable via
   `PROXY_AUTH_HEADER` (default `X-OpenPool-Auth`).
3. That's it — an authenticated request is logged in as the **owner** and gets
   a normal session cookie, so later requests ride the cookie whether or not
   the header is present. Password + 2FA login still works as a fallback.
4. *(Optional, for audit attribution)* have the proxy also pass the
   authenticated username in `PROXY_AUTH_USER_HEADER` (default
   `X-OpenPool-User`); it maps to that active staff user instead of the owner.
   An unknown or inactive named user is rejected; only an absent username
   uses the owner fallback. Create the staff account before enabling its SSO mapping.

⚠️ **Trust model:** this authenticates anyone who can send the header. It is
safe **only** because the app is reachable solely through the proxy and the
proxy strips any client-supplied copy of the header and re-adds it itself.
Never publish the app's port directly with `PROXY_AUTH_SECRET` set.

**Customer sign links.** The public `/sign/<token>` pages must be reachable
by customers — don't restrict them behind the proxy's staff auth. In Pangolin,
leave `/sign` (and `/static`, `/branding`, `/sw.js`, `/manifest.json`) on a
public path rule while gating the rest. Set **Settings → System → Public base
URL** to your public domain so emailed links use it (see "Contract e-signing").

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
done — business name, property, date, water readings and targets, chemicals
added, completed task labels, visit notes, and technician name. Dollar amounts
are excluded. Sending goes through a
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

### Email via Office 365 (Modern Auth / OAuth 2.0)

Microsoft is retiring password ("basic") SMTP auth in Exchange Online. For a
Microsoft 365 mailbox, use the built-in Modern Auth connection instead — the
app never sees or stores the mailbox password, and tokens refresh themselves.

One-time setup (~3 minutes, free), in the [Microsoft Entra admin
center](https://entra.microsoft.com) → Applications → App registrations:

1. **New registration** — any name (e.g. "OpenPoolPro mail"), supported
   account types: *Accounts in this organizational directory only*. No
   redirect URI needed.
2. On the app's Overview page, copy the **Application (client) ID** and
   **Directory (tenant) ID** into OpenPoolPro's Settings → Email → Office 365
   card and save.
3. **Authentication** → Advanced settings → set **Allow public client
   flows** to *Yes* (this is what enables the device-code sign-in).
4. **API permissions** → Add a permission → Microsoft Graph → Delegated →
   **SMTP.Send** → Add. (If your tenant requires admin consent for new
   apps, click *Grant admin consent* too.)
5. In Exchange admin, the sending mailbox must have **Authenticated SMTP**
   enabled (Recipients → the mailbox → Email apps). Modern Auth replaces
   the password, not the SMTP protocol itself.

Then click **Connect Office 365** in Settings → Email: enter the shown code
at microsoft.com/devicelogin, sign in as the sending mailbox, and the page
flips to "Connected" by itself. Host/port/STARTTLS are pinned automatically
(smtp.office365.com:587); the From address defaults to the connected
mailbox. Disconnecting (or saving the password form with "switch to
password sending" ticked) falls back to classic SMTP AUTH.

If Microsoft invalidates the grant (password reset, ~90 days of no
sending, consent revoked), sends fail with a "reconnect in Settings"
error and queued mail simply waits — reconnect and flush.

## Contract e-signing

Sending a contract emails the customer a unique signing link
(`/sign/<token>`) next to the PDF attachment. The link is 256-bit random,
single-use, and time-boxed (24 hours to 30 days — picked on the send form);
only its hash is stored, a resend replaces it, and signing or voiding kills
it. The customer reviews the PDF, types their full legal name, ticks the
terms-and-conditions box, and the app appends a Certificate of Electronic
Signature page (name, timestamp, IP) to the exact PDF that was emailed and
registers it as the signed copy.

**Deployment requirement: customers must be able to reach the app.** The
signing page is served by this app itself — a LAN-only deployment will email
links customers can never open. Run behind your HTTPS reverse proxy on a
public domain, and if staff browse the app by LAN address, set
**Settings → System → Public base URL** to the public domain so emailed
links use it (otherwise links are built from whatever address the sender
was browsing from).

Operational notes: contract emails are sent synchronously (no outbox
retry) — a failed send shows a banner on the contract page and changes
nothing, so just resend. Signing links appear in no logs (uvicorn's access
log is disabled and the app's own request log redacts the token), and the
`/sign` pages are exempt from staff login but rate-limited per IP.

## QuickBooks Online

Push your customers into QuickBooks Online over Intuit's OAuth 2.0 API, then
let QuickBooks issue and email the official invoices — no QuickBooks password
is stored (only a refresh token, same discipline as the Office 365
connection). The CSV export (**Money → Export**) remains as the
zero-dependency fallback.

One-time setup, in the [Intuit Developer portal](https://developer.intuit.com):

1. Create an app and enable the **Accounting** scope.
2. Under **Keys & OAuth**, add this **Redirect URI** exactly:
   `https://<your-domain>/settings/qbo/callback` (use your public domain — the
   one in Settings → System → Public base URL).
3. Copy the **Client ID** and **Client Secret** into Settings → System →
   QuickBooks Online, leave **Sandbox** ticked to test against a fake company
   first, and save.
4. Click **Connect QuickBooks**, approve on Intuit's screen, and you'll return
   to Settings showing "Connected — <company>".
5. **Load items from QuickBooks** and map your service/labor and chemical
   charges to QuickBooks items, choosing the income account new items post to.
   After that, billing looks after itself: a line posts to an item chosen by
   the *kind* of work — weekly labor, a job's template, a job type, or the
   individual chemical — and approving a draft creates whatever is missing and
   links whatever already exists. The draft lists what approving will add
   before you approve it, so nothing appears in your books unannounced. No suitable
   items yet? **Don't have these items? Create them** creates the two service
   items against an income account you pick and maps them in one step, linking
   to any that already exist rather than making duplicates.
6. **Review customer sync** shows what would change in both directions before
   anything is written. Customers missing from QuickBooks are created; an exact
   name match links to the existing record instead of duplicating it; a field
   QuickBooks holds empty is filled automatically; and anything that would
   *overwrite* a QuickBooks value — or rename a customer — waits for your
   per-field approval. The same page imports the QuickBooks roster into
   OpenPoolPro, which is how you onboard when QuickBooks already has your
   customers.

OpenPoolPro never deletes, voids, merges or deactivates anything in QuickBooks.
The API client refuses to build such a request at all, so the guarantee holds by
construction rather than by convention. Its only writes are: create an item,
create a customer, update customer fields you approved, create an invoice, and
send an invoice.

Going to **production** requires Intuit's app review (start it early in the
Developer portal); until then, keep the environment on Sandbox. Refresh tokens
expire after ~100 days of inactivity — reconnect if that ever happens.

### Reviewed billing

**Billing** collects completed work into monthly per-customer drafts. Someone
with **Can approve and send QuickBooks billing** (or an administrator) reviews
the charges and approves; QuickBooks then creates the invoice, emails it, and
hosts payment collection. Approval freezes the charges and recipient, and what
QuickBooks returns is compared against that snapshot before any email goes out
— a mismatch stops delivery and shows you the difference instead. Invoice
balances, payments, and credits are reconciled back every five minutes when the
scheduler is on (`RUN_SCHEDULER=1`), and surface on the billing page, customer
profiles, the AR page, and the customer portal.

Before enabling it: **turn off automatic invoice emails in QuickBooks** and
exercise the whole flow against a sandbox company. The API's `EmailStatus` is
not a verified override of company-level automation, so that check is yours to
make, not something the app can assert for you.

See [the billing workflow](docs/quickbooks-billing.md) for the monthly
routine and its operational limits, and [the transport
contract](docs/quickbooks-api-contract.md) for what the API client does and
does not guarantee.

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
- Queued work belongs to the person who queued it. Each entry records the
  signed-in staff member, and it only sends under that login — on a shared
  phone, a colleague's pending stops stay parked (and are shown as theirs)
  rather than syncing under your name. The server re-checks this and refuses a
  mismatch, so attribution in completed-by, the audit log and payroll-facing
  reports stays honest. Signing in as someone else also clears the cached
  pages, so the previous tech's customer details (gate codes, dog notes) stop
  being readable offline on that device.
- Photo uploads are **not** queued offline — those still need a live
  connection at the time.
- iOS Safari has no Background Sync API, so the queue is flushed on
  reconnect (`online` event) and on every app open, not silently in the
  background while the app is closed — reopen the app once you're back on
  signal to sync.

## Planning, customer portals, and business health

Open **Operations** in the administrator menu for route planning, payment
follow-ups, water alerts, and operational health. Each customer detail page
also has **Customer portal** access controls.

- **Route planner** (`/routes/plan`): editable suggested stop order and
  approximate travel estimates (great-circle distance × 1.3 at 25 mph, no
  external API). Saving changes the recurring weekday order used by Today and Week.
- **Customer portal:** expiring (7/30/90-day), revocable, read-only links to
  completed service records, sent invoices, agreements, and explicitly shared
  photos. Set **Settings → System → Public base URL** to the public HTTPS origin,
  and let the proxy pass `/portal/` and `/static/` without staff authentication
  (as it already does for `/sign/`). Treat a link as a credential.
- **Payment follow-ups** (`/followups`): configurable overdue thresholds, full
  message previews, explicit approval, delivery history, and cancellation when
  invoice data changes. Nothing is sent until a preview is approved.
- **Water alerts:** repeated or worsening readings with staff-created inspection jobs.
- **Operations:** backup/storage/mail/scheduler status, integration configuration,
  and unsynced work on the current device.

All of these require an administrator, except the route planner, which needs
`view_all_routes`.

## Water testers

Readings can come straight from digital testers instead of being typed:
LabCOM-connected photometers (PoolLab, PoolWaterLAB, PrimeLab), Ondilo ICO and
iopool in-pool monitors, and CSV exports from eXact iDip, Hanna Lab, LaMotte
WaterLink Connect or any other tester app. A reading is only tied to a pool by
an explicit link, by the tech choosing it on that pool's visit, or by the office
in the water-tests inbox. See [supported testers and setup](docs/water-testers.md).

## Visual smoke check

Proves every screen renders with the design system applied (catches stale
service-worker CSS). Covers the staff screens and their form pages, all seven
Settings panels, the 403/404 error pages, and the customer portal — the last
opened on a freshly issued token in a browser context with no session, which
is how a customer arrives. Every staff screen and the portal are rendered
twice, once on a light device and once on a dark one, and each render is held
to a 7:1 body-text contrast ratio:

    APP_URL=http://localhost:8000 OWNER_USER=... OWNER_PASSWORD=... python scripts/visual_check.py

Point it at a disposable instance: to reach those screens it creates a
"Visual Tech" staff member and issues then revokes a portal link for customer 1,
restoring the Public base URL it borrows to do so. Pass `THEME=control-room`
(with that theme active in Settings) to check a non-default theme.

Requires `pip install playwright && playwright install chromium`.

## Documentation

- [CI/CD & releases](docs/ci-cd.md) — pipeline, versioning, image tags, deploy
- [QuickBooks billing](docs/quickbooks-billing.md) — monthly reviewed-billing routine
- [QuickBooks transport contract](docs/quickbooks-api-contract.md) — API client guarantees
- [Water testers](docs/water-testers.md) — supported testers and monitors, and setup

## License

OpenPoolPro is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(AGPL-3.0-only). You may use, modify, and self-host it; if you run a modified
version that other people use over a network, you must offer them the source of
your modified version under the same license.
