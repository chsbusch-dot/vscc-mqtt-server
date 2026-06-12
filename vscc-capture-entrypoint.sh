#!/bin/bash
# Container entrypoint for the VSCapture capture service (LAN mode).
# Environment:
#   MONITOR_IP  - IP of the Philips IntelliVue monitor (required)
#   MONITOR_TZ  - informational here; timestamp conversion happens in the worker
#   WAVESET     - VSCapture waveform set (default 12 = all)
#   INTERVAL    - numerics export interval in seconds (default 1)
#   SCALE       - VSCapture scale option (default 2)
#   DEVID       - device id used in file names / MQTT topics (default mp50)
#
# The env values are bootstrap defaults. The dashboard's capture settings are
# written by the worker to $CONFIG_FILE (key=value) on the shared volume; the
# file is re-read before every launch and a watcher recycles the capture when
# it changes, so settings apply without recreating the container.
#
# Same lifecycle rules as the host wrapper (vscc-capture-loop.sh):
#  - ping before launching, so we idle while the monitor is off
#  - recycle on UDP timeout (awk pipe) or silent hang (no-data watchdog)
#  - stamp abrupt stops and wait out the monitor's association timeout
# Plus the hourly export-file cleanup that cron does on a host install.

set -u
DEVICE_IP="${MONITOR_IP:?Set MONITOR_IP to the monitor IP, e.g. MONITOR_IP=192.168.1.215}"
WAVESET="${WAVESET:-12}"
INTERVAL="${INTERVAL:-1}"
SCALE="${SCALE:-2}"
DEVID="${DEVID:-mp50}"
DATA_DIR=/data
BIN_DIR=/opt/vscapture
CONFIG_FILE="$DATA_DIR/vscc-capture-config.conf"
KILL_STAMP="$DATA_DIR/.vscc-abrupt-stop"
ASSOC_TIMEOUT=90
NO_DATA_LIMIT=60
DATA_FILE="$DATA_DIR/DataExportVSC.json"

