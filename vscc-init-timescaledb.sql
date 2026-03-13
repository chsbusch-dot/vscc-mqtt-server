-- 1. Create the raw data table
-- 1. Create the table for numeric data (e.g., HR, SpO2)
CREATE TABLE IF NOT EXISTS patient_numerics (
    time TIMESTAMPTZ NOT NULL,
    physio_id TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL
);

-- 2. Create the table for high-frequency waveform data (e.g., PLETH, EEG)
CREATE TABLE IF NOT EXISTS patient_waveforms (
    time TIMESTAMPTZ NOT NULL,
    physio_id TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL
);

-- 3. Convert both tables into TimescaleDB hypertables for time-series efficiency
SELECT create_hypertable('patient_numerics', 'time', if_not_exists => TRUE);
SELECT create_hypertable('patient_waveforms', 'time', if_not_exists => TRUE);

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

-- 6. Add data retention policies for automatic cleanup (replaces cleanup.py)
SELECT add_retention_policy('patient_numerics', INTERVAL '12 hours', if_not_exists => TRUE);
SELECT add_retention_policy('patient_waveforms', INTERVAL '12 hours', if_not_exists => TRUE);