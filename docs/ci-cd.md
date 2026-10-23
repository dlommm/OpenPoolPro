# CI/CD & Releases

Automated testing, versioning, image builds, and releases for OpenPoolPro run on
GitHub Actions. The pipeline lives in `.github/workflows/ci.yml`.

## What runs, and when

Every pull request and every push to `main` runs the checks below in parallel on
GitHub-hosted `ubuntu-latest` runners (clock set to `America/New_York`, which the
date-boundary tests depend on):

- **hygiene** — keeps the public repo to the application, its tests, and operator
  docs. Fails if agent/assistant tooling (`.claude/`, `.agents/`, `.codex/`,
  `CLAUDE.md`, `AGENTS.md`, `docs/superpowers/` …), secrets (`.env`, keys),
  databases, or local artifacts are tracked; if any tracked file matches
  `.gitignore` (i.e. was force-added); or if a commit message carries an AI
  co-author trailer.
- **lint** — `ruff check app/ tests/ scripts/` (config in `pyproject.toml`).
- **audit** — `pip-audit` against `requirements.lock`. Marked
  `continue-on-error`, so a brand-new upstream CVE shows up without blocking an
  unrelated urgent fix.
- **test** — the full `pytest` suite (`-n auto`, with coverage; floor 90%),
  installed from `requirements-dev.lock` so CI tests exactly the pins the image
  ships. Node is installed and `REQUIRE_JS_TESTS=1` is set, so the service-worker
  tests fail instead of silently skipping.
- **browser** — builds the Tailwind CSS, boots the app, seeds demo data, and runs
  `scripts/visual_check.py` in Chromium to prove every screen renders with the
  design system applied.
- **build-smoke** — builds the Docker image and runs it until its baked
  HEALTHCHECK reports healthy, so a broken Dockerfile/entrypoint fails the PR
  instead of the release.

On a push to `main`, once hygiene, lint, test, browser, and build-smoke are all
green, the **release** job runs:

- Computes the next version from **conventional commits** since the last tag
  (`scripts/next-version.sh`).
- **Always** rebuilds and pushes `:latest` to both registries (the image bakes
  the version in via `--build-arg APP_VERSION`):
  - `ghcr.io/dlommm/openpoolpro`
  - `docker.io/dendlomm/openpoolpro` (Docker Hub)
- **On a version bump only**: also pushes `:vX.Y.Z` to both and publishes a
  GitHub **Release**, which creates the git tag `vX.Y.Z` at that commit.
- **No bump**: if Docker Hub is missing the current `:vX.Y.Z`, the exact GHCR
  image is copied across (never rebuilt), so the two registries stay in step.

Releases are serialized (`concurrency: release`), so two quick merges can't race
to the same version. Never create version tags by hand.

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
| `fix: ...`                               | patch | v1.4.2 → v1.4.3 |
| only `docs/chore/refactor/test/...`      | none  | no new release; `:latest` still refreshed |
| no tags yet                              | —     | bootstraps **v1.0.0** |

So: keep using `feat:` / `fix:` / `feat!:` commit prefixes and versions bump themselves.

## Docker tags

- `:latest` — always the newest build of `main`.
- `:vX.Y.Z` — immutable, one per release, matches the git tag and the GitHub Release.

Both tags are identical in GHCR and Docker Hub; pull from whichever you prefer.

Deploy target is **Unraid (amd64)**. GitHub-hosted runners are amd64, so CI builds natively.
(Building on an Apple-Silicon Mac needs `--platform linux/amd64`.)

## In-app version

`app/config.py:APP_VERSION` reads the `APP_VERSION` env var (default `dev`). The Dockerfile
sets it from the `APP_VERSION` build-arg, and CI passes the computed version. It renders on
the **Settings** page ("OpenPoolPro vX.Y.Z"), theme-agnostic. Local/dev shows `dev`.

## One-time setup

The release job authenticates to GHCR and creates the Release with the built-in
`GITHUB_TOKEN` (the workflow requests `contents: write` and `packages: write` for
that job only). Docker Hub needs two repository secrets
(**Settings → Secrets and variables → Actions**):

- `DOCKERHUB_USERNAME` — the Docker Hub account that owns `dendlomm/openpoolpro`.
- `DOCKERHUB_TOKEN` — a Docker Hub personal access token with **Read & Write**
  scope (Docker Hub → Account settings → Personal access tokens).

For GHCR, after the first release publishes the package:

1. Open **GitHub → your profile → Packages → openpoolpro → Package settings**.
2. Under *Manage Actions access*, confirm the `OpenPoolPro` repository has
   **Write** (it is linked automatically through the image's
   `org.opencontainers.image.source` label).
3. Either set the package visibility to **Public** so Unraid can pull without
   credentials, or keep it private and `docker login ghcr.io` on the Unraid host
   with a classic PAT that has `read:packages`.

Branch protection on `main` requires a pull request and the `hygiene`, `lint`,
`test`, `browser`, and `build-smoke` checks; admins can still bypass in an emergency.

## Manual release fallback

From a checkout, logged into GHCR (`echo $PAT | docker login ghcr.io -u <user> --password-stdin`,
PAT with `write:packages`):

```bash
VERSION=v1.2.3
docker buildx build --platform linux/amd64 --build-arg APP_VERSION="$VERSION" \
  -t ghcr.io/dlommm/openpoolpro:latest \
  -t ghcr.io/dlommm/openpoolpro:"$VERSION" --push .
gh release create "$VERSION" --target main --title "$VERSION" --generate-notes
```

## Deploy on Unraid

```bash
docker compose -f docker-compose.unraid.yml pull
docker compose -f docker-compose.unraid.yml up -d
```
Data persists via the `/data` volume. Pin a specific version by setting the image to
`ghcr.io/dlommm/openpoolpro:vX.Y.Z` instead of `:latest`; to pull from Docker Hub
instead, use `dendlomm/openpoolpro:latest` (or `:vX.Y.Z`).
