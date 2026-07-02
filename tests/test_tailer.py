"""Integration tests for the worker's async I/O paths (WOR-7).

These exercise the real coroutines — tail_file, batch_inserter, wave_discovery_loop
— against a temp data dir + a fake MQTT client + a fake asyncpg pool. No live
broker or database needed, so they run in CI.

Each test drives one coroutine as a task, feeds it input, waits (bounded) for the
observable effect, then cancels. Tests are written as plain functions that run an
async body via asyncio.run(), so no pytest-asyncio plugin is required.
"""
import asyncio
import json

import vscc_mqtt_timescale_worker as w

TIMEOUT = 5.0  # generous ceiling so CI scheduling jitter doesn't flake the tests


class FakeMqttClient:
    """Records publishes and signals an Event so tests can await the first one."""
    def __init__(self):
        self.published = []
        self.got = asyncio.Event()

    async def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, json.loads(payload) if payload else None))
        self.got.set()


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self):
        self.executemany_calls = []
        self.inserted = asyncio.Event()

    async def executemany(self, query, rows):
        self.executemany_calls.append((query, list(rows)))
        self.inserted.set()


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        return _FakeAcquire(self.conn)


async def _drive_tail(tmp_path, filename, file_type, physio_id, topic, line_to_append):
    """Start tail_file on an (initially empty) file, append one line once the
    tailer is past its seek-to-end, and return the fake client after the publish."""
    f = tmp_path / filename
    f.write_text("")  # exists but empty; tail_file seeks to END on open
    config = {"path": f, "type": file_type, "topic": topic, "physio_id": physio_id}
    client = FakeMqttClient()

    task = asyncio.create_task(w.tail_file(config, client))
    try:
        await asyncio.sleep(0.5)  # let tail_file open the file and seek to EOF
        with open(f, "a") as fh:
            fh.write(line_to_append + "\n")
            fh.flush()
        await asyncio.wait_for(client.got.wait(), TIMEOUT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return client


def test_tail_file_json_publishes_numeric(tmp_path):
    w.numerics_buffer.clear()
    line = json.dumps([{"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": "88"}])
    client = asyncio.run(_drive_tail(tmp_path, "DataExportVSC.json", "json", None,
                                     "mp50/VitalSigns", line))

    assert client.published, "tailer never published"
    topic, payload = client.published[0]
    assert topic == "mp50/VitalSigns"
    assert payload["physio_id"] == "NOM_HR"
    assert payload["value"] == 88.0
    # ...and the row was buffered for the DB batch insert
    assert any(row[1] == "NOM_HR" and row[2] == 88.0 for row in w.numerics_buffer)


def test_tail_file_json_skips_dash_value(tmp_path):
    w.numerics_buffer.clear()
    # A "-" value is "no reading" and must NOT be published or buffered; send a
    # real reading after it so the wait has something to resolve on.
    line = json.dumps([
        {"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": "-"},
        {"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": "72"},
    ])
    client = asyncio.run(_drive_tail(tmp_path, "DataExportVSC.json", "json", None,
                                     "mp50/VitalSigns", line))
    values = [p["value"] for _, p in client.published]
    assert 72.0 in values
    assert None not in values  # the "-" record was dropped, not published as null


def test_tail_file_csv_publishes_waveform(tmp_path):
    w.waveforms_buffer.clear()
    # Waveform CSV: ts,relclock,localtime,VALUE
    line = "0,0,14-03-2026 04:39:00.500,1.23"
    client = asyncio.run(_drive_tail(tmp_path, "NOM_PLETHWaveExport.csv", "csv",
                                     "NOM_PLETH", "mp50/HF-PLETH", line))
    assert client.published, "tailer never published"
    topic, payload = client.published[0]
    assert topic == "mp50/HF-PLETH"
    assert payload["physio_id"] == "NOM_PLETH"
    assert payload["value"] == 1.23
    assert any(row[1] == "NOM_PLETH" and row[2] == 1.23 for row in w.waveforms_buffer)


async def _drive_batch_inserter():
    """Seed the numerics buffer, run one batch_inserter cycle, capture the insert."""
    from datetime import datetime, timezone
    w.db_pool = FakePool()
    w.numerics_buffer = [(datetime(2026, 3, 14, 4, 39, tzinfo=timezone.utc), "NOM_HR", 88.0)]
    w.waveforms_buffer = []
    task = asyncio.create_task(w.batch_inserter())
    try:
        await asyncio.wait_for(w.db_pool.conn.inserted.wait(), TIMEOUT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return w.db_pool.conn


def test_batch_inserter_flushes_numerics(tmp_path):
    conn = asyncio.run(_drive_batch_inserter())
    assert conn.executemany_calls, "batch_inserter never inserted"
    query, rows = conn.executemany_calls[0]
    assert "patient_numerics" in query
    assert rows == [rows[0]] and rows[0][1] == "NOM_HR" and rows[0][2] == 88.0


async def _drive_discovery(tmp_path, monkeypatched_tail):
    """wave_discovery_loop should find NOM_*WaveExport.csv files and launch a
    tailer for each; we stub tail_file so no real tailers linger."""
    (tmp_path / "NOM_PLETHWaveExport.csv").write_text("")
    (tmp_path / "NOM_RESPWaveExport.csv").write_text("")
    (tmp_path / "ignore-me.txt").write_text("")
    w.BASE_DIR = tmp_path
    tailed = set()
    task = asyncio.create_task(w.wave_discovery_loop(FakeMqttClient(), tailed))
    try:
        for _ in range(int(TIMEOUT / 0.05)):
            if len(tailed) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return tailed


def test_wave_discovery_finds_new_wave_files(tmp_path, monkeypatch):
    async def _noop_tail(config, client):
        await asyncio.sleep(3600)
    monkeypatch.setattr(w, "tail_file", _noop_tail)
    tailed = asyncio.run(_drive_discovery(tmp_path, _noop_tail))

    names = sorted(p.name for p in tailed)
    assert names == ["NOM_PLETHWaveExport.csv", "NOM_RESPWaveExport.csv"]
