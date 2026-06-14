import asyncio
import json
import os
import re
import tempfile
from array import array
from pathlib import Path
from contextlib import asynccontextmanager
import shutil
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Union, List, Dict, Any, Optional
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
import asyncpg
import aiomqtt
from prometheus_client import (
    CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST,
)

# --- Configuration ---

BASE_DIR = Path(os.getenv("VSC_DATA_DIR", Path(__file__).resolve().parent / "VSCapture"))
MQTT_BROKER = os.getenv("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:password@127.0.0.1:5432/telemetry")
WAVEFORM_DISCONNECT_TIMEOUT = 3.0  # Seconds

# --- File & Topic Mapping ---
#
# Numerics: DataExportVSC.json carries EVERY parameter PhysioID, so any new
# numeric (e.g. a BIS index from a newly-connected module) is already picked up
# by the JSON tailer with no code change.
#
# Waveforms: one CSV per signal (NOM_<id>WaveExport.csv). Rather than hardcode a
# list, we DISCOVER them by glob and keep scanning, so new modules (BIS EEG,
# capnography, ABP, ...) stream live the moment they appear.

NUMERICS_CONFIG = {
    "path": BASE_DIR / "DataExportVSC.json",
    "type": "json",
    "topic": "mp50/VitalSigns",
    "physio_id": None,
}

WAVE_GLOB = "NOM_*WaveExport.csv"

# Friendly short topic names for the common waves (keeps existing dashboard
# subscriptions working). Unknown waves fall back to a slug derived from the
# PhysioID, so the dashboard can wildcard-subscribe to mp50/HF-#.
WAVE_TOPIC_ALIASES = {
    "NOM_ECG_ELEC_POTL_II": "HF-ECG",
    "NOM_PLETH": "HF-PLETH",
    "NOM_RESP": "HF-RESP",
}


def physio_id_from_wave_file(path: Path) -> str:
    """NOM_PLETHWaveExport.csv -> NOM_PLETH"""
    suffix = "WaveExport.csv"
    name = path.name
    return name[:-len(suffix)] if name.endswith(suffix) else path.stem


def wave_topic(physio_id: str) -> str:
    short = WAVE_TOPIC_ALIASES.get(physio_id)
    if short is None:
        base = physio_id[4:] if physio_id.startswith("NOM_") else physio_id
        short = "HF-" + base
    return f"mp50/{short}"


def wave_config(path: Path) -> Dict[str, Any]:
    physio_id = physio_id_from_wave_file(path)
    return {"path": path, "type": "csv", "topic": wave_topic(physio_id),
            "physio_id": physio_id}

# Global batch buffers for TimescaleDB
numerics_buffer = []
waveforms_buffer = []
db_pool = None
# Newest data timestamp inserted this process-lifetime (data-time axis, UTC).
# Drives the auto-session manager.
last_data_time = None

# --- Observability state (process-lifetime, in-memory) ---
WORKER_START = None                                    # set in lifespan
inserted_totals = {"patient_numerics": 0, "patient_waveforms": 0}
# Per-source (DeviceID) integrity: clock offset, sequence tracking, liveness.
source_state: Dict[str, Dict[str, Any]] = {}
# Capture liveness thresholds (waveforms keep last_data_time sub-second fresh
# while capturing, so these detect a stalled/stopped capture regardless of the
# numerics interval).
CAPTURE_STALL_S = 15
CAPTURE_OFFLINE_S = 120

def _capture_state(now: datetime):
    if last_data_time is None:
        return "no_data", None
    age = (now - last_data_time).total_seconds()
    if age <= CAPTURE_STALL_S:
        return "live", age
    if age <= CAPTURE_OFFLINE_S:
        return "stalled", age
    return "offline", age

def _track_source_integrity(record: Dict[str, Any]):
    """Per-source data-integrity signals, derived purely from the export record:
    - clock offset = host wall-clock stamp (SystemLocalTime) minus monitor
      device stamp (Timestamp): timestamp uncertainty between source and capture.
    - sequence regression = the monitor's monotonic Relativetimestamp going
      backwards, which flags a capture restart / new association."""
    device = record.get("DeviceID") or "unknown"
    st = source_state.setdefault(device, {
        "clock_offset_s": None, "sequence_regressions": 0,
        "last_relativetimestamp": None, "last_seen": None, "samples": 0})
    st["samples"] += 1
    st["last_seen"] = datetime.now(timezone.utc)
    mono = parse_vsc_timestamp(record.get("Timestamp"))
    host = parse_vsc_timestamp(record.get("SystemLocalTime"))
    if mono and host:
        st["clock_offset_s"] = (host - mono).total_seconds()
    try:
        rel = int(record.get("Relativetimestamp"))
        prev = st["last_relativetimestamp"]
        if prev is not None and rel < prev:
            st["sequence_regressions"] += 1
        st["last_relativetimestamp"] = rel
    except (ValueError, TypeError):
        pass

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "/sessions"))
DEFAULT_SETTINGS = {
    "retention_hours": "12",
    "session_gap_minutes": "3",  # data silence that closes a session
}

# --- Utility Functions ---

# VSCapture timestamps are wall-clock time of the capture host, not UTC.
MONITOR_TZ = ZoneInfo(os.getenv("MONITOR_TZ", "America/Los_Angeles"))

# Timezone used ONLY to render the human-readable default session label
# (e.g. "Session 2026-06-13 11:10"). All stored timestamps stay UTC; this is
# cosmetic. Defaults to UTC so the public image is timezone-neutral; set
# SESSION_LABEL_TZ (e.g. America/Los_Angeles) to localize auto-session labels.
# The dashboard's "New session" button sends a browser-local label of its own,
# so this only governs the headless auto-session manager's default labels.
SESSION_LABEL_TZ = ZoneInfo(os.getenv("SESSION_LABEL_TZ", "UTC"))

def _default_session_label(now_utc: datetime) -> str:
    """Default 'Session <local date/time>' label, localized to SESSION_LABEL_TZ."""
    return f"Session {now_utc.astimezone(SESSION_LABEL_TZ).strftime('%Y-%m-%d %H:%M')}"

def parse_vsc_timestamp(raw_time: str) -> Union[datetime, None]:
    if not raw_time: return None
    try: return datetime.strptime(raw_time, "%d-%m-%Y %H:%M:%S.%f").replace(tzinfo=MONITOR_TZ).astimezone(timezone.utc)
    except ValueError:
        try: return datetime.strptime(raw_time, "%d-%m-%Y %H:%M:%S").replace(tzinfo=MONITOR_TZ).astimezone(timezone.utc)
        except ValueError: return None

# --- 1. The Database Batch Worker ---
async def batch_inserter():
    """Wakes up every 1 second, drains the buffers, and executes bulk inserts."""
    global numerics_buffer, waveforms_buffer
    while True:
        await asyncio.sleep(1.0)
        
        numerics_to_insert = numerics_buffer
        numerics_buffer = []
        waveforms_to_insert = waveforms_buffer
        waveforms_buffer = []

        if not numerics_to_insert and not waveforms_to_insert: continue

        try:
            async with db_pool.acquire() as connection:
                if numerics_to_insert:
                    await connection.executemany("INSERT INTO patient_numerics (time, physio_id, value) VALUES ($1, $2, $3)", numerics_to_insert)
                    inserted_totals["patient_numerics"] += len(numerics_to_insert)
                if waveforms_to_insert:
                    await connection.executemany("INSERT INTO patient_waveforms (time, physio_id, value) VALUES ($1, $2, $3)", waveforms_to_insert)
                    inserted_totals["patient_waveforms"] += len(waveforms_to_insert)
            global last_data_time
            newest = max((row[0] for row in numerics_to_insert + waveforms_to_insert), default=None)
            if newest and (last_data_time is None or newest > last_data_time):
                last_data_time = newest
        except Exception as e:
            print(f"Database batch insert error: {e}")

