import os
from zoneinfo import ZoneInfo

APP_VERSION = os.environ.get("APP_VERSION", "dev")  # baked at build time via --build-arg APP_VERSION
DATA_DIR = os.environ.get("DATA_DIR", "./data")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Known placeholder values that MUST NOT reach a real deployment. SECRET_KEY
# signs the session cookie and the pre-auth 2FA tokens, so a shipped default
# lets anyone forge an authenticated session -> full auth bypass. These are
# the module defaults above plus the literals the compose/README templates
# ship, so validate_secrets() catches an operator who never set the env vars.
_INSECURE_SECRET_KEYS = {"", "dev-secret-change-me", "generate-with-openssl-rand-hex-32",
                         "CHANGE_ME_run_openssl_rand_hex_32"}
_INSECURE_APP_PASSWORDS = {"", "changeme", "pick-a-long-password",
                           "CHANGE_ME_owner_login_password"}


def validate_secrets() -> None:
    """Refuse to run a real deployment on placeholder secrets. Called from
    create_app() only when it builds its own file-backed engine (a real boot);
    the test suite passes an in-memory engine and is exempt."""
    problems = []
    if SECRET_KEY in _INSECURE_SECRET_KEYS:
        problems.append("SECRET_KEY is unset or a known placeholder")
    if APP_PASSWORD in _INSECURE_APP_PASSWORDS:
        problems.append("APP_PASSWORD is unset or a known placeholder")
    if problems:
        raise RuntimeError(
            "Refusing to start with insecure configuration: " + "; ".join(problems)
            + ". Set SECRET_KEY (openssl rand -hex 32) and APP_PASSWORD in the environment.")
    if not COOKIE_SECURE:
        # Warn, don't refuse: a plain-HTTP LAN box is a legitimate (if
        # discouraged) self-hosted setup. But the operator must have CHOSEN it
        # — the 90-day session cookie travels in cleartext on every request.
        import logging
        logging.getLogger("app.config").warning(
            "COOKIE_SECURE=0: session cookies will be sent over plain HTTP. "
            "Serve the app over HTTPS and remove COOKIE_SECURE=0 unless this "
            "deployment is deliberately HTTP-only.")


# Secure by default: browsers only send the session + pre-auth 2FA cookies
# over HTTPS. The documented deployment is behind a TLS-terminating reverse
# proxy, and a Secure-less 90-day cookie leaks in cleartext on any stray
# http:// hit. Opt OUT (COOKIE_SECURE=0) only for a deliberately plain-HTTP
# deployment or local dev in browsers that don't exempt http://localhost.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"
TIMEZONE = os.environ.get("APP_TZ", "America/New_York")
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "0") == "1"
# Optional shell command run after each successful nightly backup, so backups
# can be shipped OFF the box (they otherwise sit on the same disk as the live
# DB — one disk failure loses both). The command runs with BACKUP_DIR and
# BACKUP_FILE in its environment; e.g. 'rclone copy "$BACKUP_FILE" remote:pool-backups'.
BACKUP_SYNC_CMD = os.environ.get("BACKUP_SYNC_CMD", "")

# Trusted-header SSO for a reverse proxy that already authenticates the user
# (Pangolin, Authelia, authentik, etc.). When PROXY_AUTH_SECRET is set, a
# request carrying header PROXY_AUTH_HEADER whose value equals the secret is
# logged in automatically as the owner — no login screen. The proxy MUST
# strip this header from untrusted inbound traffic and only add it on requests
# it has authenticated; anyone who can send the header directly to the app is
# authenticated, which is exactly why the app must be reachable ONLY through
# the proxy (bind loopback / keep the port unpublished). Empty = disabled, and
# the normal password/2FA login is always available as a fallback.
PROXY_AUTH_SECRET = os.environ.get("PROXY_AUTH_SECRET", "")
PROXY_AUTH_HEADER = os.environ.get("PROXY_AUTH_HEADER", "X-OpenPool-Auth")
# Optional: header carrying the proxy-authenticated username, so SSO maps to a
# specific staff user (for correct audit attribution) instead of the owner.
PROXY_AUTH_USER_HEADER = os.environ.get("PROXY_AUTH_USER_HEADER", "X-OpenPool-User")

# Per-IP request throttle for an internet-exposed deployment: blunts floods,
# scrapers, and brute-force sweeps from a single source. NOT DDoS protection —
# a distributed (many-IP) flood must be absorbed upstream (Cloudflare, or
# Pangolin/Traefik rate-limit middleware / Crowdsec). Requests over the limit
# get 429. 0 disables it. Only PUBLIC client IPs are throttled: if
# FORWARDED_ALLOW_IPS is misconfigured and the app sees a private proxy IP for
# everyone, the throttle fails OPEN (no-op) rather than locking out all users.
try:
    REQUEST_RATE_LIMIT_PER_MIN = int(os.environ.get("REQUEST_RATE_LIMIT_PER_MIN", "300"))
except ValueError:
    REQUEST_RATE_LIMIT_PER_MIN = 300

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") != "0"


def tz():
    return ZoneInfo(TIMEZONE)
