# Deploying OpenPoolPro

OpenPoolPro is one Docker image (`dendlomm/openpoolpro`, also
`ghcr.io/dlommm/openpoolpro`) built for x86 (amd64) and ARM (arm64), so it runs
on any VPS provider: Hostinger, Hetzner (including the ARM CAX plans),
DigitalOcean, Linode, Vultr, OVH, Oracle Cloud's Ampere instances, AWS
Lightsail or a box at home.

What changes from one setup to the next is the **reverse proxy** in front of
it, which gives you HTTPS. Pick the compose file that matches yours:

| You have | Use |
| --- | --- |
| Pangolin on the same server | `docker-compose.pangolin.yml` |
| A bare VPS with nothing on ports 80/443 | `docker-compose.caddy.yml` (brings Caddy and a Let's Encrypt certificate) |
| Nginx, Nginx Proxy Manager, Caddy or Traefik on the host, or a Cloudflare Tunnel | `docker-compose.yml` (app on `127.0.0.1:8000`) |
| Unraid | `docker-compose.unraid.yml` |

Every option starts the same way:

```bash
mkdir openpoolpro && cd openpoolpro
curl -O https://raw.githubusercontent.com/dlommm/OpenPoolPro/main/<the file>
echo "SECRET_KEY=$(openssl rand -hex 32)" > .env
```

After the app starts, read the one-time setup code from its log, open the site
and follow first-time setup:

```bash
docker compose -f <the file> logs app | grep "Setup code"
```

All data lives in `./data` next to the compose file. Back that folder up.

## Pangolin

Pangolin's installer runs Pangolin, Gerbil and Traefik on a Docker network
called `pangolin`. `docker-compose.pangolin.yml` puts OpenPoolPro on that
network with no published port, so the only way in is through Pangolin.

1. `docker compose -f docker-compose.pangolin.yml up -d`
2. In the Pangolin dashboard, add an HTTP resource with your subdomain on the
   site that runs on this server, and set its target to `openpoolpro`, port
   `8000`.
3. Read the setup code (above) and open the resource's URL.

If your Pangolin network has another name (`docker network ls`), set
`PANGOLIN_NETWORK=<name>` in `.env`.

**Pangolin sign-in in front of the app.** If you leave Pangolin's
authentication on for this resource, add bypass rules so customers can still
open the pages they get by email, which carry their own single-use links:
`/portal/*`, `/sign/*`, `/static/*`, `/branding/*`, `/manifest.json` and
`/sw.js`. Without them, customers hit a Pangolin login they can't pass.

**Single sign-on (optional).** OpenPoolPro can trust Pangolin's login instead
of asking for its own:

1. Put a long random value in `.env`: `PROXY_AUTH_SECRET=$(openssl rand -hex 32)`.
2. In the resource's settings, add a custom header `X-OpenPool-Auth` with that
   same value.
3. Optionally pass the signed-in user's name in `X-OpenPool-User` so the app
   logs actions against the right staff member; otherwise SSO signs in as the
   owner.

Only turn this on with Pangolin's authentication enabled: anyone who reaches
the app with that header is signed in. The normal password and two-step
sign-in keep working alongside it.

**OpenPoolPro on a different machine from Pangolin.** Run a Newt site
connector on that machine, run OpenPoolPro there with `docker-compose.yml`,
and point the resource at the address Newt can reach the app on.

## Caddy (bare VPS)

1. Point your domain's DNS `A` record (and `AAAA` for IPv6) at the server.
2. Open ports **80** and **443** in the provider's firewall as well as on the
   server (`ufw allow 80,443/tcp`). Most providers have a cloud firewall in
   their control panel that blocks them until you allow them.
3. Add `DOMAIN=pools.example.com` to `.env`.
4. `docker compose -f docker-compose.caddy.yml up -d`

Caddy gets and renews the certificate itself and redirects HTTP to HTTPS. Keep
the `./caddy` folder; it holds the certificates.

## Your own proxy on the host

`docker-compose.yml` publishes the app on `127.0.0.1:8000` only. Point the
proxy there. Set `FORWARDED_ALLOW_IPS` in `.env` to the address the proxy
connects from as the container sees it. For a proxy on the host that is
usually the Docker bridge gateway, `172.17.0.1` (the default). With a wrong
value, the sign-in rate limiter and audit log see the proxy's address instead
of the visitor's.

Cloudflare Tunnel works the same way: point the tunnel at
`http://localhost:8000`.

## Updating

```bash
docker compose -f <the file> pull
docker compose -f <the file> up -d
```

The app backs up its database before applying any migration. To stay on one
release, set `OPENPOOLPRO_TAG=v2.0.1` (for example) in `.env`.

## Forgotten owner password

```bash
docker compose -f <the file> exec app python -m app.manage reset-owner
```

prints a one-time reset link valid for 30 minutes. Add `--clear-2fa` if the
phone with the authenticator is lost too.

## Plain HTTP for testing

Session cookies are HTTPS-only. To try the app over plain `http://` on a test
box, add `COOKIE_SECURE=0` to `.env` and use `docker-compose.yml`. Don't run a
real business that way.
