#!/usr/bin/env bash
# Start or update the vscc stack on the .212 app host. Runs ON the box, from the
# deployed tree /opt/apphost/vscc. deploy/vscc-deploy.sh (on the Mac) ships the
# tree and calls this with POSTGRES_PASSWORD piped from BWS over ssh stdin, so
# the secret is never written to disk.
#
# Exit codes: 3 = pre-flight refused (data disk / data dir / edge network),
#             4 = started but not healthy within ~3 min.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${POSTGRES_PASSWORD:?pipe it from BWS VSCC_POSTGRES_PASSWORD (see deploy/vscc-deploy.sh)}"
export POSTGRES_PASSWORD
# the base compose file requires MONITOR_IP; the Philips MP50 is a fixed LAN address
export MONITOR_IP=${MONITOR_IP:-192.168.1.215}

# TimescaleDB lives on its own disk. Refuse to start against the bare mountpoint
# directory on the root disk (the fstab entry is nofail, so .212 still boots,
# and this stack stays down, if the disk is missing).
mountpoint -q /srv/vscc-data || { echo "/srv/vscc-data is NOT mounted; not starting vscc"; exit 3; }
for d in /srv/vscc-data/timescaledb /srv/vscc-data/sessions; do
    [ -d "$d" ] || { echo "$d is missing; not starting vscc"; exit 3; }
done
if ! sudo test -f /srv/vscc-data/timescaledb/PG_VERSION && [ "${FRESH_DB:-0}" != 1 ]; then
    echo "no database in /srv/vscc-data/timescaledb. Restore first (deploy/migrate-212/restore-to-212.sh),"
    echo "or set FRESH_DB=1 to deliberately start with an empty one."
    exit 3
fi
docker network inspect edge >/dev/null || { echo "external network 'edge' missing"; exit 3; }

C=(docker compose -p vscc -f docker-compose.yml -f compose.production.yml)
"${C[@]}" config --quiet
"${C[@]}" pull --quiet
"${C[@]}" up -d --remove-orphans

db=starting; wk=starting
for _ in $(seq 1 36); do
    db=$(docker inspect -f '{{.State.Health.Status}}' vscc-timescaledb 2>/dev/null || echo missing)
    wk=$(docker inspect -f '{{.State.Health.Status}}' vscc-worker 2>/dev/null || echo missing)
    [ "$db" = healthy ] && [ "$wk" = healthy ] && break
    sleep 5
done
"${C[@]}" ps --format 'table {{.Name}}\t{{.Image}}\t{{.Status}}'
if [ "$db" != healthy ] || [ "$wk" != healthy ]; then
    echo "NOT HEALTHY: timescaledb=$db worker=$wk"
    exit 4
fi
echo "vscc up at $(cat .deployed-commit 2>/dev/null || echo '?'): timescaledb and worker healthy"
