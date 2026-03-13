#!/bin/bash

# Check for IP address argument
if [ -z "$1" ]; then
  echo "Error: No device IP address provided. Please run the main installer script."
  exit 1
fi
DEVICE_IP=$1

# Detect the current directory and the user running the script
APP_DIR=$(pwd)
DLL_DIR="$APP_DIR/VSCapture"
CURRENT_USER=$USER

echo "--- Starting VSCapture Setup ---"
echo "Directory detected: $DLL_DIR"
echo "User detected: $CURRENT_USER"
echo "Device IP: $DEVICE_IP"

# 1. Install .NET 8 Runtime if not already installed
if ! command -v dotnet &> /dev/null; then
    echo "dotnet not found. Installing .NET 8 Runtime..."
    sudo apt-get update
    sudo apt-get install -y dotnet-runtime-8.0
else
    echo ".NET is already installed. Skipping..."
fi

# 2. Dynamically generate the systemd service file
echo "Configuring systemd service..."
cat <<EOF | sudo tee /etc/systemd/system/vscc-capture-cli.service > /dev/null
[Unit]
Description=VSCapture CLI Service
After=network.target
Wants=network-online.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$DLL_DIR
Type=idle
ExecStartPre=/bin/sleep 15
ExecStart=/usr/bin/script -q /dev/null -c "/usr/bin/dotnet $DLL_DIR/VSCaptureCLI.dll --devices 1 --device1type 1 --device1model 1 --device1arg '-mode 2 -port $DEVICE_IP -interval 1 -export 4 -devid mp50 -waveset 12 -scale 2'"

StandardInput=null
StandardOutput=journal
StandardError=journal

Restart=always
RestartSec=10
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF

# 3. Reload systemd and start the service
echo "Starting VSCapture service..."
sudo systemctl daemon-reload
sudo systemctl enable vscc-capture-cli.service
sudo systemctl restart vscc-capture-cli.service

echo "--- Installation complete! ---"
echo "To check the live logs, run: journalctl -u vscc-capture-cli.service -f"
echo "To stop the service, run: sudo systemctl stop vscc-capture-cli.service"
echo "To check the service status, run: sudo systemctl status vscc-capture-cli.service"
echo "To restart the service, run: sudo systemctl restart vscc-capture-cli.service"
echo "To remove the service, run: sudo systemctl disable vscc-capture-cli.service"