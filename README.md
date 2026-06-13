# vscc-mqtt-server — the VSCC backend

**VSCC** (*VitalSignsCapture + Charts*) is an open patient-monitor telemetry system:
this repo is the capture/broker/database backend; **VSCC Studio**
([vscc-dashboard-client](https://github.com/chsbusch-dot/vscc-dashboard-client)) is the
web frontend.

This is the **backend**: complete infrastructure for capturing, storing, and streaming
data from a Philips MP50 patient monitor — Docker + `systemd`, built around
[VSCapture](https://sourceforge.net/projects/vscapture/files/), the open-source
patient-monitor capture tool by John George K (the installer downloads the latest
VSCapture automatically). It pairs with the
[vscc-dashboard-client](https://github.com/chsbusch-dot/vscc-dashboard-client)
frontend — a React + SciChart dashboard rendering the live waveforms and vitals at
60 FPS, connecting via MQTT-over-WebSocket (port 8083) or the streamer (port 8000).

> [!WARNING]
> **Research and education use only — not a medical device.**
> This software is not cleared or approved for clinical use and must never be used
> for patient monitoring, alarming, or any clinical decision-making. A certified
> monitor remains the source of truth at all times.
>
> **Patient data stays local.** Captured telemetry may constitute Protected Health
> Information (PHI). Never publish it to brokers, dashboards, or endpoints outside
> your controlled network, and never write physiological values into logs, browser
> consoles, or third-party telemetry/analytics services. De-identify any recording
> before sharing it.

[![MP50 Vital Sign Dashboard](https://raw.githubusercontent.com/chsbusch-dot/vscc-dashboard-client/main/docs/screenshots/dashboard-full.png)](https://github.com/chsbusch-dot/vscc-dashboard-client)

- **High-frequency waveforms** — **Pleth** and **Respiration** rendered as continuous traces, plus **ECG**, **EEG**, and **BIS** channels (any new monitor module's waveform export is auto-discovered and published — no code change)
- **Numeric vitals** — SpO₂, pulse rate, NIBP, respiration rate, heart rate, and every other numeric the monitor exports

## Quick Start

All you need is Docker — on Linux, macOS (Docker Desktop), Windows (Docker
Desktop or WSL2), or a Raspberry Pi. The installer prompts for your monitor's
IP, asks whether to serve the dashboard on the same host, and starts everything
from prebuilt images (it offers to install Docker on Debian/Ubuntu if missing):

```bash
wget -qO- https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/install_backend.sh | bash
```

Then open `http://<this-host>/` in a browser and press PLAY LIVE. The capture
waits politely while the monitor is off and starts streaming within seconds of
it being powered on. (LAN capture mode only; serial/MIB needs a native install.)

### Try it without hardware (demo mode)

No monitor? A **virtual MP50** replays a de-identified recorded slice through
the real pipeline, so you get live waveforms (ECG, EEG, Pleth, Resp) and
numerics with nothing plugged in — handy for a first look or a forum demo:

```bash
wget -qO- https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/install_demo.sh | bash
# or: curl -O https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/docker-compose.demo.yml
#     docker compose -f docker-compose.demo.yml up -d
```

Open `http://<this-host>/`, press PLAY LIVE, and charts begin within seconds.
No `MONITOR_IP` needed; `capture` is swapped for the replayer.

### Two-host install (backend and dashboard on separate machines)

Run the backend installer on host A (answer "n" to the dashboard question),
then on host B:

```bash
wget -qO- https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/install_frontend.sh | bash
```

It asks for host A's IP, checks it can reach the backend, and serves the
dashboard at `http://<host-B>/`. Ports 8083, 8000, and 8001 on host A must be
reachable from viewers' browsers. Tip: give both machines DHCP reservations in
your router so the IPs never change.

Non-interactive / automation: preset the answers as env vars —
`MONITOR_IP=… WITH_DASHBOARD=yes|no` (backend), `VSCC_HOST=… PORT=…` (frontend).

<details>
<summary>What the installers run under the hood (manual alternative)</summary>

```bash
# Single host, everything:
wget https://raw.githubusercontent.com/chsbusch-dot/vscc-mqtt-server/main/docker-compose.yml
MONITOR_IP=192.168.1.215 docker compose up -d

# Backend only (host A):
MONITOR_IP=192.168.1.215 docker compose up -d emqx timescaledb capture worker streamer

# Dashboard only (host B), pointed at host A:
docker run -d -p 80:80 -e VSCC_HOST=<host-A-ip> --restart unless-stopped \
  ghcr.io/chsbusch-dot/vscc-dashboard:latest
```
</details>

### Alternative: systemd-native install (Linux)

Runs the capture and streamer as systemd services instead of containers —
useful if you prefer journald logging and host-level control. Needs a
Debian/Ubuntu host; `install.sh` offers to install missing tools (git, Docker)
with confirmation.

```bash
# 1. Backend: capture + broker + database (prompts for your monitor's IP)
git clone https://github.com/chsbusch-dot/vscc-mqtt-server.git && cd vscc-mqtt-server
# (no git yet? wget -qO- https://github.com/chsbusch-dot/vscc-mqtt-server/archive/refs/heads/main.tar.gz | tar xz && cd vscc-mqtt-server-main)
sudo ./install.sh

# 2. Dashboard, served from the same host
git clone https://github.com/chsbusch-dot/vscc-dashboard-client.git ../vscc-dashboard-client
docker compose -f vscc-docker-compose.yml --profile dashboard up -d --build

# 3. Open http://<this-host>/ in a browser and press PLAY LIVE
```

## Supported Hardware

| Status | Hardware |
|---|---|
| ✅ Tested | Philips IntelliVue **MP50** (LAN connection) |
| ⚙️ Untested, but plumbed via VSCapture | Other Philips IntelliVue models (MP30–MP90, MX series), serial/MIB mode |
| 🗺️ Roadmap | Other vendors (GE Datex S/5, GE Dash, Mindray/Draeger HL7, Spacelabs) — see [docs/ROADMAP.md](docs/ROADMAP.md) |

Reports from other hardware welcome — open an issue with your model and what happened.

## System Architecture

```
Philips MP50 ──UDP──► VSCaptureCLI ──► export files (JSON numerics + waveform CSVs)
                      (systemd,           │                    │
                       keep-alive         │ tail               │ tail
                       wrapper)           ▼                    ▼
                                    Python Worker        WebSocket Streamer
                                    (docker)             (systemd, :8000)
                                      │       │            GET /DataExportVSC.json
                                      ▼       ▼            WS  /ws/stream
                                   EMQX    TimescaleDB
                                  (:1883,   (:5432)
                                   :8083 ws)  ▲
                                      │       │ /api/historic/{minutes}
                                      ▼       │ (worker FastAPI, :8001)
                                 React Dashboard (vscc-dashboard-client repo)
```

1.  **VSCapture CLI (`systemd` service `vscc-capture-cli`):** A .NET application that connects to the patient monitor and writes raw telemetry to export files in `VSCapture/`. It runs under a keep-alive wrapper (repo file `vscc-capture-loop.sh`, installed by `install.sh` as `VSCapture/vscc-loop.sh`) that pings the monitor before launching and restarts the capture automatically when the monitor goes offline or the UDP connection times out — the service never enters a failed state while waiting for the monitor. The wrapper also handles the MP50's single-association limit: a silent-hang watchdog recycles a capture that runs without producing data (stale association), and abrupt stops are time-stamped so the next start waits out the monitor's ~90 s association timeout. Net effect: `systemctl restart vscc-capture-cli` is always safe; expect up to ~90 s before data resumes.
2.  **Python Worker (`docker` container):** Tails the vitals JSON plus every waveform CSV it discovers (`NOM_*WaveExport.csv` — a newly connected monitor module streams automatically, no code change), publishes each sample to a per-signal MQTT topic, batch-inserts everything into TimescaleDB, and serves historic data over FastAPI on port **8001**.
3.  **EMQX (`docker` container):** The MQTT broker. Raw TCP on **1883**, MQTT-over-WebSocket on **8083** (path `/mqtt`) for browser clients, admin dashboard on **18083** (default login `admin` / `public` — EMQX forces a password change on first login; do it).
4.  **TimescaleDB (`docker` container):** PostgreSQL + TimescaleDB on **5432** (db `telemetry`, hypertables `patient_numerics` / `patient_waveforms`, 12 h retention).
5.  **WebSocket Streamer (`systemd` service `vscc-websocket-streamer`):** Serves the raw numerics JSON over HTTP and a normalized live stream over WebSocket, both on port **8000**.

### MQTT topics and payload

The worker publishes numerics to `mp50/VitalSigns` and waveforms to per-signal topics: `mp50/HF-ECG`, `mp50/HF-EEG`, `mp50/HF-PLETH`, `mp50/HF-RESP` for the common waves, and `mp50/HF-<PhysioID>` for any newly discovered waveform export. Every payload is normalized JSON:

```json
{"time": 1781219308.505, "physio_id": "NOM_PULS_OXIM_SAT_O2", "value": 96.6}
```

`time` is Unix epoch **seconds UTC**. A `value: null` message on a waveform topic signals a lead-off/disconnect. Subscribe to everything with topic filter `mp50/#` (the React dashboard's default).

### Timestamps and `MONITOR_TZ`

VSCapture writes wall-clock **local** time into its exports. The worker and streamer parse it in the timezone given by the `MONITOR_TZ` environment variable (default `America/Los_Angeles`, set in `vscc-docker-compose.yml`) and convert to UTC before publishing or storing. If the capture host moves to another timezone, change `MONITOR_TZ` — otherwise all data lands hours offset from `NOW()` and `/api/historic` queries return nothing.

### Sessions & research exports (worker REST API, :8001)

Recording is organized into **sessions**: the worker opens one automatically when
data starts flowing and closes it after a configurable silence (default 3 min),
like a case recorder. The dashboard (VSCC Studio) manages all of this from the
browser; the same API is available to scripts:

| Endpoint | What it does |
| --- | --- |
| `GET /api/sessions` | List sessions (newest first) |
| `POST /api/sessions` | Start a fresh named session (`label`, `subject_code`, `notes`) at the current boundary |
| `POST /api/sessions/{id}/stop` | Explicitly stop recording an open session |
| `PATCH /api/sessions/{id}` | Rename / set subject code / notes |
| `DELETE /api/sessions/{id}` | Delete a closed session (+ its data rows) |
| `GET /api/sessions/{id}/data` | Replay payload for the dashboard charts |
| `GET /api/sessions/{id}/signals` | Distinct numeric/waveform signals in range |
| `POST` / `GET` / `DELETE /api/annotations` | Timestamped event markers (e.g. "intubation"), optionally tied to a session |
| `GET /api/sessions/{id}/quality` | **Loss statistics**: per-waveform nominal rate, expected vs actual samples, gap count, longest gap |
| `POST /api/sessions/{id}/export` | Write the export package to `./sessions/` on the host |
| `GET /api/sessions/{id}/download` | The export package as one streamed zip (add `?deidentify=1` for a **share-safe** package: relative timestamps, stripped label/subject/notes) |
| `GET /api/sessions/download-all` | Every session's package in one zip |
| `GET /api/sessions/{id}/edf` | Waveforms as **EDF** (one channel per signal) for EDFbrowser / MNE / biosignal toolchains |
| `GET /api/sessions/{id}/hrv` | **Heart-rate variability** from the ECG (R-peaks → RR → SDNN/RMSSD/pNN50 + Poincaré) |
| `GET` / `PUT /api/settings` | Retention hours, session gap, disk/DB usage |
| `GET` / `PUT /api/capture-config` | VSCapture service settings (see below) |

An export package contains `session.json` (metadata + the same quality/loss
statistics), `numerics.csv|.parquet` and `waveforms.csv|.parquet`, all times ISO 8601
UTC. EDF files are generated on demand and place samples on a per-second grid at
each signal's measured nominal rate, so gaps stay aligned (zero-filled) and channel
timing never drifts; values are 16-bit quantized over a symmetric range (digital
0 = physical 0).

### Configuring the capture service from the dashboard

`PUT /api/capture-config` (Settings → Capture in VSCC Studio) accepts `monitor_ip`,
`interval` (1/10/60/300 s), `waveset` (0–12), `scale` (1/2) and `devid`. The worker
persists them and mirrors a `vscc-capture-config.conf` file onto the shared data
volume; the capture container re-reads it before every launch and a watcher recycles
the running capture when it changes. **Applying a change restarts the capture — data
resumes within ~2 minutes** (change detection plus the monitor's association
cool-down). Values in the file are validated on both ends; invalid entries fall back
to the container's environment defaults.

### Monitoring & data integrity (worker, :8001)

| Endpoint | What it does |
| --- | --- |
| `GET /api/status` | Health snapshot: capture state (`live`/`stalled`/`offline`/`no_data`), last-data age, DB lag, DB size, buffer backlog, per-source integrity |
| `GET /api/integrity` | Live per-source report: clock offset and sequence regressions |
| `GET /metrics` | Prometheus exposition of the above |

The worker tracks two **data-integrity** signals per source, derived purely from
the export records (no extra wiring on the monitor):

- **Clock offset** — host wall-clock stamp (`SystemLocalTime`) minus the monitor's
  device stamp (`Timestamp`): the timestamp uncertainty between the source and the
  capture host. On the test MP50 this runs ~80 s, so researchers know the absolute-time
  error budget on stored samples.
- **Sequence regressions** — the monitor's monotonic `Relativetimestamp` going
  backwards, which flags a capture restart / new association (a likely data gap).

Per-**session** data-loss statistics (expected vs actual samples, gaps) are under
`GET /api/sessions/{id}/quality`. A background **gap watchdog** logs capture-liveness
transitions (`live → stalled → offline`). All of this is observability only — the
system is research/education software and never raises clinical alarms.

Scrape with Prometheus:

```yaml
scrape_configs:
  - job_name: vscc
    static_configs:
      - targets: ['<worker-host>:8001']
```

## Directory Structure

A brief overview of the key files and directories:

```
vscc-mqtt-server/
├── VSCapture/              # Downloaded VSCaptureCLI binaries, export files, vscc-loop.sh wrapper
├── .venv/                  # Python virtual environment used by the streamer service
├── install.sh              # The main installation and uninstallation script
├── update.sh               # Script to update components, run by a cron job
├── vscc-docker-compose.yml # Defines the Docker-based services (DB, MQTT, Worker) — image tags pinned
├── vscc-worker.Dockerfile  # Dockerfile for building the Python worker image
├── .dockerignore           # Optimizes the Docker build by excluding unnecessary files
├── vscc_mqtt_timescale_worker.py # Worker: tails exports → MQTT + TimescaleDB + /api/historic
├── vscc-websocket-streamer.py    # Streamer: raw JSON over HTTP + normalized WS stream (:8000)
├── vscc-install-capture-cli.sh   # Installs the VSCaptureCLI systemd service
├── vscc-install-websocket-streamer.sh # Installs the streamer systemd service
├── vscc-init-timescaledb.sql     # Creates hypertables + retention policies on first DB start
├── vscc-file-cleanup.py    # Hourly cron: truncates oversized export files
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

## Day-to-Day Operations

Everything starts automatically at boot (Docker `restart: unless-stopped` + enabled systemd units) and capture resumes by itself when the monitor is powered on — the keep-alive wrapper pings it every ~12 seconds. There is normally nothing to launch.

Manual control:

```bash
cd ~/vscc-mqtt-server
docker compose -f vscc-docker-compose.yml up -d        # broker, DB, worker
sudo systemctl start vscc-capture-cli vscc-websocket-streamer
```

Serve the dashboard from this host too (single-host install — UI at `http://<host>/`):

```bash
git clone git@github.com:chsbusch-dot/vscc-dashboard-client.git ../vscc-dashboard-client
docker compose -f vscc-docker-compose.yml --profile dashboard up -d --build
```

After editing code:

```bash
# worker (runs inside Docker)
docker compose -f vscc-docker-compose.yml up -d --build worker
# streamer (runs via systemd)
sudo systemctl restart vscc-websocket-streamer
```

Verify data is flowing:

```bash
# watch live MQTT messages
mosquitto_sub -h localhost -t 'mp50/#' -v
# rows inserted in the last minute + lag in seconds
docker exec vscc-mqtt-server-timescaledb-1 psql -U postgres -d telemetry -Atc \
  "SELECT count(*), extract(epoch from now()-max(time))::int FROM patient_numerics WHERE time > now() - interval '1 minute';"
# historic API (worker)
curl http://localhost:8001/api/historic/5
```

Logs: `journalctl -u vscc-capture-cli -f`, `journalctl -u vscc-websocket-streamer -f`, `docker logs -f vscc-mqtt-server-worker-1`. Note: the worker container buffers stdout, so sparse logs don't mean it is stuck — trust the DB freshness query. EMQX admin UI: `http://<host>:18083` (first login: `admin` / `public`, then it prompts you to set a real password).

## Automated Maintenance

The installer automatically sets up cron jobs to keep the system running smoothly.

-   **Hourly File Cleanup:** The `VSCaptureCLI` tool generates several large data files. To prevent these from consuming excessive disk space, the `vscc-file-cleanup.py` script runs every hour, truncates any data file that grows beyond 20MB, and then restarts the VSCapture process (via the keep-alive wrapper) so it re-binds to the fresh file inodes.
-   **Quarterly Updates:** The `update.sh` script runs every three months and pulls the images referenced in `vscc-docker-compose.yml`. Image tags are **pinned** (`emqx/emqx:6.0.0`, `timescale/timescaledb:2.19.3-pg14`), so upgrades only happen when a tag is deliberately bumped in the compose file — floating `latest` tags previously risked unattended breaking upgrades.

## Testing VSCapture's Direct MQTT Export

Besides the production pipeline (worker tails export files), VSCaptureCLI can publish
numerics straight to the broker (`-export 3`). Useful as a debugging/secondary feed —
verified working against the local EMQX. **Not** a replacement for the worker: it sends
numerics only (no waveforms), as raw JSON batch arrays with local-time strings the
dashboard cannot parse.

The monitor accepts only one data-export association at a time, so stop the production
capture first:

```bash
sudo systemctl stop vscc-capture-cli
cd ~/vscc-mqtt-server/VSCapture   # or a scratch copy; export files in here are root-owned
dotnet VSCaptureCLI.dll --devices 1 --device1type 1 --device1model 1 --device1arg "-mode 2 -port 192.168.1.215 -interval 1 -export 3 -devid mp50 -url ws://127.0.0.1:8083/mqtt -topic telemetry/mp50 -user none -passw none -waveset 0 -scale 2"
```

Watch the data arrive in a second terminal:

```bash
mosquitto_sub -h localhost -t 'telemetry/mp50/#' -v
```

Gotchas (all learned the hard way):

-   VSCaptureCLI appends **`/Numeric`** to the topic — it publishes to
    `telemetry/mp50/Numeric`, so always subscribe with the `/#` wildcard.
-   It prints **nothing** about MQTT, ever — connection failures and successes are equally
    silent (fire-and-forget tasks). The broker is the only source of truth.
-   The URL needs the port **and** path: `ws://127.0.0.1:8083/mqtt`. A bare `ws://127.0.0.1`
    silently targets port 80 and publishes nothing.
-   EMQX's default ACL denies subscribing to the bare `#` wildcard — use a scoped filter
    like `telemetry/mp50/#`.
-   Messages are retained QoS 1 batch arrays; a new WebSocket connection is opened for
    every 1-second batch. `Value` fields are strings and include `"-"` placeholders for
    sensors without a reading.
-   When run as a regular user inside `VSCapture/`, file writes fail with
    `UnauthorizedAccessException` spam (the export files are root-owned, created by the
    systemd service). Run from a scratch copy of the binaries instead.

When done, restore production capture:

```bash
sudo systemctl start vscc-capture-cli
```

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

## Credits & License

- **[VSCapture](https://sourceforge.net/projects/vscapture/files/)** by **John George K** —
  the open-source patient-monitor capture tool that talks to the monitor itself.
  Licensed under the **LGPL**; it is not bundled in this repository but downloaded
  from its official release at install time and runs as a separate process.
- **This project** (backend stack and installer, plus the
  [vscc-dashboard-client](https://github.com/chsbusch-dot/vscc-dashboard-client)
  frontend) is licensed under the **MIT License** — see [LICENSE](LICENSE).
- [SciChart.js](https://www.scichart.com/) (dashboard charting) is commercial
  software with a free community license; see their terms for commercial use.
- Not affiliated with, or endorsed by, Koninklijke Philips N.V. "IntelliVue" is a
  trademark of its respective owner; it is referenced solely for interoperability.
