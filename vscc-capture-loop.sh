#!/bin/bash
# Keep-alive wrapper for VSCaptureCLI. Installed by install.sh as VSCapture/vscc-loop.sh
# (with @DEVICE_IP@ substituted). Runs as the ExecStart of vscc-capture-cli.service.
#
# Responsibilities:
#  - Ping the monitor before launching, so the service idles instead of failing
#    while the monitor is powered off.
#  - Recycle the capture when it stops producing data: either a UDP timeout that
#    VSCapture reports on stdout, or a SILENT hang (the MP50 still holds a dead
#    association from an abrupt stop and ignores the new association request --
#    VSCapture waits forever without printing anything).
#  - On service stop, terminate the capture and stamp the time, so the next start
#    waits out the monitor's association timeout instead of hanging.

cd "$(dirname "$0")"

DEVICE_IP="@DEVICE_IP@"
DOTNET_CMD=$(command -v dotnet || echo "/usr/bin/dotnet")
DATA_FILE="$(pwd)/DataExportVSC.json"
KILL_STAMP="$(pwd)/.vscc-abrupt-stop"
ASSOC_TIMEOUT=90   # seconds the MP50 needs to drop a dead association
NO_DATA_LIMIT=60   # recycle the capture if no data lands for this long
GRACE_PERIOD=10    # seconds to let VSCapture release the association before SIGKILL

CMD="$DOTNET_CMD VSCaptureCLI.dll --devices 1 --device1type 1 --device1model 1 --device1arg '-mode 2 -port $DEVICE_IP -interval 1 -export 4 -devid mp50  -waveset 12 -scale 2'"

PIPE_PID=""
WATCH_PID=""
STOPPING=""

terminate() {
    # Guard against re-entrancy (TERM and INT, or repeated signals).
    [ -n "$STOPPING" ] && return
    STOPPING=1
    echo "[Wrapper] Stop requested. Shutting capture down gracefully..."
    # Stamp the stop time first, so even if anything below is interrupted the next
    # start still waits out the monitor's association timeout.
    date +%s > "$KILL_STAMP"
    # Stop our own supervisors so the silent-hang watchdog can't fire mid-shutdown.
    [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null
    [ -n "$PIPE_PID" ] && kill "$PIPE_PID" 2>/dev/null
    # Ask the capture to exit so it can release the MP50 association cleanly.
    pkill -TERM -f "VSCaptureMP.dll" 2>/dev/null
    pkill -TERM -f "VSCaptureCLI.dll" 2>/dev/null
    # Wait up to GRACE_PERIOD for it to actually exit before forcing the issue.
    for _ in $(seq 1 "$GRACE_PERIOD"); do
        pgrep -f "VSCaptureCLI.dll" > /dev/null 2>&1 || break
        sleep 1
    done
    pkill -KILL -f "VSCaptureMP.dll" 2>/dev/null
    pkill -KILL -f "VSCaptureCLI.dll" 2>/dev/null
    echo "[Wrapper] Capture terminated."
    exit 0
}
trap terminate TERM INT

echo "[Wrapper] Service started (PID $$)."
echo "[Wrapper] Configuration:"
echo "  - Target Monitor: $DEVICE_IP"
echo "  - Full Command: $CMD"

# After an abrupt stop the monitor silently ignores new association requests until
# its own timeout expires. Wait out the remainder before the first launch.
if [ -f "$KILL_STAMP" ]; then
    last=$(cat "$KILL_STAMP" 2>/dev/null || echo 0)
    age=$(( $(date +%s) - last ))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$ASSOC_TIMEOUT" ]; then
        wait_s=$(( ASSOC_TIMEOUT - age ))
        echo "[Wrapper] Previous stop was ${age}s ago. Waiting ${wait_s}s for the monitor to drop the stale association..."
        sleep "$wait_s" &
        wait $!
    fi
    rm -f "$KILL_STAMP"
fi

while true; do
    if ping -c 1 -W 2 "$DEVICE_IP" > /dev/null 2>&1; then
        echo "[Wrapper] Monitor ($DEVICE_IP) is ONLINE. Launching capture process..."

        # VSCaptureCLI crashes without a TTY, so 'script' fakes a PTY. awk exits on a
        # UDP timeout message, breaking the pipe and SIGPIPE-ing VSCapture out of its
        # "Press Escape" hang.
        ( script -q -c "$CMD" /dev/null | awk '{print} /Connection timed out/{print "[Wrapper] UDP timeout detected. Forcing restart..."; fflush(); exit}' ) &
        PIPE_PID=$!

        # Silent-hang watchdog: capture alive but writing no data => stale association.
        (
            sleep "$NO_DATA_LIMIT"
            while kill -0 "$PIPE_PID" 2>/dev/null; do
                mtime=$(stat -c %Y "$DATA_FILE" 2>/dev/null || echo 0)
                if [ $(( $(date +%s) - mtime )) -gt "$NO_DATA_LIMIT" ]; then
                    echo "[Wrapper] Capture alive but no data for ${NO_DATA_LIMIT}s (stale association?). Recycling..."
                    pkill -KILL -f "VSCaptureMP.dll" 2>/dev/null
                    pkill -KILL -f "VSCaptureCLI.dll" 2>/dev/null
                    break
                fi
                sleep 10
            done
        ) &
        WATCH_PID=$!

        wait "$PIPE_PID"
        kill "$WATCH_PID" 2>/dev/null
        PIPE_PID=""
        WATCH_PID=""
        echo "[Wrapper] Capture exited. Cooling down ${ASSOC_TIMEOUT}s so the monitor can drop the association..."
        sleep "$ASSOC_TIMEOUT" &
        wait $!
    else
        echo "[Wrapper] Monitor ($DEVICE_IP) is OFFLINE. Ping failed. Waiting..."
    fi

    sleep 10 &
    wait $!
done
