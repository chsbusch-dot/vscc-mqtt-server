#!/bin/bash
set -e

# Change to the script's directory to ensure relative paths work
cd "$(dirname "$0")"

# --- Uninstall Logic ---
if [ "$1" == "-u" ]; then
    echo "--- vscc-mqtt-server Uninstaller ---"
    
    # Ensure the script is run as root/sudo for service management
    if [ "$EUID" -ne 0 ]; then
      echo "Please run the uninstaller with sudo."
      exit 1
    fi

    # 1. Stop and disable systemd services
    echo "Stopping and disabling systemd services..."
    systemctl stop vscc-capture-cli.service || echo "Service vscc-capture-cli.service was not running."
    systemctl disable vscc-capture-cli.service || echo "Service vscc-capture-cli.service was not enabled."
    systemctl stop vscc-websocket-streamer.service || echo "Service vscc-websocket-streamer.service was not running."
    systemctl disable vscc-websocket-streamer.service || echo "Service vscc-websocket-streamer.service was not enabled."

    # 2. Remove systemd service files
    echo "Removing systemd service files..."
    rm -f /etc/systemd/system/vscc-capture-cli.service
    rm -f /etc/systemd/system/vscc-websocket-streamer.service
    systemctl daemon-reload

    # 3. Remove Cron Jobs
    echo "Removing cron jobs..."
    CRON_USER=${SUDO_USER:-$(whoami)}
    if sudo -u "$CRON_USER" crontab -l 2>/dev/null | grep -q "vscc-file-cleanup.py"; then
        sudo -u "$CRON_USER" crontab -l | grep -v "vscc-file-cleanup.py" | sudo -u "$CRON_USER" crontab -
        echo "Removed file cleanup cron job."
    else
        echo "File cleanup cron job not found."
    fi
    if sudo -u "$CRON_USER" crontab -l 2>/dev/null | grep -q "update.sh"; then
        sudo -u "$CRON_USER" crontab -l | grep -v "update.sh" | sudo -u "$CRON_USER" crontab -
        echo "Removed auto-update cron job."
    else
        echo "Auto-update cron job not found."
    fi
    
    # 4. Stop and remove Docker containers, volumes, and networks
    COMPOSE_FILE="vscc-docker-compose.yml"
    if [ -f "$COMPOSE_FILE" ]; then
        echo "Stopping and removing Docker containers, networks, and all related images/volumes..."
        # The 'down' command with -v removes volumes and --rmi 'all' removes images
        docker compose -f "$COMPOSE_FILE" down -v --rmi all
    else
        echo "Docker compose file not found, skipping Docker cleanup."
    fi

    # 5. Remove local directories and virtual environments
    #echo "Removing VSCapture binaries and Python virtual environments..."
    rm -rf VSCapture
    rm -rf .venv_viz
    rm -rf .venv
    
    # 6. Remove System Packages
    echo "Removing installed system packages..."
    if command -v apt-get &> /dev/null; then
        # WARNING: Removing python3 can be risky on some systems as it is a dependency for system tools.
        # python3-pip is included here as it was installed by the streamer script.
       apt-get remove -y dotnet-runtime-8.0 mosquitto-clients
       apt-get autoremove -y
    else
        echo "apt-get not found. Skipping package removal."
    fi
    
    echo "--- Uninstallation Complete! ---"
    exit 0
fi

echo "--- vscc-mqtt-server Automated Installer (using Docker Compose V2) ---"

# --- 0. Pre-flight Checks ---
# Ensure the script is run as root/sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo."
  exit 1
fi

# Attempt to install missing dependencies if apt-get is available
if command -v apt-get &> /dev/null; then
    MISSING_DEPS=""
    if ! command -v timeout &> /dev/null; then
        MISSING_DEPS="coreutils"
    fi
    if ! command -v mosquitto_sub &> /dev/null; then
        MISSING_DEPS="$MISSING_DEPS mosquitto-clients"
    fi
    if [ -n "$MISSING_DEPS" ]; then
        echo "Installing dependencies ($MISSING_DEPS)..."
        apt-get update && apt-get install -y $MISSING_DEPS
    fi
