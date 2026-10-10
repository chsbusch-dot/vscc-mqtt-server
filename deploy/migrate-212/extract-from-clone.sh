#!/usr/bin/env bash
# One-shot (2026-09-30 migration off VM 242): read the TimescaleDB data and the
# capture configuration off a COLD CLONE of VM 242's disk. Homelab attaches the
# clone as a second disk to a helper VM. VM 242 itself is never booted, because
# a boot starts Docker, and TimescaleDB's overdue retention job would drop the
# history within seconds (see deploy/migrate-212/README.md).
#
# Run as root on the helper VM, AFTER checking with lsblk which disk the clone is:
#   sudo bash extract-from-clone.sh /dev/sdX
#
# What it does:
#  - renames the clone's LVM VG (vgimportclone: the clone's VG is ubuntu-vg, the
#    same as the helper's), then mounts it read-only. The journal is replayed only
#    if the clone needs recovery.
#  - writes to $OUT (chris-owned, mode 700):
#      inventory.txt        what is on the clone (volumes, containers, units, sizes)
#      pgdata-<vol>.tgz     a cold tarball of each Docker volume holding a PGDATA
#                           (postgres is not running, so a file copy is consistent)
#      capture-config.tgz   systemd units, crontab, and the capture/worker checkouts
#                           minus the bulk live-export files
#      sessions-<dir>.tgz   the worker's session-export directories, if any
#      SHA256SUMS
#  - unmounts and deactivates the VG on exit, even on failure, so the disk can be detached.
# Patient data: the tarballs hold PHI. Keep them on the LAN (Mac staging), never in chat or a cloud.
set -euo pipefail

DISK=${1:?usage: sudo bash extract-from-clone.sh /dev/sdX   (the attached VM 242 clone)}
OUT=${OUT:-/home/chris/vscc-extract}
OWNER=${OWNER:-chris}
MNT=/mnt/vscc242clone
VG=vscc242clone

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 2; }
[ -b "$DISK" ] || { echo "$DISK is not a block device"; exit 2; }

# --- positive identification of the clone, refuse anything else ----------------
if lsblk -nro MOUNTPOINT "$DISK" | grep -q .; then
    echo "REFUSING: something on $DISK is mounted, so this is not the detached clone"; lsblk "$DISK"; exit 3
fi
size_g=$(( $(blockdev --getsize64 "$DISK") / 1024 / 1024 / 1024 ))
if [ "$size_g" -lt 50 ] || [ "$size_g" -gt 70 ]; then
    echo "REFUSING: $DISK is ${size_g} GiB, but the VM 242 clone is 60 GiB"; exit 3
fi
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$DISK"

LV=""
cleanup() {
    set +e
    mountpoint -q "$MNT" && umount "$MNT"
    vgs "$VG" >/dev/null 2>&1 && vgchange -an "$VG" >/dev/null
    [ -d "$OUT" ] && chown -R "$OWNER:$OWNER" "$OUT"
}
trap cleanup EXIT

PV=$(lsblk -lnpo NAME,FSTYPE "$DISK" | awk '$2=="LVM2_member"{print $1}')
if [ -n "$PV" ]; then
    [ "$(printf '%s\n' "$PV" | wc -l)" = 1 ] || { echo "more than one LVM PV on $DISK: $PV"; exit 3; }
    if ! vgs "$VG" >/dev/null 2>&1; then
        extra=()
        [ -f /etc/lvm/devices/system.devices ] && extra=(--importdevices)
        vgimportclone "${extra[@]}" --basevgname "$VG" "$PV"
    fi
    vgchange -ay "$VG" >/dev/null
    # the root LV = the largest LV in the VG
    LV=$(lvs --noheadings --units b --nosuffix -o lv_path,lv_size --sort -lv_size "$VG" | awk 'NR==1{print $1}')
else
    LV=$(lsblk -lnpbo NAME,FSTYPE,SIZE "$DISK" | awk '$2=="ext4"{print $3, $1}' | sort -n | tail -1 | cut -d' ' -f2)
fi
[ -n "$LV" ] || { echo "no root filesystem found on $DISK"; exit 3; }

mkdir -p "$MNT"
if dumpe2fs -h "$LV" 2>/dev/null | grep -q needs_recovery; then
    echo "NOTE: the clone's ext4 journal needs recovery; mounting ro WITH journal replay (writes the clone only)"
    mount -o ro "$LV" "$MNT"
else
    mount -o ro,noload "$LV" "$MNT"
fi

