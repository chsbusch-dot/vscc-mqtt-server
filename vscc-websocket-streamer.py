import asyncio
import os
import json
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
import uvicorn

BASE_DIR = Path(__file__).resolve().parent
# Use absolute path to ensure file is found regardless of where script is run
FILE_PATH = Path(os.getenv("VSC_FILE_PATH", BASE_DIR / "VSCapture" / "DataExportVSC.json"))

# --- 1. Connection Manager (Pub/Sub for WebSockets) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"Client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"Client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        # Broadcast to all connected clients simultaneously
        for connection in self.active_connections.copy():
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"Failed to send to client: {e}")
                self.disconnect(connection)

manager = ConnectionManager()

# --- Helper: Normalize Timestamp ---
def parse_vsc_timestamp(raw_time: str) -> datetime | None:
    """
    Parses VSCapture timestamp string into a UTC datetime object.
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

# --- 2. Single Background File Tailer ---
async def tail_file_and_broadcast():
    print(f"Background worker started. Waiting for {FILE_PATH}...")
    
    while not FILE_PATH.exists():
        await asyncio.sleep(1)

    print(f"Found {FILE_PATH}! Starting tail stream...")
    
    last_size = 0

    # Use aiofiles for true non-blocking disk reads
    async with aiofiles.open(FILE_PATH, mode="r") as f:
        # Jump to the end of the file to start reading live data
        await f.seek(0, os.SEEK_END)
        last_size = os.path.getsize(FILE_PATH)

        while True:
            current_size = os.path.getsize(FILE_PATH)
            
            # Handle file rotation or truncation by VSCapture
            if current_size < last_size:
                print("File truncated. Resetting to beginning...")
                await f.seek(0, 0)
                last_size = 0

            line = await f.readline()
            
            if not line:
                # 20Hz stream = 50ms between points. 0.05 is a highly optimized sleep.
                await asyncio.sleep(0.05) 
                continue
            
            last_size = current_size
            
            line = line.strip()
            if line:
                try:
                    # Parse raw VSCapture JSON
                    data = json.loads(line)
                    if isinstance(data, dict):
                        data = [data]
                    
                    for record in data:
                        physio_id = record.get("PhysioID")
                        val_str = record.get("Value", "-")
                        raw_time = record.get("SystemLocalTime", record.get("Timestamp"))
                        
                        if not physio_id or val_str == "-" or not raw_time:
                            continue
                            
                        parsed_time = parse_vsc_timestamp(raw_time)
                        if parsed_time:
                            # Normalize to standard payload (Epoch Seconds)
                            payload = {
                                "time": parsed_time.timestamp(),
                                "physio_id": physio_id,
                                "value": float(val_str),
                                "device_id": record.get("DeviceID", "mp50")
                            }
                            await manager.broadcast(json.dumps(payload))
                except Exception:
                    continue

# --- 3. App Lifespan (Startup/Shutdown logic) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the tailer in the background as soon as FastAPI boots
    tail_task = asyncio.create_task(tail_file_and_broadcast())
    yield
    # Cleanup when server shuts down
    tail_task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. The Endpoints ---
@app.websocket("/ws/stream")
async def websocket_endpoint(websocket: WebSocket):
    # Connect client to the manager. 
    # The background task will automatically push data to it.
    await manager.connect(websocket)
    try:
        # Keep the connection open indefinitely
        while True:
            # We just wait to receive messages (if the client ever sends any)
            # or wait until the connection drops to trigger the exception.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)

@app.get("/DataExportVSC.json")
async def serve_json_file(start_byte: int = 0):
    """Serves the raw JSON file via standard HTTP GET"""
    if not FILE_PATH.exists():
        return {"error": f"File not found at {FILE_PATH}"}

    async def iterfile():
        async with aiofiles.open(FILE_PATH, mode="rb") as f:
            if start_byte > 0:
                await f.seek(start_byte)
            while True:
                chunk = await f.read(64 * 1024)  # Read 64KB chunks
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(iterfile(), media_type="application/json", headers={"Cache-Control": "no-cache"})

@app.get("/mp50.json")
async def redirect_old_file():
    """Redirect old filename to new filename"""
    return RedirectResponse(url="/DataExportVSC.json")

if __name__ == "__main__":
    # Run the server using uvicorn when script is executed directly
    uvicorn.run(app, host="0.0.0.0", port=8000)
