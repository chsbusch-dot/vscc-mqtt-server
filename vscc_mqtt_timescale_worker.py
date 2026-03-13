import asyncio
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Union
from dateutil import parser
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
import asyncpg
import aiomqtt

# --- Configuration ---

BASE_DIR = Path(__file__).resolve().parent
FILE_PATH = Path(os.getenv("VSC_FILE_PATH", BASE_DIR / "VSCapture" / "DataExportVSC.json"))
MQTT_BROKER = os.getenv("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "telemetry/mp50")
DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:password@127.0.0.1:5432/telemetry")

# Global batch buffers for TimescaleDB
numerics_buffer = []
waveforms_buffer = []
db_pool = None

# Heuristic to separate fast waveforms from slow numerics
WAVEFORM_KEYWORDS = ["PLETH", "EEG", "WAVE", "RESP"]

def parse_vsc_timestamp(raw_time: str) -> Union[datetime, None]:
    """
    Parses VSCapture timestamp string into a UTC datetime object.
    Supports formats: 'DD-MM-YYYY HH:MM:SS.f' and 'DD-MM-YYYY HH:MM:SS'
    """
    if not raw_time:
        return None
    try:
        # Try parsing with microseconds first
        return datetime.strptime(raw_time, "%d-%m-%Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        # Fallback for timestamps without milliseconds
        return datetime.strptime(raw_time, "%d-%m-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

# --- 1. The Database Batch Worker ---
async def batch_inserter():
    """Wakes up every 1 second, drains the buffer, and executes a bulk insert."""
    global numerics_buffer, waveforms_buffer
    while True:
        await asyncio.sleep(1.0)
        
        # Quickly swap the buffers so the tailer can keep appending
        numerics_to_insert = numerics_buffer
        numerics_buffer = []
        
        waveforms_to_insert = waveforms_buffer
        waveforms_buffer = []

        async with db_pool.acquire() as connection:
            if numerics_to_insert:
                try:
                    await connection.executemany('''
                        INSERT INTO patient_numerics (time, physio_id, value)
                        VALUES ($1, $2, $3)
                    ''', numerics_to_insert)
                    print(f"Batched {len(numerics_to_insert)} numerics to TimescaleDB.")
                except Exception as e:
                    print(f"Numerics insert error: {e}")

            if waveforms_to_insert:
                try:
                    await connection.executemany('''
                        INSERT INTO patient_waveforms (time, physio_id, value)
                        VALUES ($1, $2, $3)
                    ''', waveforms_to_insert)
                    print(f"Batched {len(waveforms_to_insert)} waveforms to TimescaleDB.")
                except Exception as e:
                    print(f"Waveforms insert error: {e}")

# --- 2. The Async File Tailer ---
async def tail_and_process():
    print(f"Waiting for {FILE_PATH}...")
    while not FILE_PATH.exists():
        await asyncio.sleep(1)

    print("File found. Tailing live data...")
    last_size = 0

    async with aiofiles.open(FILE_PATH, mode="r") as f:
        # Jump to the end of the file for live streaming
        await f.seek(0, os.SEEK_END)
        last_size = os.path.getsize(FILE_PATH)

        while True:
            try:
                async with aiomqtt.Client(MQTT_BROKER, port=MQTT_PORT) as client:
                    print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
                    while True:
                        current_size = os.path.getsize(FILE_PATH)
                        
                        if current_size < last_size:
                            print("File truncated. Resetting...")
                            await f.seek(0, 0)
                            last_size = 0

                        line = await f.readline()
                        
                        if not line:
                            await asyncio.sleep(0.05)
                            continue
                        
                        last_size = current_size
                        line = line.strip()
                        
                        if not line:
                            continue
                        
                        try:
                            # VSCapture writes arrays of JSON objects per line
                            data_array = json.loads(line)
                            
                            # Handle case where it's a single dict instead of a list
                            if isinstance(data_array, dict):
                                data_array = [data_array]
                            
                            for record in data_array:
                                val_str = record.get("Value", "-")
                                
                                # Skip empty/invalid vital signs directly from the device
                                if val_str == "-":
                                    continue
                                
                                try:
                                    value_float = float(val_str)
                                except ValueError:
                                    continue

                                physio_id = record.get("PhysioID")
                                raw_time = record.get("SystemLocalTime", record.get("Timestamp"))
                                parsed_time = parse_vsc_timestamp(raw_time)

                                if not physio_id or not parsed_time:
                                    continue

                                device_id = record.get("DeviceID", "mp50")

                                # 1. Prepare flattened payload for React via MQTT
                                flat_payload = {
                                    "time": parsed_time.timestamp(), # Send as float SECONDS for SciChart
                                    "physio_id": physio_id,
                                    "value": value_float,
                                    "device_id": device_id
                                }
                                
                                # Blast to vscc-emqx (Async)
                                await client.publish(MQTT_TOPIC, payload=json.dumps(flat_payload), qos=0)

                                # 2. Append to database buffer (time, physio, value) - DeviceID removed for new schema
                                if any(k in physio_id for k in WAVEFORM_KEYWORDS):
                                    waveforms_buffer.append((parsed_time, physio_id, value_float))
                                else:
                                    numerics_buffer.append((parsed_time, physio_id, value_float))
                                
                        except json.JSONDecodeError:
                            print("Skipped malformed JSON line.")
                        except Exception as e:
                            print(f"Error processing line: {e}")
            except aiomqtt.MqttError as e:
                print(f"MQTT Connection lost: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

# --- 3. App Lifespan & FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    # Connect to TimescaleDB
    while True:
        try:
            db_pool = await asyncpg.create_pool(DB_DSN)
            print("Connected to TimescaleDB.")
            break
        except Exception as e:
            print(f"Database connection failed: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
    
    # Launch background tasks
    task_tail = asyncio.create_task(tail_and_process())
    task_db = asyncio.create_task(batch_inserter())
    
    yield
    
    # Cleanup
    task_tail.cancel()
    task_db.cancel()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. Historic REST API for the Client UI ---
@app.get("/api/historic/{range_minutes}")
async def get_historic_data(range_minutes: int):
    """
    The React UI will hit this when zooming out past 15 minutes.
    We route the query to the downsampled materialized views.
    """
    # Updated for new schema: querying patient_numerics directly
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

@app.post("/api/live")
async def receive_live_data(records: list[dict]):
    """
    Receives live JSON data via HTTP POST from VSCapture and publishes it to MQTT.
    """
    try:
        async with aiomqtt.Client(MQTT_BROKER, port=MQTT_PORT) as client:
            for record in records:
                val_str = record.get("Value", "-")
                if val_str == "-":
                    continue
                
                try:
                    value_float = float(val_str)
                except ValueError:
                    continue
                
                physio_id = record.get("PhysioID")
                raw_time = record.get("SystemLocalTime", record.get("Timestamp"))
                parsed_time = parse_vsc_timestamp(raw_time)
                
                if not physio_id or not parsed_time:
                    continue

                flat_payload = {
                    "time": parsed_time.timestamp(), # Send as float SECONDS for SciChart
                    "physio_id": physio_id,
                    "value": value_float,
                    "device_id": record.get("DeviceID", "mp50")
                }
                await client.publish(MQTT_TOPIC, payload=json.dumps(flat_payload), qos=0)
        return {"status": "ok", "received": len(records)}
    except Exception as e:
        print(f"Error in /api/live: {e}")
        return {"status": "error", "detail": str(e)}
