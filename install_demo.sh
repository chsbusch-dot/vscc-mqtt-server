#!/bin/bash
# vscc DEMO installer: the full stack with NO patient monitor — a virtual MP50
# replays a de-identified slice so you can try VSCC on any machine with Docker.
#
#   wget -qO- https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/install_demo.sh | bash
#
# Research and education use only — not a medical device.
set -eu

COMPOSE_URL="https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/docker-compose.demo.yml"
INSTALL_DIR="$HOME/vscc-demo"

say() { echo "[vscc-demo] $*"; }

# --- Docker ---
if ! command -v docker >/dev/null 2>&1; then
    say "Docker is required: https://docs.docker.com/get-docker/"; exit 1
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
if ! $DOCKER compose version >/dev/null 2>&1; then
    say "Docker Compose V2 is required: https://docs.docker.com/compose/install/"; exit 1
fi

# --- Install ---
mkdir -p "$INSTALL_DIR" && cd "$INSTALL_DIR"
say "Downloading $INSTALL_DIR/docker-compose.demo.yml"
if command -v wget >/dev/null 2>&1; then
    wget -qO docker-compose.demo.yml "$COMPOSE_URL"
else
    curl -fsSL -o docker-compose.demo.yml "$COMPOSE_URL"
fi

say "Starting the demo stack (no monitor needed)..."
$DOCKER compose -f docker-compose.demo.yml up -d

HOST_IP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|169\.254\.|172\.1[7-9]\.|172\.2[0-9]\.|172\.3[01]\.)' | head -1)
echo
say "Demo is up. Open http://${HOST_IP:-<this-host>}/ and press PLAY LIVE."
say "Charts begin within a few seconds as the virtual monitor replays."
say "Stop: cd $INSTALL_DIR && $DOCKER compose -f docker-compose.demo.yml down"
