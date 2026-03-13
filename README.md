# vscc-mqtt-server: Backend Infrastructure for Medical Telemetry

This directory contains the complete backend infrastructure for capturing, storing, and streaming data from a Philips MP50 patient monitor. It uses Docker and `systemd` to create a robust, manageable system.

## System Architecture

The system is composed of several key services that work together:

1.  **VSCapture CLI (`systemd` service):** A .NET application that connects directly to the patient monitor, captures raw telemetry data, and forwards it to the MQTT broker.
2.  **EMQX (`docker` container):** A high-performance MQTT broker that receives data from VSCapture and makes it available to subscribers.
3.  **TimescaleDB (`docker` container):** A PostgreSQL database with the TimescaleDB extension, optimized for storing time-series data like medical telemetry.
4.  **Python Worker (`docker` container):** A Python script that subscribes to the MQTT broker, processes the incoming data, and inserts it into the TimescaleDB database.
5.  **WebSocket Streamer (`systemd` service):** A Python script that provides a real-time stream of the data via WebSockets for client applications.

## Directory Structure

A brief overview of the key files and directories:

```
vscc-mqtt-server/
├── VSCapture/              # Contains the downloaded VSCaptureCLI binaries
├── .venv_viz/              # Python virtual environment for local development
├── install.sh              # The main installation and uninstallation script
├── update.sh               # Script to update components, run by a cron job
├── vscc-docker-compose.yml # Defines the Docker-based services (DB, MQTT, Worker)
├── vscc-worker.Dockerfile  # Dockerfile for building the Python worker image
├── .dockerignore           # Optimizes the Docker build by excluding unnecessary files
├── vscc_mqtt_timescale_worker.py # Python script for the worker service
├── vscc-file-cleanup.py    # Utility script to manage log file sizes
└── README.md               # This file
```

## Prerequisites

Before you begin, ensure you have the following software installed on your Linux system (instructions are based on Debian/Ubuntu):

-   **Docker & Docker Compose V2:** Modern versions of Docker Desktop include Compose. If on Linux Server, follow the official Docker installation guide.
-   **Essential Tools:** `sudo apt-get install wget unzip git coreutils mosquitto-clients`
-   **.NET 8.0 Runtime:** The installation scripts will attempt to install this for you.
-   **Python 3 & venv:** `sudo apt-get install python3 python3-venv`

## Automated Installation

An automated script `install.sh` is provided to simplify the setup.

1.  Make the script executable: `chmod +x install.sh`
2.  Run the script with `sudo` privileges: `sudo ./install.sh`
3.  When prompted, **enter the IP address of your Philips Patient Monitor**. The script validates the format. If you press Enter without an entry, it defaults to `192.168.1.215`.

The script will perform the following actions:
- Download the VSCapture binaries.
- Build and start the Docker services (TimescaleDB, EMQX, Worker).
- Install and start the `systemd` services for data capture and streaming.
- **Install cron jobs** for automated maintenance:
    - A job to run the `vscc-file-cleanup.py` script **every hour** to prevent data files from growing too large.
    - A job to run the `update.sh` script **every 3 months** to check for updates.

## Automated Maintenance

The installer automatically sets up cron jobs to keep the system running smoothly.

-   **Hourly File Cleanup:** The `VSCaptureCLI` tool generates several large data files. To prevent these from consuming excessive disk space, the `vscc-file-cleanup.py` script runs every hour and truncates any data file that grows beyond 20MB.
-   **Quarterly Updates:** The `update.sh` script runs every three months to pull the latest changes for the project components.

## Uninstallation

To **completely remove** all components installed by the script, run the installer with the `-u` flag. This is a destructive operation.

```bash
sudo ./install.sh -u
```

This will perform a full cleanup:

1.  **Stop and Disable Systemd Services:** Stops and disables `vscc-capture-cli.service` and `vscc-websocket-streamer.service`.
2.  **Remove Systemd Service Files:** Deletes the service definition files.
3.  **Remove Cron Jobs:** Deletes the hourly cleanup and quarterly update cron jobs.
4.  **Stop and Purge Docker Stack:** Stops all Docker containers, removes their volumes ( **deleting all database data**), and removes the downloaded container images.
5.  **Delete Local Directories:** Removes the `VSCapture` and `.venv_viz` directories.

After running, your system will be cleared of the services, data, and configurations created by the installer.
