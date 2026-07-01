#!/bin/bash
# vscc dashboard installer for a separate frontend host. Asks for the backend's
# IP and runs the prebuilt dashboard image pointed at it.
#
#   wget -qO- https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/install_frontend.sh | bash
#
# Non-interactive: VSCC_HOST=<backend-ip> [PORT=80] ... | bash
#
# Research and education use only — not a medical device.
set -eu

IMAGE="ghcr.io/chsbusch-dot/vscc-dashboard:latest"
PORT="${PORT:-80}"
IP_RE='^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'

say() { echo "[vscc] $*"; }

ask() {
    local reply=""
    if [ -t 0 ]; then
        read -r -p "$1" reply || true
    elif [ -e /dev/tty ]; then
        read -r -p "$1" reply < /dev/tty || true
    fi
    echo "${reply:-$2}"
}

# --- Docker ---
if ! command -v docker >/dev/null 2>&1; then
    say "Docker is not installed."
    if command -v apt-get >/dev/null 2>&1; then
        yn=$(ask "Install Docker now via apt-get? [y/N] " "n")
        case "$yn" in
            [Yy]*) sudo apt-get update && sudo apt-get install -y docker.io ;;
            *) say "Aborting — install Docker first: https://docs.docker.com/get-docker/"; exit 1 ;;
        esac
    else
        say "Install Docker first: https://docs.docker.com/get-docker/"; exit 1
    fi
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# --- Backend IP ---
VSCC_HOST="${VSCC_HOST:-}"
tries=0
while ! [[ "$VSCC_HOST" =~ $IP_RE ]]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 3 ]; then
        say "No valid backend IP. Re-run with VSCC_HOST=<backend-ip> preset."
        exit 1
    fi
    VSCC_HOST=$(ask "IP of the vscc backend host: " "")
done

# --- Reachability hint (non-fatal) ---
probe() {
    if command -v wget >/dev/null 2>&1; then
        wget -qO- --timeout=4 "http://$VSCC_HOST:8001/api/status" >/dev/null 2>&1
    elif command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 4 "http://$VSCC_HOST:8001/api/status" >/dev/null 2>&1
    else
        return 2   # no probe tool available — UNKNOWN, not "reachable"
    fi
}
probe; probe_rc=$?
if [ "$probe_rc" -eq 0 ]; then
    say "Backend reachable at $VSCC_HOST — good."
elif [ "$probe_rc" -eq 2 ]; then
    say "Skipping backend reachability check (no wget/curl available)."
else
    say "WARNING: could not reach the backend API at http://$VSCC_HOST:8001 —"
    say "continuing anyway, but check the backend is up and the IP is right."
fi

# --- Run ---
$DOCKER rm -f vscc-dashboard >/dev/null 2>&1 || true
say "Starting dashboard (image: $IMAGE)..."
$DOCKER run -d --name vscc-dashboard -p "$PORT:80" -e VSCC_HOST="$VSCC_HOST" \
    --restart unless-stopped "$IMAGE" >/dev/null

# Prefer a real LAN IP: drop loopback, link-local, and Docker bridge ranges
# (172.17–172.31) so we don't advertise the docker0 address to users.
HOST_IP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|169\.254\.|172\.1[7-9]\.|172\.2[0-9]\.|172\.3[01]\.)' | head -1)
echo
say "Dashboard is up: http://${HOST_IP:-<this-host>}$( [ "$PORT" = "80" ] || echo ":$PORT" )/"
say "It streams from the backend at $VSCC_HOST (MQTT :8083, streamer :8000, API :8001)."