# VSCapture writes its exports next to its working set; keep binaries and data
# together on the volume, mirroring the host install's VSCapture/ directory.
cp -un "$BIN_DIR"/* "$DATA_DIR"/ 2>/dev/null || true
cd "$DATA_DIR"

cfg_get() { sed -n "s/^$1=//p" "$CONFIG_FILE" 2>/dev/null | head -1 | tr -d '[:space:]'; }

read_config() {
    # Effective settings: env defaults overridden by the dashboard-managed
    # config file. Values are regex-checked here too — the file lives on a
    # shared volume and could be edited by hand.
    CFG_IP="$DEVICE_IP" CFG_INTERVAL="$INTERVAL" CFG_WAVESET="$WAVESET" CFG_SCALE="$SCALE" CFG_DEVID="$DEVID"
    [ -f "$CONFIG_FILE" ] || return 0
    local v
    v=$(cfg_get monitor_ip); echo "$v" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$' && CFG_IP="$v"
    v=$(cfg_get interval);   echo "$v" | grep -qE '^(1|10|60|300)$' && CFG_INTERVAL="$v"
    v=$(cfg_get waveset);    echo "$v" | grep -qE '^([0-9]|1[0-2])$' && CFG_WAVESET="$v"
    v=$(cfg_get scale);      echo "$v" | grep -qE '^[12]$' && CFG_SCALE="$v"
    v=$(cfg_get devid);      echo "$v" | grep -qE '^[A-Za-z0-9_-]{1,32}$' && CFG_DEVID="$v"
}

terminate() {
    echo "[Capture] Stop requested. Terminating capture process..."
    date +%s > "$KILL_STAMP"
    pkill -TERM -f "VSCaptureMP.dll" 2>/dev/null
    pkill -TERM -f "VSCaptureCLI.dll" 2>/dev/null
    sleep 2
    pkill -KILL -f "VSCaptureMP.dll" 2>/dev/null
    pkill -KILL -f "VSCaptureCLI.dll" 2>/dev/null
    exit 0
}
trap terminate TERM INT

echo "[Capture] Container started. Monitor: $DEVICE_IP, waveset: $WAVESET"

# Hourly export-file cleanup (replaces the host cron job)
(
    while true; do
        sleep 3600
        python3 /opt/vscc-file-cleanup.py || true
    done
) &

if [ -f "$KILL_STAMP" ]; then
    last=$(cat "$KILL_STAMP" 2>/dev/null || echo 0)
    age=$(( $(date +%s) - last ))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$ASSOC_TIMEOUT" ]; then
        wait_s=$(( ASSOC_TIMEOUT - age ))
        echo "[Capture] Previous stop was ${age}s ago. Waiting ${wait_s}s for the monitor to drop the stale association..."
        sleep "$wait_s" &
        wait $!
    fi
    rm -f "$KILL_STAMP"
fi

while true; do
    read_config
    CONFIG_MTIME=$(stat -c %Y "$CONFIG_FILE" 2>/dev/null || echo 0)
    if ping -c 1 -W 2 "$CFG_IP" > /dev/null 2>&1; then
        echo "[Capture] Monitor ($CFG_IP) is ONLINE. Launching capture (interval ${CFG_INTERVAL}s, waveset $CFG_WAVESET, scale $CFG_SCALE, devid $CFG_DEVID)..."

        CMD="dotnet VSCaptureCLI.dll --devices 1 --device1type 1 --device1model 1 --device1arg '-mode 2 -port $CFG_IP -interval $CFG_INTERVAL -export 4 -devid $CFG_DEVID  -waveset $CFG_WAVESET -scale $CFG_SCALE'"

        ( script -q -c "$CMD" /dev/null | awk '{print} /Connection timed out/{print "[Capture] UDP timeout detected. Forcing restart..."; fflush(); exit}' ) &
        PIPE_PID=$!

        (
            sleep "$NO_DATA_LIMIT"
            while kill -0 "$PIPE_PID" 2>/dev/null; do
                mtime=$(stat -c %Y "$DATA_FILE" 2>/dev/null || echo 0)
                if [ $(( $(date +%s) - mtime )) -gt "$NO_DATA_LIMIT" ]; then
                    echo "[Capture] Capture alive but no data for ${NO_DATA_LIMIT}s (stale association?). Recycling..."
                    pkill -KILL -f "VSCaptureMP.dll" 2>/dev/null
                    pkill -KILL -f "VSCaptureCLI.dll" 2>/dev/null
                    break
                fi
                sleep 10
            done
        ) &
        WATCH_PID=$!

        # Config watcher: recycle the capture when the dashboard applies new
        # settings (the main loop re-reads the file and cools down as usual).
        (
            while kill -0 "$PIPE_PID" 2>/dev/null; do
                cur=$(stat -c %Y "$CONFIG_FILE" 2>/dev/null || echo 0)
                if [ "$cur" != "$CONFIG_MTIME" ]; then
                    echo "[Capture] Capture settings changed. Recycling capture process..."
                    pkill -KILL -f "VSCaptureMP.dll" 2>/dev/null
                    pkill -KILL -f "VSCaptureCLI.dll" 2>/dev/null
                    break
                fi
                sleep 10
            done
        ) &
        CFGWATCH_PID=$!

        wait "$PIPE_PID"
        kill "$WATCH_PID" "$CFGWATCH_PID" 2>/dev/null
        echo "[Capture] Capture exited. Cooling down ${ASSOC_TIMEOUT}s so the monitor can drop the association..."
        sleep "$ASSOC_TIMEOUT" &
        wait $!
    else
        echo "[Capture] Monitor ($CFG_IP) is OFFLINE. Ping failed. Waiting..."
    fi

    sleep 10 &
    wait $!
done
