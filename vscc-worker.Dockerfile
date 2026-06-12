FROM python:3.9-slim

WORKDIR /app

# Stream print()/logging straight to docker logs (no stdout buffering),
# so file-discovery, tailing, and disconnect messages are visible live.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vscc_mqtt_timescale_worker.py .

# Demo / no-hardware replay mode (run via a command override in the demo compose)
COPY vscc-demo-replayer.py vscc-demo-data.csv.gz ./

CMD ["uvicorn", "vscc_mqtt_timescale_worker:app", "--host", "0.0.0.0", "--port", "8000"]