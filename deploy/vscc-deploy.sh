#!/usr/bin/env bash
# Deploy vscc to the .212 app host FROM THE MAC.
#
#   ~/VSCode/vscc-mqtt-server/deploy/vscc-deploy.sh [ref]     (default origin/main)
#
# 1. ships a `git archive` of this repo at <ref> to /opt/apphost/vscc on .212 and
#    records the commit in .deployed-commit (the box never holds a hand-edited
#    copy). The previous tree is kept as /opt/apphost/vscc.prev for a rollback;
# 2. pipes POSTGRES_PASSWORD from BWS (VSCC_POSTGRES_PASSWORD) over ssh stdin into
#    deploy/vscc-up.sh, which runs the pre-flight checks and `docker compose up`.
# The very first deploy comes AFTER deploy/migrate-212/restore-to-212.sh.
#
# GOTCHA (same as price-scout): `bws-get` prints with NO trailing newline, so the
# value goes through `printf "%s\n"` for the far-side `read`.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REF="${1:-origin/main}"
HOST=chris@192.168.1.212

git -C "$REPO" fetch -q origin
COMMIT="$(git -C "$REPO" rev-parse "$REF^{commit}")"
git -C "$REPO" archive "$COMMIT" | /usr/bin/ssh "$HOST" \
  "set -e; rm -rf /opt/apphost/vscc.new && mkdir -p /opt/apphost/vscc.new && tar -x -C /opt/apphost/vscc.new \
   && echo $COMMIT > /opt/apphost/vscc.new/.deployed-commit \
   && rm -rf /opt/apphost/vscc.prev && { [ ! -d /opt/apphost/vscc ] || mv /opt/apphost/vscc /opt/apphost/vscc.prev; } \
   && mv /opt/apphost/vscc.new /opt/apphost/vscc"

zsh -c 'source ~/.config/bws-helpers.zsh; printf "%s\n" "$(bws-get VSCC_POSTGRES_PASSWORD)"' \
| /usr/bin/ssh "$HOST" 'IFS= read -r P; cd /opt/apphost/vscc && POSTGRES_PASSWORD="$P" ./deploy/vscc-up.sh'
echo "deployed vscc $COMMIT to .212 (https://vscc.lan.synviron.com, behind Authelia)"
