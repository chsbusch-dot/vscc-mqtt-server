#!/usr/bin/env bash
# Deploy vscc to the .212 app host FROM THE MAC.
#
#   ~/VSCode/vscc-mqtt-server/deploy/vscc-deploy.sh [ref]     (default origin/main)
#
# /opt/apphost/vscc on .212 is a git checkout of this repo (WOR-1034, the
# git-everywhere standard WOR-203). The repo is public, so the box fetches over
# anonymous HTTPS and holds no credential and no deploy key.
# 1. resolves <ref> to a commit here, then has the box `git fetch origin` and
#    `git checkout --detach` that commit. It refuses when a tracked file on the
#    box is modified, and it asserts box HEAD == the commit before anything
#    restarts. The commit is also written to .deployed-commit (gitignored);
# 2. pipes POSTGRES_PASSWORD from BWS (VSCC_POSTGRES_PASSWORD) over ssh stdin into
#    deploy/vscc-up.sh, which runs the pre-flight checks and `docker compose up`.
# The ref must be pushed: the box fetches from GitHub, never from this Mac.
# Rollback = run this again with the "box before" commit that step 1 prints.
# A new box first needs the clone, see deploy/README.md; the very first deploy
# comes AFTER deploy/migrate-212/restore-to-212.sh.
#
# Exit codes of step 1. On any of them nothing is restarted:
#   2  <ref> does not resolve to a commit here
#   3  the box dir is not a checkout, the fetch failed, or the commit is not on origin
#   5  a tracked file on the box is modified, or the checkout failed
#   6  box HEAD != the commit after the checkout
#
# GOTCHA (same as price-scout): `bws-get` prints with NO trailing newline, so the
# value goes through `printf "%s\n"` for the far-side `read`.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REF="${1:-origin/main}"
HOST=chris@192.168.1.212

git -C "$REPO" fetch -q origin
COMMIT="$(git -C "$REPO" rev-parse --verify --quiet "$REF^{commit}")" \
  || { echo "vscc-deploy: $REF is not a commit" >&2; exit 2; }
case "$COMMIT" in
  *[!0-9a-f]*|'') echo "vscc-deploy: unexpected commit id: $COMMIT" >&2; exit 2 ;;
esac
[ "${#COMMIT}" -eq 40 ] || { echo "vscc-deploy: unexpected commit id: $COMMIT" >&2; exit 2; }

# Runs on the box. Single-quoted, so nothing in it expands here; SHA is prepended
# below and is a validated 40-hex commit id. Every git step is checked on its own,
# never through a pipe.
read -r -d '' BODY <<'REMOTE_EOF' || true
set -euo pipefail
CO=/opt/apphost/vscc
[ -d "$CO/.git" ] || { echo "FATAL: $CO is not a git checkout (deploy/README.md)"; exit 3; }
BEFORE=$(git -C "$CO" rev-parse HEAD)
echo "box before: $BEFORE"
dirty=$(git -C "$CO" status --porcelain --untracked-files=no)
if [ -n "$dirty" ]; then
    echo "FATAL: tracked files are modified on the box. Nothing was changed:"
    printf '%s\n' "$dirty"
    exit 5
fi
if ! out=$(GIT_TERMINAL_PROMPT=0 git -C "$CO" fetch --prune --quiet origin 2>&1); then
    echo "FATAL: git fetch failed on the box. Nothing was changed:"
    printf '%s\n' "$out"
    exit 3
fi
if ! git -C "$CO" cat-file -e "$SHA^{commit}" 2>/dev/null; then
    echo "FATAL: $SHA is not on origin (push it first). Nothing was changed."
    exit 3
fi
if ! out=$(git -C "$CO" -c advice.detachedHead=false checkout --quiet --detach "$SHA" 2>&1); then
    echo "FATAL: git checkout failed on the box:"
    printf '%s\n' "$out"
    exit 5
fi
HD=$(git -C "$CO" rev-parse HEAD)
if [ "$HD" != "$SHA" ]; then
    echo "FATAL: box HEAD is $HD, not $SHA, after the checkout"
    exit 6
fi
printf '%s\n' "$HD" > "$CO/.deployed-commit"
echo "box after:  $HD"
echo "rollback:   deploy/vscc-deploy.sh $BEFORE"
REMOTE_EOF

rc=0
# shellcheck disable=SC2029  # expanding here is intended: SHA is validated above
/usr/bin/ssh "$HOST" "SHA=$COMMIT
$BODY" || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "vscc-deploy: ABORTED (exit $rc). The stack was NOT restarted." >&2
    exit "$rc"
fi

zsh -c 'source ~/.config/bws-helpers.zsh; printf "%s\n" "$(bws-get VSCC_POSTGRES_PASSWORD)"' \
| /usr/bin/ssh "$HOST" 'IFS= read -r P; cd /opt/apphost/vscc && POSTGRES_PASSWORD="$P" ./deploy/vscc-up.sh'
echo "deployed vscc $COMMIT to .212 (https://vscc.lan.synviron.com, behind Authelia)"