# --- 2. The Generic Async File Tailer ---
async def tail_file(config: Dict[str, Any], client: aiomqtt.Client):
    file_path, file_type, topic = config["path"], config["type"], config["topic"]
    print(f"Waiting for file: {file_path}...")
    while not file_path.exists() or not file_path.is_file(): await asyncio.sleep(1)
    print(f"File found: {file_path}. Tailing live data for topic {topic}...")

    disconnected_sent = False
    loop = asyncio.get_event_loop()

    while True:
        if not file_path.exists():
            await asyncio.sleep(1)
            continue

        try:
            async with aiofiles.open(file_path, mode="r", encoding="utf-8", errors="ignore") as f:
                await f.seek(0, os.SEEK_END)
                last_size = os.path.getsize(file_path)
                last_ino = os.stat(file_path).st_ino
                # NOTE: monotonic clock, function-LOCAL — deliberately NOT the
                # module-global `last_data_time` (UTC datetime, which drives
                # sessions/capture-state). Distinct name so the two never cross.
                last_read_monotonic = loop.time()

                while True:
                    line = await f.readline()

                    # EOF reached; reading doesn't block, it just returns ""
                    if not line:
                        current_time = loop.time()
                        
                        # 1. Check for Disconnects (Lead Off)
                        if current_time - last_read_monotonic > WAVEFORM_DISCONNECT_TIMEOUT:
                            if file_type == "csv" and not disconnected_sent:
                                print(f"Timeout on {file_path.name}. Sending disconnect signal to {topic}.")
                                null_payload = json.dumps({"value": None, "physio_id": config["physio_id"], "time": datetime.now(timezone.utc).timestamp()})
                                await client.publish(topic, payload=null_payload, qos=0)
                                disconnected_sent = True
                        
                        # 2. Check for File Rotation (vscc-file-cleanup.py)
                        try:
                            current_size = os.path.getsize(file_path)
                            current_ino = os.stat(file_path).st_ino
                        except OSError:
                            break # File likely deleted, break to re-open loop
                        
                        if current_size < last_size or current_ino != last_ino:
                            print(f"File {file_path.name} rotated or truncated. Re-opening...")
                            break # Break out of inner loop to re-open the fresh file
                            
                        last_size = current_size
                        await asyncio.sleep(0.05) # Prevent CPU spinning
                        continue

                    # We successfully read a line
                    last_read_monotonic = loop.time()
                    disconnected_sent = False
                    line = line.strip()
                    if not line:
                        continue

                    if file_type == "json":
                        try:
                            data_array = json.loads(line)
                            if isinstance(data_array, dict): data_array = [data_array]
                            for record in data_array:
                                _track_source_integrity(record)
                                val_str = record.get("Value", "-")
                                if val_str == "-": continue
                                try: value_float = float(val_str)
                                except (ValueError, TypeError): continue
                                physio_id = record.get("PhysioID")
                                parsed_time = parse_vsc_timestamp(record.get("SystemLocalTime", record.get("Timestamp")))
                                if not physio_id or not parsed_time: continue
                                numerics_buffer.append((parsed_time, physio_id, value_float))
                                payload = json.dumps({"time": parsed_time.timestamp(), "physio_id": physio_id, "value": value_float})
                                await client.publish(topic, payload=payload, qos=0)
                        except Exception: pass
                    elif file_type == "csv":
                        try:
                            parts = line.split(',')
                            if len(parts) < 4: continue
                            parsed_time = parse_vsc_timestamp(parts[2])
                            if not parsed_time: continue
                            try: value_float = float(parts[3])
                            except (ValueError, TypeError): continue
                            physio_id = config["physio_id"]
                            waveforms_buffer.append((parsed_time, physio_id, value_float))
                            payload = json.dumps({"time": parsed_time.timestamp(), "physio_id": physio_id, "value": value_float})
                            await client.publish(topic, payload=payload, qos=0)
                        except Exception: pass

        except Exception as e:
            print(f"Error tailing {file_path.name}: {e}")
            await asyncio.sleep(1)

# --- 2b. Waveform Auto-Discovery ---
async def wave_discovery_loop(client: aiomqtt.Client, tailed_paths: set):
    """Periodically scan the data dir for NEW wave-export files (e.g. a module
    like BIS connected mid-session) and launch a tailer for each. Each file is
    tailed once; tail_file itself survives truncation/rotation, so we only spawn
    for paths not already running."""
    while True:
        try:
            for path in sorted(BASE_DIR.glob(WAVE_GLOB)):
                if path in tailed_paths:
                    continue
                tailed_paths.add(path)
                config = wave_config(path)
                print(f"Discovered waveform: {path.name} -> {config['topic']} ({config['physio_id']})")
                asyncio.create_task(tail_file(config, client))
        except Exception as e:
            print(f"Wave discovery error: {e}")
        await asyncio.sleep(10)

# --- 3. Schema bootstrap, App Lifespan & FastAPI ---