fi

# Check for essential commands
for cmd in docker wget unzip timeout mosquitto_sub; do
  if ! command -v $cmd &> /dev/null; then
    echo "Error: Required command '$cmd' not found."
    echo "On Debian/Ubuntu, you may be able to install it with:"
    echo "sudo apt-get update && sudo apt-get install -y coreutils mosquitto-clients"
    exit 1
  fi
done

# Check for Docker Compose V2 plugin
if ! docker compose version &> /dev/null; then
  echo "Error: 'docker compose' (V2) not found."
  echo "Please ensure the Docker Compose V2 plugin is installed and accessible."
  echo "It is included with modern versions of Docker Desktop."
  exit 1
fi

# --- Get User Input for Device IP ---
read -p "Enter the IP address of the Philips Patient Monitor [192.168.1.215]: " DEVICE_IP
DEVICE_IP=${DEVICE_IP:-"192.168.1.215"}

# Validate the IP address format
while ! [[ "$DEVICE_IP" =~ ^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$ ]]; do
    echo "Invalid IP address format. Please try again."
    read -p "Enter the IP address of the Philips Patient Monitor [192.168.1.215]: " DEVICE_IP
    DEVICE_IP=${DEVICE_IP:-"192.168.1.215"}
done
echo "Using monitor IP: $DEVICE_IP"

# --- 1. Download and Unpack VSCaptureCLI Binaries ---
DOWNLOAD_DIR="VSCapture"
ZIP_FILE="VSCaptureCLIv1.007Binary.zip"
DLL_PATH="$DOWNLOAD_DIR/VSCaptureCLI.dll"
URL="https://sourceforge.net/projects/vscapture/files/VSCaptureCLI/VSCaptureCLIv1.007Binary.zip/download"

if [ -f "$DLL_PATH" ]; then
    echo "VSCaptureCLI already exists. Skipping download."
else
    echo "Downloading VSCaptureCLI..."
    wget -q --show-progress "$URL" -O "$ZIP_FILE"
    
    echo "Extracting binaries..."
    mkdir -p "$DOWNLOAD_DIR"
    unzip -o "$ZIP_FILE" -d "$DOWNLOAD_DIR/"
    
    echo "Cleaning up..."
    rm "$ZIP_FILE"
fi
echo "VSCaptureCLI is ready."

# --- 2. Clean and Start Docker Backend Services ---
COMPOSE_FILE="vscc-docker-compose.yml"

echo "Ensuring no old containers are running..."
# The 'down' command with -v removes volumes, ensuring a clean slate.
docker compose -f "$COMPOSE_FILE" down -v

echo "Starting backend services with Docker Compose..."
echo "This may take a few minutes on the first run to build the worker image..."
docker compose -f "$COMPOSE_FILE" up -d --build

echo "Waiting for services to initialize..."
sleep 15 # Give TimescaleDB and vscc-emqx time to start up properly

echo "Docker services started."
docker compose -f "$COMPOSE_FILE" ps

# --- 3. Make Scripts Executable ---
echo "Setting permissions for installation scripts..."
chmod +x vscc-install-capture-cli.sh
chmod +x vscc-install-websocket-streamer.sh
chmod +x update.sh

# --- 4. Install Systemd Services ---
echo "Installing VSCapture CLI service..."
./vscc-install-capture-cli.sh "$DEVICE_IP"

# --- PATCH: Install the Keep-Alive Wrapper for VSCapture ---
# This prevents the service from entering a 'failed' state if the monitor is offline
# or if the connection drops, recycles silent hangs from stale monitor associations,
# and stamps abrupt stops so the next start waits out the association timeout.
# The wrapper itself lives in the repo as vscc-capture-loop.sh.

WRAPPER_SCRIPT="$(pwd)/VSCapture/vscc-loop.sh"
SERVICE_FILE="/etc/systemd/system/vscc-capture-cli.service"

echo "Installing keep-alive wrapper at $WRAPPER_SCRIPT..."
sed "s/@DEVICE_IP@/$DEVICE_IP/g" vscc-capture-loop.sh > "$WRAPPER_SCRIPT"
chmod +x "$WRAPPER_SCRIPT"

