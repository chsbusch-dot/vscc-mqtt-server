#!/bin/bash
set -e

# Detect the current directory and the user running the script
APP_DIR=$(pwd)
CURRENT_USER=$USER
SERVICE_NAME="vscc-websocket-streamer.service"
PYTHON_SCRIPT="vscc-websocket-streamer.py"

echo "--- Starting VSCapture Streamer Setup ---"
echo "Directory detected: $APP_DIR"
echo "User detected: $CURRENT_USER"

# 0. Pre-flight check
if [ ! -f "$APP_DIR/$PYTHON_SCRIPT" ]; then
    echo "Error: Target script '$PYTHON_SCRIPT' not found in current directory."
    exit 1
fi

# 1. Ensure Python 3 and venv are installed on the host machine
if ! command -v python3 &> /dev/null; then
    echo "Python3 not found. Installing..."
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip
else
    echo "Python3 is installed."
    # Ensure the venv module is available (often missing on minimal Ubuntu installs)
    echo "Ensuring python3-venv is installed..."
    sudo apt-get install -y python3-venv
fi

# 2. Setup the Virtual Environment dynamically
if [ ! -d "$APP_DIR/.venv" ]; then
    echo "Creating Python virtual environment in $APP_DIR/.venv..."
    python3 -m venv "$APP_DIR/.venv"
else
    echo "Virtual environment already exists. Skipping creation..."
fi

# 3. Install dependencies if requirements.txt exists
if [ -f "$APP_DIR/requirements.txt" ]; then
    echo "Installing Python dependencies from requirements.txt..."
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
else
    echo "No requirements.txt found. Skipping dependency installation..."
fi

# Explicitly install uvicorn[standard] to ensure WebSocket support
echo "Installing uvicorn[standard]..."
"$APP_DIR/.venv/bin/pip" install "uvicorn[standard]"

# 4. Dynamically generate the systemd service file
echo "Configuring systemd service..."
cat <<EOF | sudo tee /etc/systemd/system/$SERVICE_NAME > /dev/null
[Unit]
Description=VSCapture Python Async Streamer Service
After=network.target docker.service
Wants=network-online.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$APP_DIR
Type=simple

# Execute using the local virtual environment's Python binary
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/$PYTHON_SCRIPT

StandardInput=null
StandardOutput=journal
StandardError=journal

Restart=always
RestartSec=10
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF

# 5. Reload and Start the Service
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Enabling service to start on boot..."
sudo systemctl enable $SERVICE_NAME

echo "Starting service..."
sudo systemctl restart $SERVICE_NAME

echo "--- Setup Complete ---"
echo "Service status:"
sudo systemctl status $SERVICE_NAME --no-pager