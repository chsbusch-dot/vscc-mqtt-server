#!/bin/bash
set -e

# Change to the script's directory to ensure relative paths work
cd "$(dirname "$0")"

echo "--- vscc-mqtt-server Dependency Updater ---"

# --- 0. Pre-flight Checks ---
# Ensure the script is run as root/sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo."
  exit 1
fi

# Check for Docker Compose V2 plugin
if ! docker compose version &> /dev/null; then
  echo "Error: 'docker compose' (V2) not found."
  echo "Please ensure the Docker Compose V2 plugin is installed."
  exit 1
fi

COMPOSE_FILE="vscc-docker-compose.yml"

# --- 1. Pull Latest Docker Images ---
echo "Pulling the latest images for all backend services (EMQX, TimescaleDB, Worker)..."
echo "This may take a few moments."
docker compose -f "$COMPOSE_FILE" pull

# --- 2. Recreate and Restart Services ---
echo "Stopping and recreating containers with the new images..."
# The 'up' command with -d and --remove-orphans is a safe way to apply changes.
# It will only recreate containers whose images have changed.
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

echo ""
echo "--- Update Complete! ---"
echo ""
echo "Summary of running services:"
echo "----------------------------"
docker compose -f "$COMPOSE_FILE" ps