# Patch the systemd service to use the wrapper instead of direct dotnet call
if [ -f "$SERVICE_FILE" ]; then
    echo "Patching systemd service to use keep-alive wrapper..."
    # Fully replace the ExecStart line to ensure valid syntax and prevent trailing garbage
    sed -i "s|^ExecStart=.*|ExecStart=$WRAPPER_SCRIPT|" "$SERVICE_FILE"

    # Remove the sleep delay to speed up startup (wrapper handles waiting now)
    echo "Optimizing service startup time..."
    sed -i "/^ExecStartPre/d" "$SERVICE_FILE"

    # Graceful shutdown: TERM goes to the wrapper only (KillMode=mixed), which kills
    # the capture and stamps the stop time; stragglers get SIGKILL after 20s.
    echo "Configuring graceful shutdown..."
    sed -i "/^TimeoutStopSec=/d;/^KillMode=/d" "$SERVICE_FILE"
    sed -i "s/^KillSignal=.*/KillSignal=SIGTERM\nTimeoutStopSec=20\nKillMode=mixed/" "$SERVICE_FILE"

    systemctl daemon-reload
    systemctl restart vscc-capture-cli.service
fi

echo "Installing WebSocket streamer service..."
./vscc-install-websocket-streamer.sh

# --- 5. Install Cron Jobs for Maintenance ---
echo "Installing cron jobs for automated cleanup and updates..."
# Use SUDO_USER to ensure cron jobs are installed for the original user, not root
CRON_USER=${SUDO_USER:-$(whoami)}
APP_PATH=$(pwd)
CLEANUP_SCRIPT_PATH="$APP_PATH/vscc-file-cleanup.py"
UPDATE_SCRIPT_PATH="$APP_PATH/update.sh"

# Job 1: File Cleanup (every hour)
CLEANUP_JOB="0 * * * * /usr/bin/python3 $CLEANUP_SCRIPT_PATH"
if ! (sudo -u "$CRON_USER" crontab -l 2>/dev/null | grep -Fq "$CLEANUP_SCRIPT_PATH"); then
    (sudo -u "$CRON_USER" crontab -l 2>/dev/null; echo "$CLEANUP_JOB") | sudo -u "$CRON_USER" crontab -
    echo "File cleanup cron job installed to run hourly."
else
    echo "File cleanup cron job already exists. Skipping."
fi

# Job 2: Auto-Update (every 3 months)
UPDATE_JOB="0 0 1 */3 * $UPDATE_SCRIPT_PATH"
if ! (sudo -u "$CRON_USER" crontab -l 2>/dev/null | grep -Fq "$UPDATE_SCRIPT_PATH"); then
    (sudo -u "$CRON_USER" crontab -l 2>/dev/null; echo "$UPDATE_JOB") | sudo -u "$CRON_USER" crontab -
    echo "Auto-update cron job installed to run quarterly."
else
    echo "Auto-update cron job already exists. Skipping."
fi


# --- 6. Final Status & Verification ---
echo ""
echo "--- Installation Complete! ---"
echo ""
echo "Summary of services:"
echo "----------------------"
echo "Docker Containers:"
docker compose -f "$COMPOSE_FILE" ps
echo ""
echo "Systemd Services:"
systemctl status vscc-capture-cli.service --no-pager
systemctl status vscc-websocket-streamer.service --no-pager
echo ""
echo "Verifying MQTT message reception from MP50..."
echo "(Listening for 5 seconds...)"
if timeout 5 mosquitto_sub -h 127.0.0.1 -t telemetry/mp50 -C 1 -v; then
    echo "MQTT verification successful. Messages are being received."
else
    echo "Warning: No MQTT messages were received in the 5-second window."
    echo "This could be a configuration issue, a network problem, or the device may not be sending data yet."
fi
echo ""
echo "Next Steps:"
echo " - To view live data capture logs, run: journalctl -u vscc-capture-cli.service -f"
echo " - To view live worker logs, run: sudo docker compose -f $COMPOSE_FILE logs -f worker"
echo " - To view the frontend, launch the visualizer or the React Dashboard."
