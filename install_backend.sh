#!/bin/bash
# vscc backend installer: broker + database + capture + worker + streamer,
# optionally the dashboard too (single-host install).
#
#   wget -qO- https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/install_backend.sh | bash
#
# Non-interactive: preset env vars to skip prompts:
#   MONITOR_IP=192.168.1.215 WITH_DASHBOARD=yes ... | bash
#
# Research and education use only — not a medical device.
set -eu

COMPOSE_URL="https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/docker-compose.yml"
INSTALL_DIR="$HOME/vscc"
IP_RE='^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'

say() { echo "[vscc] $*"; }

# Prompt that also works when the script is piped into bash (reads /dev/tty).
ask() { # ask <prompt> <default> -> echoes the answer
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
            [Yy]*) sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 ;;
            *) say "Aborting — install Docker first: https://docs.docker.com/get-docker/"; exit 1 ;;
        esac
    else
        say "Install Docker first: https://docs.docker.com/get-docker/"; exit 1
    fi
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
if ! $DOCKER compose version >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing Docker Compose V2 plugin..."
        sudo apt-get update && sudo apt-get install -y docker-compose-v2
    else
        say "Docker Compose V2 is required: https://docs.docker.com/compose/install/"; exit 1
    fi
fi

# --- Monitor IP ---
MONITOR_IP="${MONITOR_IP:-}"
tries=0
while ! [[ "$MONITOR_IP" =~ $IP_RE ]]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 3 ]; then
        say "No valid monitor IP. Re-run with MONITOR_IP=<ip> preset, e.g.:"
        say "  MONITOR_IP=192.168.1.215 $0"
        exit 1
    fi
    MONITOR_IP=$(ask "IP of the Philips monitor [192.168.1.215]: " "192.168.1.215")
done

# --- Dashboard on this host too? ---
WITH_DASHBOARD="${WITH_DASHBOARD:-}"
if [ -z "$WITH_DASHBOARD" ]; then
    WITH_DASHBOARD=$(ask "Also serve the dashboard on this host? [Y/n] " "Y")
fi

# --- Install ---
mkdir -p "$INSTALL_DIR" && cd "$INSTALL_DIR"
say "Downloading compose file to $INSTALL_DIR/docker-compose.yml"
if command -v wget >/dev/null 2>&1; then
    wget -qO docker-compose.yml "$COMPOSE_URL"
else
    curl -fsSL -o docker-compose.yml "$COMPOSE_URL"
fi

# Compose reads MONITOR_IP from .env — survives sudo's env reset and means
# future manual `docker compose` commands in this directory just work.
printf 'MONITOR_IP=%s\n' "$MONITOR_IP" > .env
say "Wrote $INSTALL_DIR/.env"

SERVICES="emqx timescaledb capture worker streamer"
case "$WITH_DASHBOARD" in [Yy]*) SERVICES="$SERVICES dashboard" ;; esac

say "Starting: $SERVICES"
$DOCKER compose up -d $SERVICES

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
say "Backend is up. Monitor: $MONITOR_IP (capture starts automatically when it is on)."
case "$WITH_DASHBOARD" in
    [Yy]*) say "Dashboard: http://${HOST_IP:-<this-host>}/" ;;
    *)     say "Dashboard host setup: run install_frontend.sh on the other machine"
           say "and enter this backend's IP when asked: ${HOST_IP:-<this-host>}" ;;
esac
say "Status: cd $INSTALL_DIR && $DOCKER compose ps"