# Mirrors vscc-init-timescaledb.sql so the stack also runs without the init.sql
# bind mount (single-file `docker compose up` install). Every statement is
# idempotent; on an already-initialized database this is a no-op.
SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS patient_numerics (
        time TIMESTAMPTZ NOT NULL,
        physio_id TEXT NOT NULL,
        value DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS patient_waveforms (
        time TIMESTAMPTZ NOT NULL,
        physio_id TEXT NOT NULL,
        value DOUBLE PRECISION NOT NULL
    )""",
    "SELECT create_hypertable('patient_numerics', 'time', if_not_exists => TRUE)",
    "SELECT create_hypertable('patient_waveforms', 'time', if_not_exists => TRUE)",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS patient_waveforms_1min
       WITH (timescaledb.continuous) AS
       SELECT physio_id,
              time_bucket('1 minute', time) AS bucket,
              AVG(value) AS avg_value,
              MIN(value) AS min_value,
              MAX(value) AS max_value
       FROM patient_waveforms
       GROUP BY physio_id, bucket""",
    """SELECT add_continuous_aggregate_policy('patient_waveforms_1min',
        start_offset => INTERVAL '1 hour',
        end_offset   => INTERVAL '1 minute',
        schedule_interval => INTERVAL '5 minutes',
        if_not_exists => TRUE)""",
    "SELECT add_retention_policy('patient_numerics', INTERVAL '12 hours', if_not_exists => TRUE)",
    "SELECT add_retention_policy('patient_waveforms', INTERVAL '12 hours', if_not_exists => TRUE)",
    # Sessions: time-range metadata over the data axis (no per-row tagging).
    """CREATE TABLE IF NOT EXISTS sessions (
        id SERIAL PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        subject_code TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS vscc_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # Annotations: timestamped event markers (e.g. "intubation", "drug given")
    # on the data-time axis, optionally tied to a session.
    """CREATE TABLE IF NOT EXISTS annotations (
        id SERIAL PRIMARY KEY,
        time TIMESTAMPTZ NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS annotations_time_idx ON annotations (time)",
]

async def ensure_schema(pool):
    failures = 0
    async with pool.acquire() as connection:
        for stmt in SCHEMA_STATEMENTS:
            try:
                await connection.execute(stmt)
            except Exception as e:
                failures += 1
                print(f"Schema bootstrap statement failed (continuing): {e}")
    if failures:
        print(f"Database schema bootstrap finished with {failures}/{len(SCHEMA_STATEMENTS)} "
              f"statement(s) failing — see errors above.")
    else:
        print("Database schema verified.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, WORKER_START
    WORKER_START = datetime.now(timezone.utc)
    while True:
        try:
            db_pool = await asyncpg.create_pool(DB_DSN)
            print("Connected to TimescaleDB.")
            break
        except Exception as e:
            print(f"Database connection failed: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
    await ensure_schema(db_pool)
    # Apply persisted settings (e.g. user-configured retention) on every boot.
    try:
        await apply_retention(int((await get_settings())["retention_hours"]))
    except Exception as e:
        print(f"Could not apply persisted retention setting: {e}")
    # Mirror persisted capture settings to the shared config file (survives
    # volume recreation; no-op while the user never changed capture settings).
    try:
        if any(k.startswith("capture_") for k in await get_settings()):
            _write_capture_config_file(await _capture_settings())
    except Exception as e:
        print(f"Could not write capture config file: {e}")
    
    tasks = []
    app_started = False
    try:
        while not app_started:
            try:
                async with aiomqtt.Client(MQTT_BROKER, port=MQTT_PORT) as client:
                    print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
                    tasks.append(asyncio.create_task(batch_inserter()))
                    tasks.append(asyncio.create_task(session_manager()))
                    tasks.append(asyncio.create_task(gap_watchdog()))
                    # Numerics: one fixed JSON tailer (carries every parameter PhysioID).
                    tasks.append(asyncio.create_task(tail_file(NUMERICS_CONFIG, client)))
                    # Waveforms: discovered dynamically so new modules stream with no code change.
                    tasks.append(asyncio.create_task(wave_discovery_loop(client, set())))
                    print("Launched numerics tailer + waveform auto-discovery + DB batch worker + watchdog.")
                    app_started = True
                    yield
            except aiomqtt.MqttError as e:
                # At boot the EMQX container can take ~15s longer than this one.
                if app_started:
                    print(f"MQTT connection closed during shutdown: {e}")
                else:
                    print(f"MQTT broker not ready: {e}. Retrying in 5s...")
                    await asyncio.sleep(5)
    finally:
        print("Shutting down... Cancelling background tasks.")
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if db_pool: await db_pool.close()

app = FastAPI(lifespan=lifespan)
# allow_credentials stays False: the API uses no cookies/auth, and "*" origins
# with credentials is both an invalid combination and a needless exposure for a
# service the README flags as PHI-bearing.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/historic/{range_minutes}")
async def get_historic_data(range_minutes: int):
    """
    The React UI hits this when zooming out past the live window.
    range_minutes is coerced to int by FastAPI, so it is safe to inline.
    """
    # Clamp to a sane window: negatives produce a future (empty) window, and an
    # unbounded value would full-scan the whole hypertable on an open endpoint.
    range_minutes = max(1, min(int(range_minutes), 10080))  # 1 minute .. 7 days
    query = f"""
        SELECT time, physio_id, value
        FROM patient_numerics
        WHERE time >= NOW() - INTERVAL '{range_minutes} minutes'
        ORDER BY time ASC
    """

    async with db_pool.acquire() as connection:
        records = await connection.fetch(query)

    # Format for the React charting engine
    history = {}
    for r in records:
        pid = r['physio_id']
        if pid not in history:
            history[pid] = []
        # Standardize on Unix Epoch Seconds (Float)
        history[pid].append({"time": r['time'].timestamp(), "value": r['value']})

    return history

# --- 4. Settings ---

async def get_settings() -> Dict[str, str]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM vscc_settings")
    settings = dict(DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in rows})
    return settings

async def apply_retention(hours: int):
    """Live-update the retention policies (whole-chunk drops, applied by Timescale's scheduler)."""
    async with db_pool.acquire() as conn:
        for table in ("patient_numerics", "patient_waveforms"):
            await conn.execute(f"SELECT remove_retention_policy('{table}', if_exists => TRUE)")
            await conn.execute(f"SELECT add_retention_policy('{table}', INTERVAL '{int(hours)} hours', if_not_exists => TRUE)")

@app.get("/api/settings")
async def read_settings():
    settings = await get_settings()
    async with db_pool.acquire() as conn:
        db_bytes = await conn.fetchval("SELECT pg_database_size('telemetry')")
    disk = {}
    try:
        usage = shutil.disk_usage(SESSIONS_DIR if SESSIONS_DIR.exists() else "/")
        disk = {"total_bytes": usage.total, "free_bytes": usage.free}
    except Exception:
        pass
    return {**settings, "db_size_bytes": db_bytes, "disk": disk,
            "sessions_dir": str(SESSIONS_DIR), "parquet_available": _parquet_available()}

@app.put("/api/settings")
async def write_settings(payload: Dict[str, Any]):
    allowed = {k: str(v) for k, v in payload.items() if k in DEFAULT_SETTINGS}
    # Validate numeric settings BEFORE persisting — a non-numeric value would
    # otherwise be stored and then break the session manager / retention loop on
    # every tick (it reads these back) until the row is hand-fixed.
    if "retention_hours" in allowed:
        try:
            if int(allowed["retention_hours"]) <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return {"ok": False, "error": "retention_hours must be a positive integer"}
    if "session_gap_minutes" in allowed:
        try:
            if float(allowed["session_gap_minutes"]) <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return {"ok": False, "error": "session_gap_minutes must be a positive number"}
    async with db_pool.acquire() as conn:
        for k, v in allowed.items():
            await conn.execute(
                "INSERT INTO vscc_settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", k, v)
    if "retention_hours" in allowed:
        try:
            await apply_retention(int(allowed["retention_hours"]))
        except Exception as e:
            return {"ok": False, "error": f"settings saved but retention not applied: {e}"}
    return {"ok": True, **allowed}

# --- 4b. Capture service configuration ---
#
# The dashboard edits these; the worker persists them in vscc_settings and
# mirrors them to a plain key=value file on the shared /data volume. The
# capture container re-reads the file before every launch and recycles the
# running capture when it changes (data resumes within ~2 minutes: change
# detection plus the monitor's association cool-down). The file is key=value
# rather than JSON because the capture image only ships python3-minimal,
# which has no json module — plain lines parse with sed/grep.

CAPTURE_CONFIG_FILE = BASE_DIR / "vscc-capture-config.conf"
CAPTURE_DEFAULTS = {
    "monitor_ip": "",     # empty = keep the capture container's MONITOR_IP env
    "interval": "1",      # numerics export interval, seconds
    "waveset": "12",      # VSCapture waveform set (12 = all waves)
    "scale": "2",
    "devid": "mp50",      # device id used in export file names / MQTT topics
}
_IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

def _validate_capture(key: str, value: str) -> Optional[str]:
    """Returns an error message, or None when the value is acceptable."""
    if key == "monitor_ip":
        if value == "":
            return None
        m = _IP_RE.match(value)
        return None if m and all(int(o) <= 255 for o in m.groups()) else "monitor_ip must be an IPv4 address"
    if key == "interval":
        return None if value in ("1", "10", "60", "300") else "interval must be 1, 10, 60 or 300 (seconds)"
    if key == "waveset":
        return None if value.isdigit() and 0 <= int(value) <= 12 else "waveset must be 0-12"
    if key == "scale":
        return None if value in ("1", "2") else "scale must be 1 or 2"
    if key == "devid":
        return None if re.match(r"^[A-Za-z0-9_-]{1,32}$", value) else "devid must be 1-32 chars of [A-Za-z0-9_-]"
    return "unknown key"

async def _capture_settings() -> Dict[str, str]:
    stored = await get_settings()
    return {k: stored.get(f"capture_{k}", v) for k, v in CAPTURE_DEFAULTS.items()}

def _write_capture_config_file(cfg: Dict[str, str]):
    """Atomic write (tmp + rename) so the capture's watcher never sees a torn file."""
    content = "".join(f"{k}={v}\n" for k, v in cfg.items() if v != "")
    CAPTURE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CAPTURE_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(content)
    os.replace(tmp, CAPTURE_CONFIG_FILE)

@app.get("/api/capture-config")
async def read_capture_config():
    cfg = await _capture_settings()
    return {**cfg, "config_file": str(CAPTURE_CONFIG_FILE)}

@app.put("/api/capture-config")
async def write_capture_config(payload: Dict[str, Any]):
    updates = {k: str(payload[k]).strip() for k in CAPTURE_DEFAULTS if k in payload}
    if not updates:
        return {"ok": False, "error": "nothing to update"}
    for k, v in updates.items():
        err = _validate_capture(k, v)
        if err:
            return {"ok": False, "error": err}
    async with db_pool.acquire() as conn:
        for k, v in updates.items():
            await conn.execute(
                "INSERT INTO vscc_settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", f"capture_{k}", v)
    cfg = await _capture_settings()
    try:
        _write_capture_config_file(cfg)
    except Exception as e:
        return {"ok": False, "error": f"settings saved but config file not written: {e}"}
    return {"ok": True, **cfg}

# --- 4c. Observability: status, Prometheus metrics, integrity report ---

METRICS = CollectorRegistry()
_G = lambda name, doc, labels=None: Gauge(name, doc, labels or [], registry=METRICS)
M_last_age   = _G("vscc_last_data_age_seconds", "Seconds since the newest data point ingested")
M_db_lag     = _G("vscc_db_lag_seconds", "Seconds between now and the newest row in patient_numerics")
M_db_size    = _G("vscc_db_size_bytes", "TimescaleDB 'telemetry' database size")
M_capture_up = _G("vscc_capture_up", "1 when capture is live (data within the stall threshold)")
M_uptime     = _G("vscc_worker_uptime_seconds", "Worker process uptime")
M_buffer     = _G("vscc_buffer_backlog_rows", "Rows buffered awaiting insert", ["kind"])
M_inserted   = _G("vscc_inserted_rows_total", "Cumulative rows inserted this process-lifetime", ["table"])
M_offset     = _G("vscc_source_clock_offset_seconds", "Host stamp minus monitor stamp, per source", ["device"])
M_seq        = _G("vscc_source_sequence_regressions_total", "Monitor Relativetimestamp regressions, per source", ["device"])

async def _refresh_metrics():
    now = datetime.now(timezone.utc)
    state, age = _capture_state(now)
    M_last_age.set(age if age is not None else -1)
    M_capture_up.set(1 if state == "live" else 0)
    M_uptime.set((now - WORKER_START).total_seconds() if WORKER_START else 0)
    M_buffer.labels(kind="numerics").set(len(numerics_buffer))
    M_buffer.labels(kind="waveforms").set(len(waveforms_buffer))
    for table, n in inserted_totals.items():
        M_inserted.labels(table=table).set(n)
    for dev, s in source_state.items():
        if s["clock_offset_s"] is not None:
            M_offset.labels(device=dev).set(s["clock_offset_s"])
        M_seq.labels(device=dev).set(s["sequence_regressions"])
    try:
        async with db_pool.acquire() as conn:
            db_max = await conn.fetchval("SELECT max(time) FROM patient_numerics")
            db_size = await conn.fetchval("SELECT pg_database_size('telemetry')")
        M_db_lag.set((now - db_max).total_seconds() if db_max else -1)
        M_db_size.set(db_size or 0)
    except Exception as e:
        print(f"metrics db refresh failed: {e}")

@app.get("/metrics")
async def metrics():
    """Prometheus exposition — scrape target for capture state, last-data age,
    DB lag/size, per-source clock offset and sequence regressions."""
    await _refresh_metrics()
    return Response(generate_latest(METRICS), media_type=CONTENT_TYPE_LATEST)

def _sources_view(now: datetime) -> Dict[str, Any]:
    return {dev: {
        "clock_offset_seconds": s["clock_offset_s"],
        "sequence_regressions": s["sequence_regressions"],
        "samples_seen": s["samples"],
        "last_seen_age_seconds": (now - s["last_seen"]).total_seconds() if s["last_seen"] else None,
    } for dev, s in source_state.items()}

@app.get("/api/status")
async def status():
    """Health snapshot for dashboards/monitoring: capture state, last-data age,
    DB lag, buffer backlog, per-source integrity."""
    now = datetime.now(timezone.utc)
    state, age = _capture_state(now)
    db_lag = db_size = None
    try:
        async with db_pool.acquire() as conn:
            db_max = await conn.fetchval("SELECT max(time) FROM patient_numerics")
            db_size = await conn.fetchval("SELECT pg_database_size('telemetry')")
        db_lag = (now - db_max).total_seconds() if db_max else None
    except Exception as e:
        print(f"status db query failed: {e}")
    return {
        "capture_state": state,
        "last_data_age_seconds": age,
        "db_lag_seconds": db_lag,
        "db_size_bytes": db_size,
        "worker_uptime_seconds": (now - WORKER_START).total_seconds() if WORKER_START else None,
        "buffer_backlog": {"numerics": len(numerics_buffer), "waveforms": len(waveforms_buffer)},
        "inserted_total": dict(inserted_totals),
        "sources": _sources_view(now),
        "thresholds": {"stall_s": CAPTURE_STALL_S, "offline_s": CAPTURE_OFFLINE_S},
    }

@app.get("/api/integrity")
async def integrity_report():
    """Live per-source data-integrity report: clock offset (host SystemLocalTime
    minus monitor Timestamp — timestamp uncertainty between source and capture)
    and sequence regressions (monitor counter going backwards = capture restart).
    Per-session data-loss statistics live under /api/sessions/{id}/quality."""
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "clock_offset_note": "host SystemLocalTime minus monitor Timestamp, seconds",
        "sources": _sources_view(now),
    }

# --- 5. Sessions: auto-managed recording ranges over the data-time axis ---

async def session_manager():
    """Opens a session when data starts flowing, closes it after a configurable
    silence (monitor off / case over). Runs forever; survives DB hiccups."""
    while True:
        await asyncio.sleep(10)
        try:
            settings = await get_settings()
            gap = timedelta(minutes=float(settings["session_gap_minutes"]))
            now = datetime.now(timezone.utc)
            async with db_pool.acquire() as conn:
                open_row = await conn.fetchrow("SELECT id, started_at FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
                if last_data_time is None:
                    # No data this process-lifetime; close any stale open session at its last known data.
                    if open_row:
                        last = await conn.fetchval("SELECT max(time) FROM patient_numerics") or open_row["started_at"]
                        await conn.execute("UPDATE sessions SET ended_at = GREATEST($1, started_at) WHERE id=$2", last, open_row["id"])
                        print(f"[Sessions] Closed stale session #{open_row['id']}")
                    continue
                if now - last_data_time <= gap and open_row is None:
                    row = await conn.fetchrow(
                        "INSERT INTO sessions (label, started_at) VALUES ($1, $2) RETURNING id",
                        _default_session_label(now), last_data_time)
                    print(f"[Sessions] Opened session #{row['id']}")
                elif now - last_data_time > gap and open_row is not None:
                    await conn.execute("UPDATE sessions SET ended_at = GREATEST($1, started_at) WHERE id=$2", last_data_time, open_row["id"])
                    print(f"[Sessions] Closed session #{open_row['id']} (data gap)")
        except Exception as e:
            print(f"[Sessions] manager error: {e}")

async def gap_watchdog():
    """Logs capture-liveness transitions (live → stalled → offline and back).
    Notification = log + reflected in /api/status; this never alarms (the system
    is research/education only and must not be used for clinical alerting)."""
    prev = "init"
    while True:
        await asyncio.sleep(10)
        now = datetime.now(timezone.utc)
        state, age = _capture_state(now)
        if state != prev:
            detail = f" (no data for {age:.0f}s)" if age is not None else ""
            print(f"[Watchdog] capture {prev} -> {state}{detail}")
            prev = state

def _session_dict(r) -> Dict[str, Any]:
    return {"id": r["id"], "label": r["label"], "subject_code": r["subject_code"],
            "notes": r["notes"], "started_at": r["started_at"].timestamp(),
            "ended_at": r["ended_at"].timestamp() if r["ended_at"] else None,
            "recording": r["ended_at"] is None}

@app.get("/api/sessions")
async def list_sessions():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 200")
    return [_session_dict(r) for r in rows]

@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: int, payload: Dict[str, Any]):
    fields = {k: str(payload[k]) for k in ("label", "subject_code", "notes") if k in payload}
    if not fields:
        return {"ok": False, "error": "nothing to update"}
    sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow(f"UPDATE sessions SET {sets} WHERE id = $1 RETURNING *",
                                session_id, *fields.values())
    return _session_dict(r) if r else {"ok": False, "error": "not found"}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int, purge_data: bool = True):
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"ok": False, "error": "not found"}
        if r["ended_at"] is None:
            return {"ok": False, "error": "session is recording — cannot delete a live session"}
        deleted_rows = 0
        if purge_data:
            for table in ("patient_numerics", "patient_waveforms"):
                res = await conn.execute(f"DELETE FROM {table} WHERE time >= $1 AND time <= $2",
                                         r["started_at"], r["ended_at"])
                deleted_rows += int(res.split()[-1])
        await conn.execute("DELETE FROM sessions WHERE id = $1", session_id)
    return {"ok": True, "deleted_data_rows": deleted_rows}

