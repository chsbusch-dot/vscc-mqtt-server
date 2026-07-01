# WebSocket streamer: serves the raw numerics JSON over HTTP and a normalized
# live stream over WebSocket on :8000, tailing the shared capture volume.

# 3.12: the streamer uses PEP 604 union syntax (X | None), needs Python >= 3.10
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/chsbusch-dot/vscc-mqtt-server" \
      org.opencontainers.image.description="VSCC websocket streamer (research/education use only)" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vscc-websocket-streamer.py .

ENV VSC_FILE_PATH=/data/DataExportVSC.json
VOLUME /data

# The script self-hosts uvicorn (hyphenated filename is not importable as a module)
CMD ["python", "vscc-websocket-streamer.py"]