install -d -m 700 -o "$OWNER" -g "$OWNER" "$OUT"
INV="$OUT/inventory.txt"
{
    echo "# VM 242 clone inventory, $(date -u +%FT%TZ), disk $DISK, fs $LV"
    grep -E '^(PRETTY_NAME|VERSION_ID)=' "$MNT/etc/os-release" || true
    echo "hostname: $(cat "$MNT/etc/hostname" 2>/dev/null)"
    echo; echo "## filesystem"; df -h "$MNT" | tail -1
    echo; echo "## docker volumes (du -sh)"
    du -sh "$MNT"/var/lib/docker/volumes/*/ 2>/dev/null || echo "(none)"
    echo; echo "## docker containers (secret-looking env values masked)"
    python3 - "$MNT/var/lib/docker/containers" <<'PY'
import json, pathlib, re, sys
for cfg in sorted(pathlib.Path(sys.argv[1]).glob("*/config.v2.json")):
    c = json.loads(cfg.read_text())
    host = json.loads((cfg.parent / "hostconfig.json").read_text()) if (cfg.parent / "hostconfig.json").exists() else {}
    env = []
    for e in c.get("Config", {}).get("Env", []) or []:
        k, _, v = e.partition("=")
        if re.search(r"PASS|SECRET|TOKEN|KEY", k, re.I):
            v = "<masked>"
        if not k.startswith(("PATH", "GOSU", "PG_SHA", "LANG", "DOTNET_", "ASPNETCORE_", "APP_UID", "PYTHON_", "GPG_KEY")):
            env.append(f"{k}={v}")
    print(f"- {c.get('Name','?').lstrip('/')}: image={c.get('Config',{}).get('Image')} id={c.get('Image','')[:19]} "
          f"running_at_shutdown={c.get('State',{}).get('Running')} restart={host.get('RestartPolicy',{}).get('Name')}")
    for m in (c.get("MountPoints") or {}).values():
        print(f"    mount {m.get('Name') or m.get('Source')} -> {m.get('Destination')}")
    if env:
        print("    env " + " ".join(env))
PY
    echo; echo "## systemd units mentioning vsc/mp50/capture"
    grep -liE 'vsc|mp50|capture' "$MNT"/etc/systemd/system/*.service 2>/dev/null || echo "(none)"
    echo; echo "## enabled (wants) links"
    for w in "$MNT"/etc/systemd/system/multi-user.target.wants/*; do
        case "${w##*/}" in *vsc*|*mp50*|*capture*|*docker*) echo "${w##*/}" ;; esac
    done
    echo; echo "## crontab chris"
    cat "$MNT/var/spool/cron/crontabs/chris" 2>/dev/null | grep -v '^#' || echo "(none)"
    echo; echo "## home dirs of interest (du -sh)"
    for d in vscc-mqtt-server VSCaptureMQTT Downloads/VSCapture7; do
        [ -e "$MNT/home/chris/$d" ] && du -sh "$MNT/home/chris/$d"
    done
    echo; echo "## VSCaptureCLI binaries (sha256)"
    find "$MNT/home/chris" -name 'VSCaptureCLI.dll' -exec sha256sum {} + 2>/dev/null | sed "s#$MNT##" || true
} > "$INV"

# --- cold PGDATA tarballs ------------------------------------------------------
found=0
for vol in "$MNT"/var/lib/docker/volumes/*/; do
    v=$(basename "$vol")
    [ -f "$vol/_data/PG_VERSION" ] || continue
    found=$((found + 1))
    echo "## PGDATA volume $v: PG_VERSION $(cat "$vol/_data/PG_VERSION"), $(du -sh "$vol/_data" | cut -f1) on disk" >> "$INV"
    tar -C "$vol" --numeric-owner -cf - _data | gzip -1 > "$OUT/pgdata-$v.tgz"
done
[ "$found" -gt 0 ] || { echo "no Docker volume with a PG_VERSION found on the clone" | tee -a "$INV"; exit 4; }

# --- capture configuration (no bulk live-export data) ---------------------------
cfg=()
while IFS= read -r f; do cfg+=("$f"); done < <(cd "$MNT" && grep -liE 'vsc|mp50|capture' etc/systemd/system/*.service 2>/dev/null || true)
for d in home/chris/vscc-mqtt-server home/chris/VSCaptureMQTT home/chris/Downloads/VSCapture7 var/spool/cron/crontabs/chris; do
    [ -e "$MNT/$d" ] && cfg+=("$d")
done
if [ "${#cfg[@]}" -gt 0 ]; then
    tar -C "$MNT" -czf "$OUT/capture-config.tgz" \
        --exclude='*WaveExport*.csv' --exclude='MPrawoutput*' --exclude='DataExportVSC.json' \
        --exclude='sessions' --exclude='node_modules' --exclude='.venv*' --exclude='__pycache__' \
        "${cfg[@]}"
else
    echo "## no capture units or checkouts found on the clone" >> "$INV"
fi

# --- session exports (generated by the worker; PHI) -----------------------------
for s in $(cd "$MNT" && find home/chris -maxdepth 3 -type d -name sessions 2>/dev/null); do
    echo "## session export dir $s: $(du -sh "$MNT/$s" | cut -f1)" >> "$INV"
    tar -C "$MNT" -czf "$OUT/sessions-$(echo "$s" | tr '/' '_').tgz" "$s"
done

(cd "$OUT" && sha256sum -- *.tgz inventory.txt > SHA256SUMS)
echo; cat "$INV" | grep -vE '^    env ' ; echo; ls -la "$OUT"
echo "DONE. Unmounting and deactivating $VG; the clone can be detached afterwards."
