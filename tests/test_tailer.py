"""Integration tests for the worker's async I/O paths (WOR-7).

These exercise the real coroutines — tail_file, batch_inserter, wave_discovery_loop
— against a temp data dir + a fake MQTT client + a fake asyncpg pool. No live
broker or database needed, so they run in CI.

Each test drives one coroutine as a task, feeds it input, waits (bounded) for the
observable effect, then cancels. Tests are plain `async def` run by pytest-asyncio
(asyncio_mode = "auto" in pyproject.toml).
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

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


async def test_tail_file_json_publishes_numeric(tmp_path):
    w.numerics_buffer.clear()
    line = json.dumps([{"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": "88"}])
    client = await _drive_tail(tmp_path, "DataExportVSC.json", "json", None,
                               "mp50/VitalSigns", line)

    assert client.published, "tailer never published"
    topic, payload = client.published[0]
    assert topic == "mp50/VitalSigns"
    assert payload["physio_id"] == "NOM_HR"
    assert payload["value"] == 88.0
    # ...and the row was buffered for the DB batch insert
    assert any(row[1] == "NOM_HR" and row[2] == 88.0 for row in w.numerics_buffer)


async def test_tail_file_json_skips_dash_value(tmp_path):
    w.numerics_buffer.clear()
    # A "-" value is "no reading" and must NOT be published or buffered; send a
    # real reading after it so the wait has something to resolve on.
    line = json.dumps([
        {"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": "-"},
        {"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": "72"},
    ])
    client = await _drive_tail(tmp_path, "DataExportVSC.json", "json", None,
                               "mp50/VitalSigns", line)
    values = [p["value"] for _, p in client.published]
    assert 72.0 in values
    assert None not in values  # the "-" record was dropped, not published as null


async def test_tail_file_csv_publishes_waveform(tmp_path):
    w.waveforms_buffer.clear()
    # Waveform CSV: ts,relclock,localtime,VALUE
    line = "0,0,14-03-2026 04:39:00.500,1.23"
    client = await _drive_tail(tmp_path, "NOM_PLETHWaveExport.csv", "csv",
                               "NOM_PLETH", "mp50/HF-PLETH", line)
    assert client.published, "tailer never published"
    topic, payload = client.published[0]
    assert topic == "mp50/HF-PLETH"
    assert payload["physio_id"] == "NOM_PLETH"
    assert payload["value"] == 1.23
    assert any(row[1] == "NOM_PLETH" and row[2] == 1.23 for row in w.waveforms_buffer)


@pytest.mark.parametrize(("physio_id", "block", "ecg"), [
    ("NOM_ECG_ELEC_POTL_II", 192, True),
    ("NOM_ECG_ELEC_POTL_I", 192, True),
    ("NOM_PLETH", 32, False),
])
async def test_tail_file_csv_drops_ecg_copy_rows(tmp_path: Path, physio_id: str, block: int, ecg: bool) -> None:
    # WOR-976: CSV column 1 counts blocks (+2048 each). An ECG block's first 64
    # rows are a copy, so the tailer keeps rows 65..192, and drops the block it
    # joins mid-way (the lead-in) whole. Other waves pass every row.
    w.waveforms_buffer.clear()
    stamp = "14-03-2026 04:39:00.500"
    lead_in = [f"0,1000,{stamp},{9000 + i}" for i in range(block // 4)]
    blocks = [f"{b},{3048 + 2048 * b},{stamp},{1000 * b + i}" for b in range(3) for i in range(block)]
    want = [1000 * b + i for b in range(3) for i in range(64, block)] if ecg else \
        [9000 + i for i in range(len(lead_in))] + [1000 * b + i for b in range(3) for i in range(block)]

    f = tmp_path / f"{physio_id}WaveExport.csv"
    f.write_text("")
    client = FakeMqttClient()
    config = {"path": f, "type": "csv", "topic": "mp50/HF-X", "physio_id": physio_id}
    task = asyncio.create_task(w.tail_file(config, cast(Any, client)))
    try:
        await asyncio.sleep(0.5)  # let tail_file open the file and seek to EOF
        with open(f, "a") as fh:
            fh.write("".join(line + "\n" for line in lead_in + blocks))
        for _ in range(int(TIMEOUT / 0.05)):
            if len(client.published) >= len(want):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # rows past `want` would land here
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert [p["value"] for _, p in client.published] == want
    assert [row[2] for row in w.waveforms_buffer if row[1] == physio_id] == want


async def _feed_tail(tmp_path: Path, config: dict[str, Any], lines: list[str], n_want: int) -> FakeMqttClient:
    """Start tail_file on an empty file, append lines past its seek-to-end, and
    return the client once n_want publishes landed (plus a beat for extras)."""
    f = config["path"]
    f.write_text("")
    client = FakeMqttClient()
    task = asyncio.create_task(w.tail_file(config, cast(Any, client)))
    try:
        await asyncio.sleep(0.5)  # let tail_file open the file and seek to EOF
        with open(f, "a") as fh:
            fh.write("".join(line + "\n" for line in lines))
        for _ in range(int(TIMEOUT / 0.05)):
            if len(client.published) >= n_want:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # rows past n_want would land here
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return client


async def test_tail_file_csv_splits_eeg_channels(tmp_path: Path) -> None:
    # WOR-1008: each 64-row col1 block of the EEG wave is 32 rows of channel 1,
    # then 32 of channel 2. The combined series stays as it was; the per-channel
    # ones start at the first full block (the lead-in can't be placed).
    w.waveforms_buffer.clear()
    pid = w.EEG_WAVE_ID
    stamp = "14-03-2026 04:39:00.500"
    lead_in = [f"0,1000,{stamp},{9000 + i}" for i in range(20)]
    blocks = [f"{b},{3048 + 2048 * b},{stamp},{1000 * b + i}" for b in range(3) for i in range(64)]
    combined = [9000 + i for i in range(20)] + [1000 * b + i for b in range(3) for i in range(64)]
    eeg1 = [1000 * b + i for b in range(3) for i in range(32)]
    eeg2 = [1000 * b + i for b in range(3) for i in range(32, 64)]
    config = {"path": tmp_path / f"{pid}WaveExport.csv", "type": "csv",
              "topic": w.wave_topic(pid), "physio_id": pid}

    client = await _feed_tail(tmp_path, config, lead_in + blocks, len(combined) + len(eeg1) + len(eeg2))

    def on(topic: str) -> list[tuple[str, float]]:
        return [(p["physio_id"], p["value"]) for t, p in client.published if t == topic]
    assert on("mp50/HF-EEG_ELEC_POTL_CRTX") == [(pid, v) for v in combined]
    assert on("mp50/HF-EEG_ELEC_POTL_CRTX_EEG1") == [(f"{pid}_EEG1", v) for v in eeg1]
    assert on("mp50/HF-EEG_ELEC_POTL_CRTX_EEG2") == [(f"{pid}_EEG2", v) for v in eeg2]
    assert len(client.published) == len(combined) + len(eeg1) + len(eeg2)
    for want_id, want in ((pid, combined), (f"{pid}_EEG1", eeg1), (f"{pid}_EEG2", eeg2)):
        assert [row[2] for row in w.waveforms_buffer if row[1] == want_id] == want


async def test_tail_file_json_splits_eeg_numerics(tmp_path: Path) -> None:
    # WOR-1008: an EEG numerics poll is two lines with the same
    # Relativetimestamp, channel 1 first. The combined ids stay as they were;
    # the poll the tailer joins mid-way (here its second line) gets no channel,
    # and a non-EEG line in between does not break a pair.
    w.numerics_buffer.clear()
    tot, alpha = "NOM_EEG_PWR_SPEC_TOT", "NOM_EEG_PWR_SPEC_ALPHA_REL"

    def eeg(rel: int, v_tot: str, v_alpha: str) -> str:
        return json.dumps([
            {"Timestamp": "14-03-2026 04:39:00", "Relativetimestamp": rel, "PhysioID": tot, "Value": v_tot},
            {"Timestamp": "14-03-2026 04:39:00", "Relativetimestamp": rel, "PhysioID": alpha, "Value": v_alpha},
        ])
    hr = json.dumps([{"Timestamp": "14-03-2026 04:39:00", "Relativetimestamp": 8292, "PhysioID": "NOM_HR", "Value": "60"}])
    lines = [eeg(100, "1", "2"),                       # lead-in: second line of a poll
             eeg(8292, "11", "12"), hr, eeg(8292, "21", "-"),
             eeg(16484, "31", "32"), eeg(16484, "41", "42")]
    want = {tot: [1.0, 11.0, 21.0, 31.0, 41.0], alpha: [2.0, 12.0, 32.0, 42.0], "NOM_HR": [60.0],
            f"{tot}_EEG1": [11.0, 31.0], f"{tot}_EEG2": [21.0, 41.0],
            f"{alpha}_EEG1": [12.0, 32.0], f"{alpha}_EEG2": [42.0]}
    config = {"path": tmp_path / "DataExportVSC.json", "type": "json", "topic": "mp50/VitalSigns", "physio_id": None}

    client = await _feed_tail(tmp_path, config, lines, sum(len(v) for v in want.values()))

    assert {t for t, _ in client.published} == {"mp50/VitalSigns"}
    got: dict[str, list[float]] = {}
    for _, p in client.published:
        got.setdefault(p["physio_id"], []).append(p["value"])
    assert got == want
    buffered: dict[str, list[float]] = {}
    for row in w.numerics_buffer:
        buffered.setdefault(row[1], []).append(row[2])
    assert buffered == want


async def test_tail_file_eeg_disconnect_nulls_every_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "WAVEFORM_DISCONNECT_TIMEOUT", 0.1)
    pid = w.EEG_WAVE_ID
    config = {"path": tmp_path / f"{pid}WaveExport.csv", "type": "csv",
              "topic": w.wave_topic(pid), "physio_id": pid}

    client = await _feed_tail(tmp_path, config, [], 3)

    assert sorted((t, p["physio_id"], p["value"]) for t, p in client.published) == [
        ("mp50/HF-EEG_ELEC_POTL_CRTX", pid, None),
        ("mp50/HF-EEG_ELEC_POTL_CRTX_EEG1", f"{pid}_EEG1", None),
        ("mp50/HF-EEG_ELEC_POTL_CRTX_EEG2", f"{pid}_EEG2", None),
    ]


async def test_batch_inserter_flushes_numerics():
    w.db_pool = FakePool()
    w.numerics_buffer = [(datetime(2026, 3, 14, 4, 39, tzinfo=UTC), "NOM_HR", 88.0)]
    w.waveforms_buffer = []
    task = asyncio.create_task(w.batch_inserter())
    try:
        await asyncio.wait_for(w.db_pool.conn.inserted.wait(), TIMEOUT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    conn = w.db_pool.conn
    assert conn.executemany_calls, "batch_inserter never inserted"
    query, rows = conn.executemany_calls[0]
    assert "patient_numerics" in query
    assert len(rows) == 1 and rows[0][1] == "NOM_HR" and rows[0][2] == 88.0


async def test_wave_discovery_finds_new_wave_files(tmp_path, monkeypatch):
    # Stub tail_file so discovery doesn't launch real tailers that would linger.
    async def _noop_tail(config, client):
        await asyncio.sleep(3600)
    monkeypatch.setattr(w, "tail_file", _noop_tail)

    (tmp_path / "NOM_PLETHWaveExport.csv").write_text("")
    (tmp_path / "NOM_RESPWaveExport.csv").write_text("")
    (tmp_path / "ignore-me.txt").write_text("")
    monkeypatch.setattr(w, "BASE_DIR", tmp_path)

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

    names = sorted(p.name for p in tailed)
    assert names == ["NOM_PLETHWaveExport.csv", "NOM_RESPWaveExport.csv"]