@app.get("/api/sessions/{session_id}/data")
async def session_data(session_id: int, agg: str = "auto", max_raw_minutes: int = 15):
    """Session data for chart replay. `agg` selects the resolution:
      raw   — every sample
      1min  — 1-minute averages (waveforms from the continuous aggregate)
      5min  — 5-minute averages (re-bucketed on the fly from the 1-min aggregate)
      auto  — raw for spans <= max_raw_minutes, else 1-min (default; back-compat)
    Numerics are bucketed to the same width for 1min/5min; raw otherwise."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)
        span_min = (end - r["started_at"]).total_seconds() / 60
        # Resolve the effective waveform bucket: explicit for 1min/5min, span-based for auto.
        # asyncpg encodes an interval param from a timedelta (a str raises DataError).
        bucket_td = {"1min": timedelta(minutes=1), "5min": timedelta(minutes=5)}.get(agg)
        # Any request that didn't resolve to an explicit bucket (raw, auto, OR an
        # unknown/typo'd agg) downshifts to 1-min beyond max_raw_minutes, so no
        # request — UI, direct, or malformed — can pull millions of raw rows into
        # one response (an OOM/DoS of the worker). aggregated_waveforms reports it.
        if bucket_td is None and span_min > max_raw_minutes:
            bucket_td = timedelta(minutes=1)
        aggregated = bucket_td is not None

        # Numerics: bucket them to match an explicit 1min/5min request; raw otherwise.
        if agg in ("1min", "5min"):
            numerics = await conn.fetch(
                "SELECT time_bucket($3, time) AS time, physio_id, avg(value) AS value "
                "FROM patient_numerics WHERE time BETWEEN $1 AND $2 GROUP BY 1, physio_id ORDER BY 1",
                r["started_at"], end, bucket_td)
        else:
            numerics = await conn.fetch(
                "SELECT time, physio_id, value FROM patient_numerics WHERE time BETWEEN $1 AND $2 ORDER BY time",
                r["started_at"], end)

        # Waveforms: 5min re-buckets the 1-min aggregate; 1min/auto read it directly; raw reads samples.
        if agg == "5min":
            waveforms = await conn.fetch(
                "SELECT time_bucket('5 minutes', bucket) AS time, physio_id, avg(avg_value) AS value "
                "FROM patient_waveforms_1min WHERE bucket BETWEEN $1 AND $2 GROUP BY 1, physio_id ORDER BY 1",
                r["started_at"], end)
        elif aggregated:
            waveforms = await conn.fetch(
                "SELECT bucket AS time, physio_id, avg_value AS value FROM patient_waveforms_1min "
                "WHERE bucket BETWEEN $1 AND $2 ORDER BY bucket", r["started_at"], end)
        else:
            waveforms = await conn.fetch(
                "SELECT time, physio_id, value FROM patient_waveforms WHERE time BETWEEN $1 AND $2 ORDER BY time, ctid",
                r["started_at"], end)
    fmt = lambda rows: [{"time": x["time"].timestamp(), "physio_id": x["physio_id"], "value": float(x["value"])} for x in rows]
    return {"session": _session_dict(r), "aggregated_waveforms": aggregated,
            "numerics": fmt(numerics), "waveforms": fmt(waveforms)}

def _parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False

EXPORT_BATCH = 50_000  # rows per cursor prefetch / parquet batch

def _session_package_dir(r, deidentify: bool = False) -> Path:
    if deidentify:
        # No label (user free-text, may carry PHI) and no date in the name.
        return SESSIONS_DIR / f"deid-session-{r['id']}"
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in (r["label"] or "session"))[:60].strip("-") or "session"
    return SESSIONS_DIR / f"{r['started_at'].strftime('%Y%m%d-%H%M')}_{r['id']}_{slug}"

async def _export_session_files(session_id: int, deidentify: bool = False,
                                base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Write session.json + numerics/waveforms CSV (and Parquet when pyarrow is
    present). Rows are streamed from the DB with a cursor in fixed-size batches,
    so multi-GB sessions export with flat memory. base_dir overrides SESSIONS_DIR
    (used by download-all to export into a throwaway temp dir).

    deidentify=True produces a share-safe package: timestamps become seconds
    relative to session start (no absolute wall-clock date/time), and the
    user-entered label/subject_code/notes + device context are stripped."""
    pa = pq = None
    if _parquet_available():
        import pyarrow as pa
        import pyarrow.parquet as pq

    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"ok": False, "error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)
        t0 = r["started_at"]
        time_col = "time_rel_s" if deidentify else "time_utc"
        rel = lambda t: round((t - t0).total_seconds(), 6)

        out = _session_package_dir(r, deidentify)
        if base_dir is not None:
            out = base_dir / out.name
        out.mkdir(parents=True, exist_ok=True)

        files, counts = [], {}
        for name, table in (("numerics", "patient_numerics"), ("waveforms", "patient_waveforms")):
            count = 0
            writer = None
            if pq:
                ttype = pa.float64() if deidentify else pa.timestamp("us", tz="UTC")
                schema = pa.schema([(time_col, ttype), ("physio_id", pa.string()), ("value", pa.float64())])
                writer = pq.ParquetWriter(out / f"{name}.parquet", schema)
            # Waveform frames share one millisecond stamp; ctid keeps intra-frame
            # samples in the order VSCapture wrote them (see EDF export note).
            order = "time, ctid" if table == "patient_waveforms" else "time"
            with open(out / f"{name}.csv", "w") as f:
                f.write(f"{time_col},physio_id,value\n")
                batch = []
                async with conn.transaction():
                    async for x in conn.cursor(
                            f"SELECT time, physio_id, value FROM {table} WHERE time BETWEEN $1 AND $2 ORDER BY {order}",
                            t0, end, prefetch=EXPORT_BATCH):
                        tcell = rel(x['time']) if deidentify else x['time'].isoformat()
                        f.write(f"{tcell},{x['physio_id']},{x['value']}\n")
                        count += 1
                        if writer:
                            batch.append(x)
                            if len(batch) >= EXPORT_BATCH:
                                writer.write_table(pa.table(
                                    {time_col: [(rel(b["time"]) if deidentify else b["time"]) for b in batch],
                                     "physio_id": [b["physio_id"] for b in batch],
                                     "value": [float(b["value"]) for b in batch]}, schema=schema))
                                batch = []
                if writer:
                    if batch:
                        writer.write_table(pa.table(
                            {time_col: [(rel(b["time"]) if deidentify else b["time"]) for b in batch],
                             "physio_id": [b["physio_id"] for b in batch],
                             "value": [float(b["value"]) for b in batch]}, schema=schema))
                    writer.close()
                    files.append(f"{name}.parquet")
            files.append(f"{name}.csv")
            counts[name] = count
        quality = await _compute_quality(conn, t0, end)
        # Retention truncation: if the earliest surviving row is later than the
        # session start, part of the session has aged out of the DB (retention
        # drops whole chunks oldest-first). Flag it so the package isn't silently
        # mistaken for the complete recording.
        min_surviving = await conn.fetchval(
            "SELECT min(t) FROM ("
            " SELECT min(time) AS t FROM patient_numerics WHERE time BETWEEN $1 AND $2"
            " UNION ALL SELECT min(time) FROM patient_waveforms WHERE time BETWEEN $1 AND $2) q",
            t0, end)
        # Threshold well above normal start-up / export-interval latency (so a
        # complete session isn't mislabelled) but far below the retention window
        # (a truncated session's first surviving row is hours after start).
        partial = bool(min_surviving and (min_surviving - t0).total_seconds() > 600.0)
        retained_from_s = rel(min_surviving) if (partial and min_surviving) else 0.0

    # The quality block carries absolute epoch first_sample/last_sample. In a
    # de-identified package those would recover the exact wall-clock recording
    # time, so relativize them to seconds-from-start like every other timestamp.
    if deidentify:
        t0_epoch = t0.timestamp()
        for grp in ("waveforms", "numerics"):
            for item in quality.get(grp, []):
                for k in ("first_sample", "last_sample"):
                    if item.get(k) is not None:
                        item[k] = round(item[k] - t0_epoch, 6)

    if deidentify:
        meta = {"id": r["id"], "deidentified": True, "duration_s": rel(end),
                "time_format": "seconds from session start"}
    else:
        meta = {**_session_dict(r), "time_format": "ISO 8601 UTC"}
    with open(out / "session.json", "w") as f:
        json.dump({**meta, "exported_at": datetime.now(timezone.utc).isoformat(),
                   "partial": partial, "retained_from_s": retained_from_s,
                   "numeric_rows": counts["numerics"], "waveform_rows": counts["waveforms"],
                   "quality": quality}, f, indent=2)
    files.insert(0, "session.json")
    return {"ok": True, "path": str(out), "files": files, "deidentified": deidentify,
            "partial": partial, "numeric_rows": counts["numerics"], "waveform_rows": counts["waveforms"]}

