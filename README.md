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
├── vscc-rawdata-spotcheck.py # Diagnostic: detect real vitals vs flatline/noise in exports
├── vscc-rawdata-format-probe.py # Diagnostic: identify the format of JSON / raw export files
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

## Raw Data Spot-Check

`VSCaptureCLI` produces multi-GB waveform CSVs (ECG, EEG, PLETH, RESP) plus a
numerics JSON. To tell whether an export holds a **real vital-sign trace** or is
just a **flatline / noise** — without reading the whole file — use
`vscc-rawdata-spotcheck.py`. It never loads more than a few MB into memory.

It targets the same files as `vscc-file-cleanup.py` and defaults to the
`VSCapture/` capture dir (override with `VSC_CAPTURE_DIR`).

Two modes:

-   **Fast sampler (default):** seeks to ~20 evenly-spaced offsets, reads a small
    window at each, and reports per-window stats (min/max/std, distinct values,
    oscillation) with a `SIGNAL` / `NEAR-FLAT` / `FLAT` / `NOISE?` label.
    Sub-second even on a 2 GB file. A trace that only toggles between 1–2
    adjacent quantization levels is flagged `NEAR-FLAT`, not signal.
-   **Streaming profile (`--profile`):** one constant-memory pass reporting the
    distinct-value count per segment. Slower, but **cannot miss intermittent
    signal** — the fast sampler can skip a short live stretch between its probe
    points.

```bash
# fast triage of all known exports in the capture dir
python3 vscc-rawdata-spotcheck.py

# check specific files
python3 vscc-rawdata-spotcheck.py /path/to/NOM_PLETHWaveExport.csv

# definitive scan when you need certainty (catches sparse signal)
python3 vscc-rawdata-spotcheck.py --profile /path/to/NOM_PLETHWaveExport.csv
```

## Raw Data Format Probe

Where the spot-check answers *"is there live data?"*, `vscc-rawdata-format-probe.py`
answers *"what IS this file?"* — handy when deciding how to parse an export or feed
it into a pipeline. Same capture-dir convention (`VSC_CAPTURE_DIR`).

-   **`DataExportVSC.json`** — reports the framing (it's **line-delimited JSON
    arrays / NDJSON with a UTF-8 BOM**, *not* one JSON document, so a plain
    `json.load()` fails), record count, schema, batch sizes, DeviceIDs, distinct
    PhysioIDs, and the captured time span.
-   **`MPrawoutput.txt`** — the raw **Philips IntelliVue / IEEE-11073 Data Export**
    stream as dash-separated hex. Reports BOM, frame (line) count, frame-size
    distribution (waveform sample-array frames vs. numerics/keepalive), total
    decoded protocol bytes, and the IEEE/Philips OID arc. The CSV and JSON exports
    are decoded from this single stream.

```bash
python3 vscc-rawdata-format-probe.py                 # known files in capture dir
python3 vscc-rawdata-format-probe.py /path/to/DataExportVSC.json /path/to/MPrawoutput.txt
```

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
