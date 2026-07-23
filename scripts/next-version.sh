#!/usr/bin/env bash
# Compute the next semantic version from conventional commits since the last vX.Y.Z tag.
#
# Prints two eval-able lines:
#   VERSION=vX.Y.Z
#   BUMP=major|minor|patch|initial|none
#
# Rules (conventional commits, scanning commits in <last-tag>..HEAD):
#   - "feat!:" / "type!:" / a "BREAKING CHANGE" footer  -> major
#   - "feat:"                                            -> minor
#   - "fix:"                                             -> patch
#   - anything else only                                -> none (no new release; keep last tag)
#   - no tags yet                                        -> bootstrap v1.0.0 (initial)
#
# Requires full history + tags (in CI: GIT_DEPTH=0 and `git fetch --tags`).
set -euo pipefail

last=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)

if [ -z "$last" ]; then
  echo "VERSION=v1.0.0"
  echo "BUMP=initial"
  exit 0
fi

range="${last}..HEAD"
subjects=$(git log "$range" --format='%s' 2>/dev/null || true)
bodies=$(git log "$range" --format='%B' 2>/dev/null || true)

bump=none
if printf '%s\n' "$subjects" | grep -qE '^[a-z]+(\([^)]+\))?!:' \
   || printf '%s\n' "$bodies" | grep -qE '^BREAKING[ -]CHANGE:'; then
  bump=major
elif printf '%s\n' "$subjects" | grep -qE '^feat(\([^)]+\))?:'; then
  bump=minor
elif printf '%s\n' "$subjects" | grep -qE '^fix(\([^)]+\))?:'; then
  bump=patch
fi

if [ "$bump" = "none" ]; then
  echo "VERSION=${last}"
  echo "BUMP=none"
  exit 0
fi

ver=${last#v}
MA=${ver%%.*}; rest=${ver#*.}; MI=${rest%%.*}; PA=${rest#*.}
MA=${MA:-0}; MI=${MI:-0}; PA=${PA:-0}
case "$bump" in
  major) MA=$((MA + 1)); MI=0; PA=0 ;;
  minor) MI=$((MI + 1)); PA=0 ;;
  patch) PA=$((PA + 1)) ;;
esac
echo "VERSION=v${MA}.${MI}.${PA}"
echo "BUMP=${bump}"
