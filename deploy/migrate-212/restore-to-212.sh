#!/usr/bin/env bash
# Restore the VM 242 TimescaleDB dump onto the .212 app host (2026-09-30 migration).
# Run FROM THE MAC, after homelab has confirmed /srv/vscc-data is mounted, and
# BEFORE the vscc stack is started for the first time:
#
#   RETENTION_HOURS=<n> [CHUNK_INTERVAL='1 hour'] [SESSIONS_TGZ=<tgz>] \
#       deploy/migrate-212/restore-to-212.sh <staging-dir>/<volume>
#
# <staging-dir>/<volume> holds stage-dump.sh's output: telemetry.dump,
# source-manifest.txt, source-verify.tsv. The dump is streamed over ssh into
# pg_restore, so it is never written to .212's disks.
# CHUNK_INTERVAL (optional) sets the hypertables' chunk size before anything is
# written. Retention drops WHOLE chunks, so with the default 7-day chunks a 12 h
# retention still keeps up to ~7.5 days of capture (measured 2026-09-30: about
# 7.4 GB per day of continuous capture); '1 hour' makes the retention bound real.
# SESSIONS_TGZ (optional) is the extract's session-export tarball; it is unpacked
# into /srv/vscc-data/sessions (existing files are kept).
#
# Order matters. The worker re-applies vscc_settings.retention_hours on every
# start, and the TimescaleDB scheduler runs an overdue retention job at once.
# Either one would drop the restored history. So:
#  1. a one-off restore container (no network) initialises the empty data dir
#     /srv/vscc-data/timescaledb with timescaledb.max_background_workers=0;
#  2. timescaledb_pre_restore(), pg_restore, then timescaledb_post_restore()
#     (with no background workers, nothing can run yet);
#  3. retention_hours and both retention policies are set to RETENTION_HOURS. If
#     that value would drop restored rows, the script refuses unless ALLOW_PURGE=1;
#  4. every table is checked against source-verify.tsv (rows, min/max time);
#  5. the restore container is stopped cleanly. Start the stack with
#     deploy/vscc-deploy.sh afterwards.
# POSTGRES_PASSWORD comes from BWS (VSCC_POSTGRES_PASSWORD) and travels over ssh stdin only.
set -euo pipefail

STAGE=${1:?usage: RETENTION_HOURS=<n> restore-to-212.sh <staging-dir>}
: "${RETENTION_HOURS:?set RETENTION_HOURS (the retention the restored DB gets; see deploy/migrate-212/README.md)}"
[[ "$RETENTION_HOURS" =~ ^[1-9][0-9]*$ ]] || { echo "RETENTION_HOURS must be a positive integer"; exit 2; }
CHUNK_INTERVAL=${CHUNK_INTERVAL:-}
if [ -n "$CHUNK_INTERVAL" ] && ! [[ "$CHUNK_INTERVAL" =~ ^[1-9][0-9]*\ (minutes?|hours?|days?)$ ]]; then
    echo "CHUNK_INTERVAL must look like '1 hour' / '6 hours' / '1 day'"; exit 2
fi
SESSIONS_TGZ=${SESSIONS_TGZ:-}
[ -z "$SESSIONS_TGZ" ] || [ -s "$SESSIONS_TGZ" ] || { echo "SESSIONS_TGZ $SESSIONS_TGZ not found"; exit 2; }
HOST=${HOST:-chris@192.168.1.212}
DB=telemetry
CT=vscc-restore-pg
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE=$(sed -n 's/^ *image: \(timescale\/timescaledb:[^ ]*\)$/\1/p' "$ROOT/compose.production.yml")
[ -n "$IMAGE" ] || { echo "could not read the timescaledb image from compose.production.yml"; exit 2; }
DUMP="$STAGE/$DB.dump"
VERIFY="$STAGE/source-verify.tsv"
MANIFEST="$STAGE/source-manifest.txt"
for f in "$DUMP" "$VERIFY" "$MANIFEST"; do [ -s "$f" ] || { echo "missing $f"; exit 2; }; done
(cd "$STAGE" && shasum -a 256 -c STAGE-SHA256SUMS)
SRC_EXT=$(awk '/^extension timescaledb /{print $3; exit}' "$MANIFEST")
[ -n "$SRC_EXT" ] || { echo "source timescaledb version not found in $MANIFEST"; exit 2; }

