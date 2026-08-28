# CI/CD & Releases

Automated testing, versioning, image builds, and releases for OpenPoolPro on the
self-hosted GitLab at `github.com`. Pipeline lives in `.gitlab-ci.yml`.

## What happens on a push to `main`

1. **test stage** — four parallel jobs, all of which must pass before release
   (except `audit`, which warns):
   - **lint** — `ruff check app/ tests/ scripts/` (config in `pyproject.toml`).
   - **audit** — `pip-audit` against `requirements.lock`; `allow_failure: true`
     so a brand-new upstream CVE turns the pipeline yellow without blocking an
     unrelated urgent fix.
   - **test** — the full `pytest` suite (`-n auto`, with coverage; floor 90%)
     inside a throwaway `python:3.12-slim` container, installed from
     `requirements-dev.lock` so CI tests exactly the pins the image ships.
   - **build-smoke** — builds the Docker image and runs it until its baked
     HEALTHCHECK reports healthy, so a broken Dockerfile/entrypoint fails the
     MR instead of the release.
2. **release** —
   - Computes the next version from **conventional commits** since the last tag
     (`scripts/next-version.sh`).
   - **Always** rebuilds and pushes `ghcr.io/dlommm/openpoolpro:latest`
     (the image bakes the version in via `--build-arg APP_VERSION`).
   - **On a version bump only**: also pushes `:vX.Y.Z`, creates and pushes the git
     tag `vX.Y.Z`, and publishes a GitLab **Release**.

Merge requests run the **test stage** only.

## Dependency locks

`requirements.txt` / `requirements-dev.txt` are the human-edited inputs (loose
floor pins); `requirements.lock` / `requirements-dev.lock` are compiled pins
that CI installs and the Dockerfile ships — so the released image can never
contain package versions CI didn't test. After editing either input file,
regenerate and commit both locks:

    uv pip compile --python-version 3.12 requirements.txt -o requirements.lock
    uv pip compile --python-version 3.12 requirements-dev.txt -o requirements-dev.lock

## Versioning (conventional commits)

`scripts/next-version.sh` scans commit subjects/bodies in `<last-tag>..HEAD`:

| Commit                                   | Bump  | Example        |
|------------------------------------------|-------|----------------|
| `feat!:` / `type!:` / `BREAKING CHANGE`  | major | v1.4.2 → v2.0.0 |
| `feat: ...`                              | minor | v1.4.2 → v1.5.0 |
| `fix: ...`                              | patch | v1.4.2 → v1.4.3 |
| only `docs/chore/refactor/test/...`      | none  | no new release; `:latest` still refreshed |
| no tags yet                              | —     | bootstraps **v1.0.0** |

So: keep using `feat:` / `fix:` / `feat!:` commit prefixes and versions bump themselves.

## Docker tags

- `:latest` — always the newest build of `main`.
- `:vX.Y.Z` — immutable, one per release, matches the git tag and the GitLab Release.

Deploy target is **Unraid (amd64)**. The GitLab runner is amd64, so CI builds natively.
(Building on an Apple-Silicon Mac needs `--build-arg APP_VERSION=… --platform linux/amd64`.)

## In-app version

`app/config.py:APP_VERSION` reads the `APP_VERSION` env var (default `dev`). The Dockerfile
sets it from the `APP_VERSION` build-arg, and CI passes the computed version. It renders on
the **Settings** page ("OpenPoolPro vX.Y.Z"), theme-agnostic. Local/dev shows `dev`.

## One-time setup (required for the pipeline to work)

1. **Docker-executor runner** assigned to this project, with a reachable docker daemon
   (the `test` job runs in a `python:3.12-slim` image; the `release` job runs in
   `docker:27-cli` and builds via `docker build`). The project's existing runner already
   provides this — no volume mounts are used (an earlier shell-runner assumption is gone).
**No extra secret is required.** The release job creates the git tag + GitLab Release via
the Releases API using the built-in `CI_JOB_TOKEN` (`POST /releases` with a `ref` creates
the tag at the commit and the Release). Docker registry auth also uses `CI_JOB_TOKEN`.

Until a docker runner is assigned, pushes to `main` won't build/release automatically — use
the manual fallback below.

## Manual release fallback (no runner / one-off)

From a checkout, logged into the registry (`docker login ghcr.io`):

```bash
VERSION=v1.2.3
# amd64 image for Unraid (add --platform linux/amd64 when building on Apple Silicon)
docker buildx build --platform linux/amd64 --build-arg APP_VERSION="$VERSION" \
  -t ghcr.io/dlommm/openpoolpro:latest \
  -t ghcr.io/dlommm/openpoolpro:"$VERSION" --push .
git tag -a "$VERSION" -m "Release $VERSION" && git push origin "$VERSION"
```

## Deploy on Unraid

```bash
docker compose -f docker-compose.unraid.yml pull
docker compose -f docker-compose.unraid.yml up -d
```
Data persists via the `/data` volume. Pin a specific version by setting the image to
`ghcr.io/dlommm/openpoolpro:vX.Y.Z` instead of `:latest`.