@app.post("/api/sessions/{session_id}/export")
async def export_session(session_id: int, deidentify: bool = False):
    return await _export_session_files(session_id, deidentify)

@app.get("/api/sessions/{session_id}/download")
async def download_session(session_id: int, deidentify: bool = False):
    """Full data package as a zip, streamed — the browser saves it natively and
    GB-scale packages never have to fit in memory (fresh export to disk, then a
    chunked streaming zip of the directory). deidentify=1 → share-safe package
    (relative timestamps, stripped label/subject/notes)."""
    result = await _export_session_files(session_id, deidentify)
    if not result.get("ok"):
        return result
    from zipstream import ZipStream  # zipstream-ng
    import zipfile as _zf
    out = Path(result["path"])
    zs = ZipStream.from_path(out, compress_type=_zf.ZIP_DEFLATED, compress_level=1)
    return StreamingResponse(iter(zs), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{out.name}.zip"'})

@app.get("/api/sessions/download-all")
async def download_all_sessions(deidentify: bool = False):
    """One zip of every session that still has data, each fresh-exported into a
    throwaway temp dir and only that dir is zipped. This honours `deidentify`
    for the whole bundle (so it can't silently leak PHI), and avoids both
    sweeping stale/leftover folders out of SESSIONS_DIR and clobbering existing
    complete packages with retention-truncated re-exports. The temp dir is
    removed once streaming finishes."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, started_at, ended_at FROM sessions ORDER BY id")
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=str(SESSIONS_DIR), prefix="download-all-"))
    try:
        for r in rows:
            end = r["ended_at"] or datetime.now(timezone.utc)
            async with db_pool.acquire() as conn:
                has_data = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM patient_numerics WHERE time BETWEEN $1 AND $2 LIMIT 1)"
                    " OR EXISTS (SELECT 1 FROM patient_waveforms WHERE time BETWEEN $1 AND $2 LIMIT 1)",
                    r["started_at"], end)
            if has_data:
                await _export_session_files(r["id"], deidentify, base_dir=tmp)
        from zipstream import ZipStream  # zipstream-ng
        import zipfile as _zf
        zs = ZipStream(compress_type=_zf.ZIP_DEFLATED, compress_level=1)
        for child in sorted(tmp.iterdir()):
            zs.add_path(child, arcname=child.name)  # session folders at the zip root, no temp-dir prefix
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        suffix = "-deid" if deidentify else ""

        def stream_then_cleanup():
            try:
                yield from zs
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        return StreamingResponse(stream_then_cleanup(), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="vscc-sessions-{stamp}{suffix}.zip"'})
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

@app.post("/api/sessions")
async def create_session(payload: Optional[Dict[str, Any]] = None):
    """Manual named 'Start session': closes any open session at the boundary and
    starts recording into a fresh one with optional metadata (label, subject_code,
    notes). Incoming data keeps flowing; only the session metadata boundary moves."""
    payload = payload or {}
    now = datetime.now(timezone.utc)
    label = payload.get("label") or _default_session_label(now)
    subject = str(payload.get("subject_code", ""))
    notes = str(payload.get("notes", ""))
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE sessions SET ended_at = GREATEST($1, started_at) WHERE ended_at IS NULL", now)
        r = await conn.fetchrow(
            "INSERT INTO sessions (label, subject_code, notes, started_at) VALUES ($1, $2, $3, $4) RETURNING *",
            label, subject, notes, now)
    return _session_dict(r)

@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: int):
    """Explicit 'Stop recording': closes an open session now. Pairs with the
    named start above; auto-close on data silence still applies as a fallback."""
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"ok": False, "error": "not found"}
        if r["ended_at"] is not None:
            return {"ok": False, "error": "session already stopped"}
        r = await conn.fetchrow(
            "UPDATE sessions SET ended_at = GREATEST($1, started_at) WHERE id = $2 RETURNING *",
            now, session_id)
    return _session_dict(r)

@app.get("/api/sessions/{session_id}/signals")
async def session_signals(session_id: int):
    """Distinct physio_ids in the session's range — drives the UI's metric legend."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)
        nums = await conn.fetch(
            "SELECT DISTINCT physio_id FROM patient_numerics WHERE time BETWEEN $1 AND $2 ORDER BY 1",
            r["started_at"], end)
        waves = await conn.fetch(
            "SELECT DISTINCT physio_id FROM patient_waveforms WHERE time BETWEEN $1 AND $2 ORDER BY 1",
            r["started_at"], end)
    return {"numerics": [x["physio_id"] for x in nums],
            "waveforms": [x["physio_id"] for x in waves]}

# --- 6. Capture quality / loss statistics ---
#
# Waveform timestamps arrive frame-stamped: a whole UDP frame of samples shares
# one millisecond stamp (e.g. ECG bursts of 192 rows per ms), so sub-second
# spacing carries no timing information. Per-second sample counts ARE stable
# (ECG 768/s, Pleth 128/s, Resp 64/s on the MP50), so the nominal rate is the
# statistical mode of per-second counts and loss is measured against
# rate x active span. Numerics get plain counts only — many are intermittent
# by design (NIBP only on inflation), so "expected" would be meaningless.

QUALITY_WAVE_SQL = """
WITH b AS (
    SELECT physio_id, time_bucket('1 second', time) AS sec, count(*) AS n
    FROM patient_waveforms WHERE time BETWEEN $1 AND $2
    GROUP BY 1, 2
), g AS (
    SELECT physio_id, sec, n,
           EXTRACT(EPOCH FROM sec - lag(sec) OVER (PARTITION BY physio_id ORDER BY sec)) AS step
    FROM b
)
SELECT physio_id,
       sum(n)::bigint                                     AS samples,
       count(*)::bigint                                   AS seconds_with_data,
       -- 90th percentile of per-second counts: equals the nominal rate for any
       -- normal capture (most seconds are full) but, unlike mode(), is not thrown
       -- off by partial-second counts dominating a very short capture.
       percentile_disc(0.9) WITHIN GROUP (ORDER BY n)     AS rate_hz,
       min(sec)                                           AS first_sec,
       max(sec)                                           AS last_sec,
       count(*) FILTER (WHERE step > 1)                   AS gap_count,
       COALESCE(max(step) FILTER (WHERE step > 1) - 1, 0) AS longest_gap_s
FROM g GROUP BY physio_id ORDER BY physio_id
"""

async def _compute_quality(conn, started_at, end) -> Dict[str, Any]:
    waves = []
    for r in await conn.fetch(QUALITY_WAVE_SQL, started_at, end):
        span_s = int((r["last_sec"] - r["first_sec"]).total_seconds()) + 1
        expected = int(r["rate_hz"]) * span_s
        waves.append({
            "physio_id": r["physio_id"],
            "rate_hz": int(r["rate_hz"]),
            "samples": int(r["samples"]),
            "expected_samples": expected,
            "missing_samples": max(0, expected - int(r["samples"])),
            "completeness_pct": round(min(100.0, 100.0 * int(r["samples"]) / expected), 2) if expected else None,
            "gap_count": int(r["gap_count"]),
            "longest_gap_s": float(r["longest_gap_s"]),
            "first_sample": r["first_sec"].timestamp(),
            "last_sample": r["last_sec"].timestamp() + 1,
        })
    nums = await conn.fetch(
        "SELECT physio_id, count(*) AS samples, min(time) AS first, max(time) AS last "
        "FROM patient_numerics WHERE time BETWEEN $1 AND $2 GROUP BY physio_id ORDER BY physio_id",
        started_at, end)
    return {"waveforms": waves,
            "numerics": [{"physio_id": x["physio_id"], "samples": int(x["samples"]),
                          "first_sample": x["first"].timestamp(), "last_sample": x["last"].timestamp()}
                         for x in nums]}

@app.get("/api/sessions/{session_id}/quality")
async def session_quality(session_id: int):
    """Loss statistics: per-waveform expected vs actual samples, gaps, rates."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)
        quality = await _compute_quality(conn, r["started_at"], end)
    return {"session": _session_dict(r), **quality}

# --- 7. EDF export ---
#
# One EDF channel per waveform signal. Because timestamps are frame-stamped
# (see section 6), samples are placed on a per-second grid at the signal's
# nominal rate: each second's samples fill that second's slot in arrival
# order, short seconds are zero-padded, empty seconds stay zero — so channel
# alignment never drifts and gaps are preserved in place. Values are scaled
# symmetrically (digital 0 == physical 0) so the zero gap-filler is honest.
# Channels stream from DB cursors into sparse temp files, then interleave
# into EDF records — memory stays flat for GB sessions.

EDF_DIG_MAX = 32767

def _edf_ascii(value, width: int) -> bytes:
    """Fixed-width space-padded ASCII header field."""
    s = str(value)[:width]
    return s.ljust(width).encode("ascii", "replace")

def _edf_num(value: float, width: int) -> bytes:
    """Numeric header field squeezed into width chars."""
    for fmt in ("%g", "%.5g", "%.4g", "%.3g", "%.2g"):
        s = fmt % value
        if len(s) <= width:
            return s.ljust(width).encode("ascii")
    return ("%.1e" % value)[:width].ljust(width).encode("ascii")

def _edf_flush_second(f, sec_idx: Optional[int], n_records: int, spr: int, buf: List[int]):
    if sec_idx is None or not (0 <= sec_idx < n_records):
        return
    vals = (buf + [0] * spr)[:spr]  # pad short seconds, trim overfull ones
    f.seek(sec_idx * spr * 2)
    f.write(array("h", vals).tobytes())

async def _export_session_edf(session_id: int) -> Dict[str, Any]:
    """Build <package>/waveforms.edf, regenerated fresh on every call."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"ok": False, "error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)

        chans = []
        for q in await conn.fetch(QUALITY_WAVE_SQL, r["started_at"], end):
            lim = await conn.fetchrow(
                "SELECT min(value) AS vmin, max(value) AS vmax FROM patient_waveforms "
                "WHERE physio_id = $1 AND time BETWEEN $2 AND $3",
                q["physio_id"], r["started_at"], end)
            pabs = max(abs(lim["vmin"]), abs(lim["vmax"]), 1e-9)
            chans.append({"physio_id": q["physio_id"], "spr": int(q["rate_hz"]),
                          "first_sec": q["first_sec"], "last_sec": q["last_sec"], "pabs": pabs})
        if not chans:
            return {"ok": False, "error": "session has no waveform data"}

        t0 = min(c["first_sec"] for c in chans)
        t0_epoch = int(t0.timestamp())
        n_records = int(max(c["last_sec"] for c in chans).timestamp()) - t0_epoch + 1

        out = _session_package_dir(r)
        out.mkdir(parents=True, exist_ok=True)
        edf_path = out / "waveforms.edf"

        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(SESSIONS_DIR)) as tmpdir:
            for i, ch in enumerate(chans):
                scale = EDF_DIG_MAX / ch["pabs"]
                ch["tmp"] = Path(tmpdir) / f"ch{i}.raw"
                with open(ch["tmp"], "wb") as f:
                    f.truncate(n_records * ch["spr"] * 2)  # sparse: unwritten == digital 0 == physical 0
                    sec_idx, buf = None, []
                    async with conn.transaction():
                        # A whole VSCapture frame shares one millisecond stamp, so
                        # ORDER BY time alone leaves intra-frame sample order to the
                        # planner. ctid breaks ties in insertion order == the order
                        # VSCapture wrote the samples (rows are never updated).
                        async for x in conn.cursor(
                                "SELECT time, value FROM patient_waveforms "
                                "WHERE physio_id = $1 AND time BETWEEN $2 AND $3 ORDER BY time, ctid",
                                ch["physio_id"], r["started_at"], end, prefetch=EXPORT_BATCH):
                            idx = int(x["time"].timestamp()) - t0_epoch
                            if idx != sec_idx:
                                _edf_flush_second(f, sec_idx, n_records, ch["spr"], buf)
                                sec_idx, buf = idx, []
                            v = int(round(x["value"] * scale))
                            buf.append(max(-EDF_DIG_MAX, min(EDF_DIG_MAX, v)))
                    _edf_flush_second(f, sec_idx, n_records, ch["spr"], buf)

            ns = len(chans)
            with open(edf_path, "wb") as o:
                o.write(_edf_ascii("0", 8))                                       # version
                o.write(_edf_ascii(r["subject_code"] or "X", 80))                 # patient (anonymous)
                o.write(_edf_ascii(f"VSCC session {r['id']} (times UTC) {r['label']}", 80))
                o.write(_edf_ascii(t0.strftime("%d.%m.%y"), 8))
                o.write(_edf_ascii(t0.strftime("%H.%M.%S"), 8))
                o.write(_edf_ascii(256 * (ns + 1), 8))                            # header bytes
                o.write(_edf_ascii("", 44))                                       # reserved (plain EDF)
                o.write(_edf_ascii(n_records, 8))
                o.write(_edf_ascii(1, 8))                                         # record duration, s
                o.write(_edf_ascii(ns, 4))
                for c in chans:
                    pid = c["physio_id"]
                    o.write(_edf_ascii(pid[4:] if pid.startswith("NOM_") else pid, 16))
                for c in chans: o.write(_edf_ascii("", 80))                       # transducer
                for c in chans: o.write(_edf_ascii("", 8))                        # physical dimension (monitor units)
                for c in chans: o.write(_edf_num(-c["pabs"], 8))
                for c in chans: o.write(_edf_num(c["pabs"], 8))
                for c in chans: o.write(_edf_ascii(-EDF_DIG_MAX, 8))
                for c in chans: o.write(_edf_ascii(EDF_DIG_MAX, 8))
                for c in chans: o.write(_edf_ascii("", 80))                       # prefiltering
                for c in chans: o.write(_edf_ascii(c["spr"], 8))
                for c in chans: o.write(_edf_ascii("", 32))                       # reserved
                handles = [open(c["tmp"], "rb") for c in chans]
                try:
                    for _ in range(n_records):
                        for h, c in zip(handles, chans):
                            o.write(h.read(c["spr"] * 2))
                finally:
                    for h in handles:
                        h.close()

    return {"ok": True, "path": str(edf_path), "n_records": n_records,
            "start_utc": t0.isoformat(),
            "channels": [{"physio_id": c["physio_id"], "rate_hz": c["spr"]} for c in chans]}