# r: remote command with stdin closed (safe inside `while read` loops);
# rin: remote command that consumes our stdin (secret pipe, dump stream).
r() { /usr/bin/ssh -n -o BatchMode=yes "$HOST" "$@"; }
rin() { /usr/bin/ssh -o BatchMode=yes "$HOST" "$@"; }
psql212() { r "docker exec -e PGTZ=UTC $CT psql -U postgres -d $DB -v ON_ERROR_STOP=1 -AtF \$'\\t' $*"; }

echo "== pre-flight on $HOST"
r 'set -e
   mountpoint -q /srv/vscc-data || { echo "/srv/vscc-data is NOT a mountpoint, refusing"; exit 3; }
   df -h /srv/vscc-data | tail -1
   if [ -d /srv/vscc-data/timescaledb ] && sudo find /srv/vscc-data/timescaledb -mindepth 1 -print -quit | grep -q .; then
       echo "/srv/vscc-data/timescaledb is not empty, refusing (this script only restores into a fresh data dir)"; exit 3; fi
   if docker ps -a --format "{{.Names}}" | grep -qxE "vscc-timescaledb|vscc-worker|'"$CT"'"; then
       echo "a vscc DB/worker/restore container already exists, refusing"; exit 3; fi
   sudo install -d -m 700 /srv/vscc-data/timescaledb
   sudo install -d -m 755 /srv/vscc-data/sessions'

echo "== init the data dir with background workers OFF ($IMAGE)"
zsh -c 'source ~/.config/bws-helpers.zsh; printf "%s\n" "$(bws-get VSCC_POSTGRES_PASSWORD)"' \
| rin "IFS= read -r P; export POSTGRES_PASSWORD=\"\$P\"; docker run -d --name $CT --network none \
     -v /srv/vscc-data/timescaledb:/var/lib/postgresql/data \
     -e POSTGRES_PASSWORD -e POSTGRES_DB=$DB -e TS_TUNE_MEMORY=2GB -e TS_TUNE_NUM_CPUS=2 \
     -e TIMESCALEDB_TELEMETRY=off --memory 2g --shm-size 256m \
     $IMAGE postgres -c timescaledb.max_background_workers=0 >/dev/null"
for _ in $(seq 1 90); do
    if r "docker logs $CT 2>&1 | grep -q 'PostgreSQL init process complete' && docker exec $CT pg_isready -U postgres -q"; then break; fi
    sleep 2
done
r "docker logs $CT 2>&1 | grep -q 'PostgreSQL init process complete'" || { echo "init did not complete"; r "docker logs --tail 30 $CT"; exit 4; }
[ "$(psql212 -c "'SHOW timescaledb.max_background_workers'")" = 0 ] || { echo "bgworkers not disabled, stopping"; r "docker stop $CT"; exit 4; }

TGT_EXT=$(psql212 -c "\"SELECT extversion FROM pg_extension WHERE extname='timescaledb'\"")
echo "timescaledb: source $SRC_EXT, target $TGT_EXT"
if [ "$TGT_EXT" != "$SRC_EXT" ]; then
    echo "== matching the extension version to the source ($SRC_EXT)"
    psql212 -c "\"DROP EXTENSION timescaledb\"" -c "\"CREATE EXTENSION timescaledb VERSION '$SRC_EXT'\""
fi

echo "== pre_restore + pg_restore (streamed) + post_restore"
psql212 -c "'SELECT timescaledb_pre_restore()'" >/dev/null
set +e
rin "docker exec -i $CT pg_restore -U postgres -d $DB --no-owner" < "$DUMP" 2> "$STAGE/restore-212.log"
rc=$?
set -e
echo "pg_restore exit $rc; $(grep -c 'error:' "$STAGE/restore-212.log" || true) error lines (log: $STAGE/restore-212.log)"
grep 'error:' "$STAGE/restore-212.log" | head -20 || true
psql212 -c "'SELECT timescaledb_post_restore()'" >/dev/null
psql212 -c "'ANALYZE'" >/dev/null

