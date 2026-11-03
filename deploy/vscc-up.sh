#!/usr/bin/env bash
# Start or update the vscc stack on the .212 app host. Runs ON the box, from the
# git checkout /opt/apphost/vscc. deploy/vscc-deploy.sh (on the Mac) checks out
# the commit there and calls this with POSTGRES_PASSWORD piped from BWS over ssh
# stdin, so the secret is never written to disk.
#
# Exit codes: 3 = pre-flight refused (data disk / data dir / edge network),
#             4 = started but not healthy within ~3 min, the worker holds no
#                 EMQX connection, or EMQX runs an outdated ACL.
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
emqx_before=$(docker inspect -f '{{.Id}}' vscc-emqx 2>/dev/null || echo none)
"${C[@]}" up -d --remove-orphans
# `up` recreates a container only when its compose entry or image changes, so the
# dashboard would keep its bind mount of the PREVIOUS tree's dashboard-nginx.conf.
# Restart it so every deploy serves the shipped nginx config.
"${C[@]}" restart dashboard

# EMQX has the same trap with its ACL (deploy/emqx-acl.conf), and reads it at boot
# only. The worker does not reconnect to a restarted broker (WOR-840), so EMQX
# never restarts alone: an outdated ACL restarts EMQX and the worker, a recreated
# EMQX (new image or compose entry) restarts the worker.
acl_shipped=$(sha256sum deploy/emqx-acl.conf)
acl_running=$(docker exec vscc-emqx sha256sum /opt/emqx/etc/acl.conf)
emqx_after=$(docker inspect -f '{{.Id}}' vscc-emqx)
if [ "${acl_running%% *}" != "${acl_shipped%% *}" ]; then
    echo "EMQX runs an outdated ACL: restarting EMQX together with the worker"
    "${C[@]}" restart emqx worker
    acl_running=$(docker exec vscc-emqx sha256sum /opt/emqx/etc/acl.conf)
    [ "${acl_running%% *}" = "${acl_shipped%% *}" ] || { echo "EMQX still runs an outdated ACL"; exit 4; }
elif [ "$emqx_after" != "$emqx_before" ]; then
    echo "EMQX was recreated: restarting the worker so it reconnects"
    "${C[@]}" restart worker
fi

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
# healthy does not prove the live data path: the worker must hold an EMQX connection
wip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' vscc-worker)
connected=no
for _ in $(seq 1 12); do
    # emqx ctl exits 1 while the node is still booting; that is a retry, not a failure
    clients=$(docker exec vscc-emqx emqx ctl clients list 2>/dev/null || true)
    [[ $clients == *"peername=$wip:"* ]] && { connected=yes; break; }
    sleep 5
done
if [ "$connected" != yes ]; then
    echo "NOT CONNECTED: the worker ($wip) holds no connection to EMQX"
    exit 4
fi
echo "vscc up at $(git rev-parse HEAD 2>/dev/null || cat .deployed-commit 2>/dev/null || echo '?'): timescaledb and worker healthy"
