FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vscc_mqtt_timescale_worker.py .

CMD ["uvicorn", "vscc_mqtt_timescale_worker:app", "--host", "0.0.0.0", "--port", "8000"]