echo "== retention: $RETENTION_HOURS h"
oldest_h=$(psql212 -c "\"SELECT COALESCE(ceil(extract(epoch FROM now() - least((SELECT min(time) FROM patient_numerics), (SELECT min(time) FROM patient_waveforms))) / 3600), 0)::bigint\"")
echo "oldest restored row is ${oldest_h} h old"
if [ "$oldest_h" -gt "$RETENTION_HOURS" ] && [ "${ALLOW_PURGE:-0}" != 1 ]; then
    echo "REFUSING: RETENTION_HOURS=$RETENTION_HOURS would let the retention job drop restored history (oldest row ${oldest_h} h)."
    echo "The restore container is left running with no background workers; rerun the retention step or set ALLOW_PURGE=1 deliberately."
    exit 5
fi
psql212 -c "\"INSERT INTO vscc_settings (key, value) VALUES ('retention_hours', '$RETENTION_HOURS') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value\"" \
        -c "\"SELECT remove_retention_policy('patient_numerics', if_exists => TRUE)\"" \
        -c "\"SELECT add_retention_policy('patient_numerics', INTERVAL '$RETENTION_HOURS hours', if_not_exists => TRUE)\"" \
        -c "\"SELECT remove_retention_policy('patient_waveforms', if_exists => TRUE)\"" \
        -c "\"SELECT add_retention_policy('patient_waveforms', INTERVAL '$RETENTION_HOURS hours', if_not_exists => TRUE)\"" >/dev/null
if [ -n "$CHUNK_INTERVAL" ]; then
    echo "== chunk interval: $CHUNK_INTERVAL (applies to chunks created from now on)"
    psql212 -c "\"SELECT set_chunk_time_interval('patient_numerics', INTERVAL '$CHUNK_INTERVAL')\"" \
            -c "\"SELECT set_chunk_time_interval('patient_waveforms', INTERVAL '$CHUNK_INTERVAL')\"" >/dev/null
fi
psql212 -c "\"SELECT hypertable_name, time_interval FROM timescaledb_information.dimensions ORDER BY 1\""
psql212 -c "\"SELECT job_id, proc_name, hypertable_name, config FROM timescaledb_information.jobs ORDER BY 1\""

echo "== verify against the source manifest"
fail=0
while IFS=$'\t' read -r table rows tmin tmax; do
    if [ -n "$tmin" ]; then
        col=$(psql212 -c "\"SELECT column_name FROM timescaledb_information.dimensions WHERE format('%I.%I', hypertable_schema, hypertable_name) = '$table' AND dimension_number = 1\"")
        got=$(psql212 -c "\"SELECT count(*), min($col), max($col) FROM $table\"")
        want=$(printf '%s\t%s\t%s' "$rows" "$tmin" "$tmax")
    else
        got=$(psql212 -c "\"SELECT count(*) FROM $table\"")
        want=$rows
    fi
    if [ "$got" = "$want" ]; then echo "MATCH $table rows=$rows"; else echo "DIFF  $table source=[$want] target=[$got]"; fail=1; fi
done < "$VERIFY"

if [ -n "$SESSIONS_TGZ" ]; then
    echo "== session exports -> /srv/vscc-data/sessions"
    rin "sudo tar -xzf - -C /srv/vscc-data/sessions --skip-old-files --strip-components=4 home/chris/vscc-mqtt-server/sessions" < "$SESSIONS_TGZ"
    r 'sudo find /srv/vscc-data/sessions -type f | wc -l | sed "s/^/session export files: /"'
fi

r "docker stop -t 60 $CT >/dev/null && docker rm $CT >/dev/null"
r 'df -h /srv/vscc-data | tail -1; sudo du -sh /srv/vscc-data/timescaledb'
[ "$fail" = 0 ] || { echo "VERIFY FAILED: see the DIFF lines above"; exit 6; }
echo "restore verified; start the stack with deploy/vscc-deploy.sh"
