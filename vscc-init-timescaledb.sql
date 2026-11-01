-- 1. Create the raw data table
-- 1. Create the table for numeric data (e.g., HR, SpO2)
CREATE TABLE IF NOT EXISTS patient_numerics (
    time TIMESTAMPTZ NOT NULL,
    physio_id TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL
);

-- 2. Create the table for high-frequency waveform data (e.g., PLETH, ECG)
CREATE TABLE IF NOT EXISTS patient_waveforms (
    time TIMESTAMPTZ NOT NULL,
    physio_id TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL
);

-- 3. Convert both tables into TimescaleDB hypertables for time-series efficiency
SELECT create_hypertable('patient_numerics', 'time', if_not_exists => TRUE);
SELECT create_hypertable('patient_waveforms', 'time', if_not_exists => TRUE);
-- 1-day chunks: retention drops and keeps whole chunks, so a kept session pins about a day
SELECT set_chunk_time_interval('patient_numerics', INTERVAL '1 day');
SELECT set_chunk_time_interval('patient_waveforms', INTERVAL '1 day');

-- 4. Create a continuous aggregate for 1-minute downsampled waveform data
CREATE MATERIALIZED VIEW patient_waveforms_1min
WITH (timescaledb.continuous) AS
SELECT
    physio_id,
    time_bucket('1 minute', time) AS bucket,
    AVG(value) AS avg_value,
    MIN(value) AS min_value,
    MAX(value) AS max_value
FROM
    patient_waveforms
GROUP BY
    physio_id, bucket;

-- 5. Add a policy to automatically refresh the continuous aggregate
SELECT add_continuous_aggregate_policy('patient_waveforms_1min',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes');

-- 6. Sessions, with the keep flag that exempts a session's chunks from retention
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    subject_code TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS keep BOOLEAN NOT NULL DEFAULT FALSE;

-- 7. Retention: no stock retention policy. The worker installs the keep-aware
-- retention job (vscc_retention_plan / vscc_retention_job) on start, using
-- vscc_settings.retention_hours.
