#!/usr/bin/env bash
# Mac staging step (2026-09-30 migration off VM 242). It turns the COLD PGDATA
# tarball read off the VM 242 disk clone (extract-from-clone.sh) into:
#   source-manifest.txt  versions, DB and hypertable sizes, per-table row counts
#                        + min/max time, TimescaleDB jobs, vscc_settings;
#                        restore-to-212.sh verifies the restore against it
#   source-verify.tsv    the machine-readable part of the manifest
#   <db>.dump            pg_dump -Fc (TimescaleDB-aware: restored between
#                        timescaledb_pre_restore() / timescaledb_post_restore())
#
# The scratch server runs with timescaledb.max_background_workers=0 and no
# network. That is the whole point: a TimescaleDB scheduler would run the
# overdue retention job at once and drop the history before it is dumped.
#
#   DOCKER_CONTEXT=colima-vscc-stage bash stage-dump.sh <staging-dir>
# Run it on the Mac staging copy, against a DEDICATED Colima profile
# (`colima start --profile vscc-stage --vm-type vz --arch aarch64 --disk 120`).
# On 2026-09-30 the default profile was unusable (I/O errors) and the .135
# helper has no Docker; `colima delete --profile vscc-stage` removes the scratch
# VM, PHI included, afterwards. x86_64 -> aarch64 PGDATA is fine here: both are
# 64-bit little-endian with the same alignment, the same alpine/musl image runs on
# both sides, and postgres refuses to start on a pg_control mismatch anyway.
# Patient data: everything stays on the LAN; never paste rows or dumps into a
# chat or a cloud tool.
set -euo pipefail

STAGE=${1:?usage: stage-dump.sh <extract-dir holding pgdata-*.tgz + SHA256SUMS>}
IMAGE=${IMAGE:-timescale/timescaledb:2.19.3-pg14}
VOL=vscc-stage-pgdata
CT=vscc-stage-pg
cd "$STAGE"

if command -v sha256sum >/dev/null; then SHA=(sha256sum); else SHA=(shasum -a 256); fi
"${SHA[@]}" -c SHA256SUMS
tgzs=(pgdata-*.tgz)
[ "${#tgzs[@]}" = 1 ] || { echo "expected exactly one pgdata-*.tgz, found: ${tgzs[*]}"; exit 2; }

docker rm -f "$CT" >/dev/null 2>&1 || true
docker volume rm "$VOL" >/dev/null 2>&1 || true
docker volume create "$VOL" >/dev/null
docker run --rm -i --network none --entrypoint tar -v "$VOL":/stage "$IMAGE" \
    --numeric-owner -xzf - -C /stage < "${tgzs[0]}"

# Small, fixed settings on the command line override the VM-tuned postgresql.conf
# inside PGDATA (sized for VM 242, not for this scratch server).
docker run -d --name "$CT" --network none \
    -v "$VOL":/var/lib/postgresql/stage -e PGDATA=/var/lib/postgresql/stage/_data \
    "$IMAGE" postgres \
    -c timescaledb.max_background_workers=0 -c timescaledb.telemetry_level=off \
    -c listen_addresses='' -c shared_buffers=256MB -c effective_cache_size=1GB \
    -c work_mem=16MB -c maintenance_work_mem=256MB -c max_connections=20 >/dev/null

for _ in $(seq 1 120); do
    docker exec "$CT" pg_isready -U postgres -q && break
    sleep 1
done
docker exec "$CT" pg_isready -U postgres
bgw=$(docker exec "$CT" psql -U postgres -Atc "SHOW timescaledb.max_background_workers")
[ "$bgw" = 0 ] || { echo "background workers are NOT disabled ($bgw), stopping"; docker stop "$CT"; exit 3; }

# PGTZ=UTC: min/max timestamps must print identically here and on .212
q() { docker exec -e PGTZ=UTC "$CT" psql -U postgres -v ON_ERROR_STOP=1 -d "$1" -AtF $'\t' -c "$2"; }

DBS=$(q postgres "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1")
M=source-manifest.txt
V=source-verify.tsv
: > "$V"
{
    echo "# VM 242 TimescaleDB, staged $(date -u +%FT%TZ) from ${tgzs[0]} (bgworkers disabled)"
    q postgres "SELECT version()"
    q postgres "SHOW timescaledb.max_background_workers" | sed 's/^/timescaledb.max_background_workers=/'
    echo; echo "## databases"
    q postgres "SELECT datname, pg_size_pretty(pg_database_size(datname)), pg_database_size(datname) FROM pg_database WHERE NOT datistemplate ORDER BY 1"
    echo; echo "## roles"; q postgres "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1"
    for db in $DBS; do
        echo; echo "### database $db"
        q "$db" "SELECT 'extension '||extname||' '||extversion FROM pg_extension ORDER BY 1"
        echo "## hypertables (name, total size, bytes, chunks)"
        q "$db" "SELECT format('%I.%I', hypertable_schema, hypertable_name),
                        pg_size_pretty(hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass)),
                        hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass), num_chunks
                 FROM timescaledb_information.hypertables ORDER BY 1"
        echo "## continuous aggregates"
        q "$db" "SELECT view_name FROM timescaledb_information.continuous_aggregates ORDER BY 1"
        echo "## jobs (id, proc, hypertable, schedule, config, scheduled, next_start)"
        q "$db" "SELECT job_id, proc_name, hypertable_name, schedule_interval, config, scheduled, next_start
                 FROM timescaledb_information.jobs ORDER BY 1"
        if [ "$(q "$db" "SELECT to_regclass('public.vscc_settings') IS NOT NULL")" = t ]; then
            echo "## vscc_settings"; q "$db" "SELECT key, value FROM vscc_settings ORDER BY 1"
        fi
        echo "## rows per table (table, rows, min time, max time)"
        # hypertables: count + time range on the time dimension
        while IFS=$'\t' read -r ht col; do
            r=$(q "$db" "SELECT count(*), min($col), max($col) FROM $ht")
            printf '%s\t%s\n' "$ht" "$r" | tee -a "$V"
        done < <(q "$db" "SELECT format('%I.%I', hypertable_schema, hypertable_name), column_name
                          FROM timescaledb_information.dimensions WHERE dimension_number = 1 ORDER BY 1")
        # ordinary application tables: count only
        while IFS= read -r t; do
            r=$(q "$db" "SELECT count(*) FROM $t")
            printf '%s\t%s\t\t\n' "$t" "$r" | tee -a "$V"
        done < <(q "$db" "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                          WHERE c.relkind = 'r' AND n.nspname = 'public'
                            AND c.oid NOT IN (SELECT format('%I.%I', hypertable_schema, hypertable_name)::regclass
                                              FROM timescaledb_information.hypertables)
                          ORDER BY 1")
    done
} > "$M"

for db in $DBS; do
    docker exec "$CT" pg_dump -U postgres -Fc -d "$db" > "$db.dump"
    n=$(docker exec -i "$CT" pg_restore -l < "$db.dump" | grep -cv '^;')
    echo "dumped $db: $(du -h "$db.dump" | cut -f1), $n TOC entries" | tee -a "$M"
done

docker stop -t 60 "$CT" >/dev/null
docker rm "$CT" >/dev/null
[ "${KEEP_VOLUME:-0}" = 1 ] || docker volume rm "$VOL" >/dev/null
"${SHA[@]}" ./*.dump "$M" "$V" > STAGE-SHA256SUMS
echo "staged: $(ls ./*.dump | tr '\n' ' ')  manifest: $M"
