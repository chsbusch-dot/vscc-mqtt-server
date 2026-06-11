import asyncio
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Union, List, Dict, Any
from fastapi import FastAPI
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

# --- File & Topic Mapping (Corrected Topic Names) ---

FILE_CONFIGS = [
    {
        "path": BASE_DIR / "DataExportVSC.json",
        "type": "json",
        "topic": "mp50/VitalSigns",
        "physio_id": None,
    },
    {
        "path": BASE_DIR / "NOM_ECG_ELEC_POTL_IIWaveExport.csv",
        "type": "csv",
        "topic": "mp50/HF-ECG",
        "physio_id": "NOM_ECG_ELEC_POTL_II",
    },
    {
        "path": BASE_DIR / "NOM_EEG_ELEC_POTL_CRTXWaveExport.csv",
        "type": "csv",
        "topic": "mp50/HF-EEG",
        "physio_id": "NOM_EEG_ELEC_POTL_CRTX",
    },
    {
        "path": BASE_DIR / "NOM_PLETHWaveExport.csv",
        "type": "csv",
        "topic": "mp50/HF-PLETH",
        "physio_id": "NOM_PLETH",
    },
    {
        "path": BASE_DIR / "NOM_RESPWaveExport.csv",
        "type": "csv",
        "topic": "mp50/HF-RESP",
        "physio_id": "NOM_RESP",
    },
]

# Global batch buffers for TimescaleDB
numerics_buffer = []
waveforms_buffer = []
db_pool = None

# --- Utility Functions ---

def parse_vsc_timestamp(raw_time: str) -> Union[datetime, None]:
    if not raw_time: return None
    try: return datetime.strptime(raw_time, "%d-%m-%Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        try: return datetime.strptime(raw_time, "%d-%m-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
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

# --- 3. App Lifespan & FastAPI ---
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
    
    tasks = []
    try:
        async with aiomqtt.Client(MQTT_BROKER, port=MQTT_PORT) as client:
            print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
            tasks.append(asyncio.create_task(batch_inserter()))
            for config in FILE_CONFIGS: tasks.append(asyncio.create_task(tail_file(config, client)))
            print(f"Launched {len(FILE_CONFIGS)} file tailers and 1 DB worker.")
            yield
    except aiomqtt.MqttError as e:
        print(f"CRITICAL: Could not connect to MQTT broker: {e}.")
    finally:
        print("Shutting down... Cancelling background tasks.")
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if db_pool: await db_pool.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/historic/{range_minutes}")
async def get_historic_data(range_minutes: int):
    # Unchanged
    pass
