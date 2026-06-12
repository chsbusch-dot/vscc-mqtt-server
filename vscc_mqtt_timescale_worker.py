import asyncio
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
import shutil
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Union, List, Dict, Any, Optional
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
import asyncpg
import aiomqtt

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
    "NOM_EEG_ELEC_POTL_CRTX": "HF-EEG",
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

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "/sessions"))
DEFAULT_SETTINGS = {
    "retention_hours": "12",
    "session_gap_minutes": "3",  # data silence that closes a session
}

# --- Utility Functions ---

# VSCapture timestamps are wall-clock time of the capture host, not UTC.
MONITOR_TZ = ZoneInfo(os.getenv("MONITOR_TZ", "America/Los_Angeles"))

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
                if waveforms_to_insert:
                    await connection.executemany("INSERT INTO patient_waveforms (time, physio_id, value) VALUES ($1, $2, $3)", waveforms_to_insert)
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
                last_data_time = loop.time()

                while True:
                    line = await f.readline()

                    # EOF reached; reading doesn't block, it just returns ""
                    if not line:
                        current_time = loop.time()
                        
                        # 1. Check for Disconnects (Lead Off)
                        if current_time - last_data_time > WAVEFORM_DISCONNECT_TIMEOUT:
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
                    last_data_time = loop.time()
                    disconnected_sent = False
                    line = line.strip()
                    if not line:
                        continue

                    if file_type == "json":
                        try:
                            data_array = json.loads(line)
                            if isinstance(data_array, dict): data_array = [data_array]
                            for record in data_array:
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
]