@app.get("/api/sessions/{session_id}/edf")
async def download_session_edf(session_id: int):
    """Waveforms as EDF for EEG/biosignal toolchains (EDFbrowser, MNE, ...)."""
    result = await _export_session_edf(session_id)
    if not result.get("ok"):
        return result
    path = Path(result["path"])

    def _iter():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_iter(), media_type="application/octet-stream",
                             headers={"Content-Disposition": f'attachment; filename="{path.parent.name}.edf"'})

# --- 9. Annotations: timestamped event markers ---

def _annotation_dict(r) -> Dict[str, Any]:
    return {"id": r["id"], "time": r["time"].timestamp(), "label": r["label"],
            "session_id": r["session_id"]}

@app.post("/api/annotations")
async def create_annotation(payload: Dict[str, Any]):
    """Add an event marker. `time` is epoch seconds (default: now). Optionally
    tie it to a session_id (deleted with the session)."""
    label = str(payload.get("label", "")).strip()
    if not label:
        return {"ok": False, "error": "label is required"}
    # Validate client-supplied time/session_id up front so malformed input returns
    # {ok:false} instead of surfacing as an unhandled 500.
    ts = payload.get("time")
    try:
        when = datetime.fromtimestamp(float(ts), timezone.utc) if ts is not None else datetime.now(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return {"ok": False, "error": "time must be epoch seconds"}
    session_id = payload.get("session_id")
    try:
        sid = int(session_id) if session_id is not None else None
    except (ValueError, TypeError):
        return {"ok": False, "error": "session_id must be an integer"}
    try:
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow(
                "INSERT INTO annotations (time, label, session_id) VALUES ($1, $2, $3) RETURNING *",
                when, label[:500], sid)
    except asyncpg.ForeignKeyViolationError:
        return {"ok": False, "error": f"session #{sid} does not exist"}
    return _annotation_dict(r)

@app.get("/api/annotations")
async def list_annotations(session_id: Optional[int] = None,
                           from_ts: Optional[float] = None, to_ts: Optional[float] = None):
    """List event markers, newest first. Filter by session_id or a time window
    (from_ts/to_ts, epoch seconds)."""
    clauses, args = [], []
    if session_id is not None:
        args.append(session_id); clauses.append(f"session_id = ${len(args)}")
    if from_ts is not None:
        args.append(datetime.fromtimestamp(from_ts, timezone.utc)); clauses.append(f"time >= ${len(args)}")
    if to_ts is not None:
        args.append(datetime.fromtimestamp(to_ts, timezone.utc)); clauses.append(f"time <= ${len(args)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM annotations{where} ORDER BY time DESC LIMIT 500", *args)
    return [_annotation_dict(r) for r in rows]

@app.delete("/api/annotations/{annotation_id}")
async def delete_annotation(annotation_id: int):
    async with db_pool.acquire() as conn:
        res = await conn.execute("DELETE FROM annotations WHERE id = $1", annotation_id)
    return {"ok": res.split()[-1] != "0"}
