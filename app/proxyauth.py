"""Trusted-header SSO: log a user in from a header a reverse proxy injects.

For deployments behind a proxy that already authenticates people (Pangolin,
Authelia, authentik, Cloudflare Access, ...). When PROXY_AUTH_SECRET is set,
a request carrying config.PROXY_AUTH_HEADER == that secret is accepted as
authenticated, so staff never see OpenPoolPro's own login screen.

SECURITY MODEL — read before enabling: this trusts an HTTP header, so it is
only safe when the app is reachable ONLY through the proxy and the proxy
STRIPS the header from client input and RE-ADDS it only after authenticating.
Anyone able to send the header straight to the app is authenticated. Hence:
bind the container to loopback / leave the port unpublished, and set the
secret long and random. The header value is compared in constant time.

The optional user header (PROXY_AUTH_USER_HEADER) maps the SSO identity to a
specific active staff user for correct audit attribution; missing -> owner,
unknown -> rejected. Password + 2FA login always remains available as a
fallback (this never disables it).
"""
import secrets

from sqlalchemy import select

from app import config, models


def resolve_user(request, session) -> "models.User | None":
    """The User a trusted proxy header authenticates, or None when proxy auth
    is disabled or the header is absent/wrong. Never raises."""
    secret = config.PROXY_AUTH_SECRET
    if not secret:
        return None
    presented = request.headers.get(config.PROXY_AUTH_HEADER, "")
    if not presented or not secrets.compare_digest(presented, secret):
        return None

    username = (request.headers.get(config.PROXY_AUTH_USER_HEADER, "") or "").strip()
    user = None
    if username:
        user = session.scalars(
            select(models.User).where(models.User.username == username)).first()
    else:
        # No username header: the first active owner (there is no fixed
        # "owner" account any more).
        user = session.scalars(
            select(models.User).where(models.User.role == "admin", models.User.active)
            .order_by(models.User.id)).first()
    return user if (user is not None and user.active) else None