async def ensure_schema(pool):
    async with pool.acquire() as connection:
        for stmt in SCHEMA_STATEMENTS:
            try:
                await connection.execute(stmt)
            except Exception as e:
                print(f"Schema bootstrap statement failed (continuing): {e}")
    print("Database schema verified.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
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
    
    tasks = []
    app_started = False
    try:
        while not app_started:
            try:
                async with aiomqtt.Client(MQTT_BROKER, port=MQTT_PORT) as client:
                    print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
                    tasks.append(asyncio.create_task(batch_inserter()))
                    tasks.append(asyncio.create_task(session_manager()))
                    # Numerics: one fixed JSON tailer (carries every parameter PhysioID).
                    tasks.append(asyncio.create_task(tail_file(NUMERICS_CONFIG, client)))
                    # Waveforms: discovered dynamically so new modules stream with no code change.
                    tasks.append(asyncio.create_task(wave_discovery_loop(client, set())))
                    print("Launched numerics tailer + waveform auto-discovery + DB batch worker.")
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/historic/{range_minutes}")
async def get_historic_data(range_minutes: int):
    """
    The React UI hits this when zooming out past the live window.
    range_minutes is coerced to int by FastAPI, so it is safe to inline.
    """
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
                        f"Session {now.strftime('%Y-%m-%d %H:%M')}", last_data_time)
                    print(f"[Sessions] Opened session #{row['id']}")
                elif now - last_data_time > gap and open_row is not None:
                    await conn.execute("UPDATE sessions SET ended_at = GREATEST($1, started_at) WHERE id=$2", last_data_time, open_row["id"])
                    print(f"[Sessions] Closed session #{open_row['id']} (data gap)")
        except Exception as e:
            print(f"[Sessions] manager error: {e}")

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
async def session_data(session_id: int, max_raw_minutes: int = 15):
    """Session data for chart replay. Waveforms come back raw for short sessions,
    1-minute aggregated (avg) beyond max_raw_minutes to keep payloads sane."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)
        span_min = (end - r["started_at"]).total_seconds() / 60
        numerics = await conn.fetch(
            "SELECT time, physio_id, value FROM patient_numerics WHERE time BETWEEN $1 AND $2 ORDER BY time",
            r["started_at"], end)
        aggregated = span_min > max_raw_minutes
        if aggregated:
            waveforms = await conn.fetch(
                "SELECT bucket AS time, physio_id, avg_value AS value FROM patient_waveforms_1min "
                "WHERE bucket BETWEEN $1 AND $2 ORDER BY bucket", r["started_at"], end)
        else:
            waveforms = await conn.fetch(
                "SELECT time, physio_id, value FROM patient_waveforms WHERE time BETWEEN $1 AND $2 ORDER BY time",
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

async def _export_session_files(session_id: int) -> Dict[str, Any]:
    """Write session.json + numerics/waveforms CSV (and Parquet when pyarrow is
    present) under SESSIONS_DIR. Rows are streamed from the DB with a cursor in
    fixed-size batches, so multi-GB sessions export with flat memory."""
    pa = pq = None
    if _parquet_available():
        import pyarrow as pa
        import pyarrow.parquet as pq

    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not r:
            return {"ok": False, "error": "not found"}
        end = r["ended_at"] or datetime.now(timezone.utc)

        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in (r["label"] or "session"))[:60].strip("-") or "session"
        out = SESSIONS_DIR / f"{r['started_at'].strftime('%Y%m%d-%H%M')}_{r['id']}_{slug}"
        out.mkdir(parents=True, exist_ok=True)

        files, counts = [], {}
        for name, table in (("numerics", "patient_numerics"), ("waveforms", "patient_waveforms")):
            count = 0
            writer = None
            if pq:
                schema = pa.schema([("time_utc", pa.timestamp("us", tz="UTC")),
                                    ("physio_id", pa.string()), ("value", pa.float64())])
                writer = pq.ParquetWriter(out / f"{name}.parquet", schema)
            with open(out / f"{name}.csv", "w") as f:
                f.write("time_utc,physio_id,value\n")
                batch = []
                async with conn.transaction():
                    async for x in conn.cursor(
                            f"SELECT time, physio_id, value FROM {table} WHERE time BETWEEN $1 AND $2 ORDER BY time",
                            r["started_at"], end, prefetch=EXPORT_BATCH):
                        f.write(f"{x['time'].isoformat()},{x['physio_id']},{x['value']}\n")
                        count += 1
                        if writer:
                            batch.append(x)
                            if len(batch) >= EXPORT_BATCH:
                                writer.write_table(pa.table(
                                    {"time_utc": [b["time"] for b in batch],
                                     "physio_id": [b["physio_id"] for b in batch],
                                     "value": [float(b["value"]) for b in batch]}, schema=schema))
                                batch = []
                if writer:
                    if batch:
                        writer.write_table(pa.table(
                            {"time_utc": [b["time"] for b in batch],
                             "physio_id": [b["physio_id"] for b in batch],
                             "value": [float(b["value"]) for b in batch]}, schema=schema))
                    writer.close()
                    files.append(f"{name}.parquet")
            files.append(f"{name}.csv")
            counts[name] = count

    with open(out / "session.json", "w") as f:
        json.dump({**_session_dict(r), "exported_at": datetime.now(timezone.utc).isoformat(),
                   "numeric_rows": counts["numerics"], "waveform_rows": counts["waveforms"],
                   "time_format": "ISO 8601 UTC"}, f, indent=2)
    files.insert(0, "session.json")
    return {"ok": True, "path": str(out), "files": files,
            "numeric_rows": counts["numerics"], "waveform_rows": counts["waveforms"]}

@app.post("/api/sessions/{session_id}/export")
async def export_session(session_id: int):
    return await _export_session_files(session_id)

@app.get("/api/sessions/{session_id}/download")
async def download_session(session_id: int):
    """Full data package as a zip, streamed — the browser saves it natively and
    GB-scale packages never have to fit in memory (fresh export to disk, then a
    chunked streaming zip of the directory)."""
    result = await _export_session_files(session_id)
    if not result.get("ok"):
        return result
    from zipstream import ZipStream  # zipstream-ng
    import zipfile as _zf
    out = Path(result["path"])
    zs = ZipStream.from_path(out, compress_type=_zf.ZIP_DEFLATED, compress_level=1)
    return StreamingResponse(iter(zs), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{out.name}.zip"'})

@app.post("/api/sessions")
async def create_session(payload: Optional[Dict[str, Any]] = None):
    """Manual 'New Session': closes any open session at the boundary and starts
    recording into a fresh one (incoming data keeps flowing; only the session
    metadata boundary moves)."""
    now = datetime.now(timezone.utc)
    label = (payload or {}).get("label") or f"Session {now.strftime('%Y-%m-%d %H:%M')}"
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE sessions SET ended_at = GREATEST($1, started_at) WHERE ended_at IS NULL", now)
        r = await conn.fetchrow(
            "INSERT INTO sessions (label, started_at) VALUES ($1, $2) RETURNING *", label, now)
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
