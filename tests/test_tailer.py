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

import aiomqtt
from fastapi import Response

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


# --- WOR-840: MQTT reconnect after an EMQX-only restart ------------------------
#
# The bug: the single aiomqtt client was created once in the lifespan and handed
# to the tailers. When the broker went away (an EMQX-only `vscc-deploy.sh`
# recreate) that client died and nothing recreated it -- the worker stayed "up"
# but published nothing until a manual `docker restart vscc-worker`. The fix is a
# reconnecting MqttPublisher handle plus a connection-manager task. These tests
# reproduce a mid-run broker outage against a disposable fake broker (no live
# .212 broker, so they run in CI) and assert publishing resumes with no restart.


async def _wait_until(pred, timeout=TIMEOUT):
    """Poll pred() until true or timeout; returns whether it became true."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


class _FakeBroker:
    """A switchable fake broker. `up` governs whether a NEW connection succeeds
    and whether publishes on an open client succeed; flipping it mid-run
    simulates the EMQX container being recreated out from under the worker."""
    def __init__(self):
        self.up = True
        self.clients = []           # every FakeClient handed out, in order
        self.connect_attempts = 0


class _FakeClient:
    """Stands in for `aiomqtt.Client(...)` as an async context manager. Raises the
    REAL aiomqtt.MqttError on connect/publish while the broker is down, so the
    manager's `except aiomqtt.MqttError` path is exercised exactly as in prod."""
    def __init__(self, broker):
        self._broker = broker
        self.published = []
        self.alive = False

    async def __aenter__(self):
        self._broker.connect_attempts += 1
        if not self._broker.up:
            raise aiomqtt.MqttError("fake broker down (connect refused)")
        self.alive = True
        self._broker.clients.append(self)
        return self

    async def __aexit__(self, *exc):
        self.alive = False
        return False

    async def publish(self, topic, payload=None, qos=0):
        if not self._broker.up or not self.alive:
            raise aiomqtt.MqttError("fake broker down (publish failed)")
        self.published.append((topic, payload))


async def test_connection_manager_reconnects_after_broker_bounce(monkeypatch):
    # Fast timings so the backoff loop turns over quickly under the test ceiling.
    monkeypatch.setattr(w, "MQTT_PROBE_INTERVAL", 0.05)
    monkeypatch.setattr(w, "MQTT_RECONNECT_BASE", 0.05)
    monkeypatch.setattr(w, "MQTT_RECONNECT_MAX", 0.1)

    broker = _FakeBroker()
    monkeypatch.setattr(w.aiomqtt, "Client", lambda *a, **k: _FakeClient(broker))

    pub = w.MqttPublisher()
    task = asyncio.create_task(w.mqtt_connection_manager(pub))
    try:
        # POSITIVE CONTROL: it connects and a publish through the handle lands.
        assert await _wait_until(lambda: pub.connected), "manager never connected"
        await pub.publish("t/x", payload="hello")
        assert ("t/x", "hello") in broker.clients[-1].published

        # Broker goes away mid-run (EMQX-only recreate). The keepalive probe (or a
        # data publish) must notice and the handle must flip disconnected.
        broker.up = False
        assert await _wait_until(lambda: not pub.connected), \
            "manager did not detect the broker drop (health would stay green)"
        clients_during_outage = len(broker.clients)

        # Broker comes back: the manager must reconnect on its own, no restart.
        broker.up = True
        assert await _wait_until(lambda: pub.connected), \
            "manager did not reconnect after the broker returned"
        assert len(broker.clients) > clients_during_outage, "no fresh client bound"

        # POSITIVE CONTROL (post-outage): publishing resumes on the NEW client.
        await pub.publish("t/after", payload="resumed")
        assert ("t/after", "resumed") in broker.clients[-1].published
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_tailer_resumes_publishing_across_broker_bounce(tmp_path):
    # Drives the REAL tail_file through a MqttPublisher while the broker is bounced
    # by hand (standing in for the connection manager's bind/unbind). Proves the
    # same running tailer resumes publishing with no restart, and that DB ingest
    # continues through the outage (the row is buffered before the publish).
    w.numerics_buffer.clear()
    broker = _FakeBroker()
    pub = w.MqttPublisher()
    client_a = _FakeClient(broker)
    client_a.alive = True
    pub.bind(client_a)

    f = tmp_path / "DataExportVSC.json"
    f.write_text("")  # exists but empty; tail_file seeks to END on open
    config = {"path": f, "type": "json", "topic": "mp50/VitalSigns", "physio_id": None}
    task = asyncio.create_task(w.tail_file(config, pub))

    def _append(value):
        line = json.dumps([{"Timestamp": "14-03-2026 04:39:00", "PhysioID": "NOM_HR", "Value": value}])
        with open(f, "a") as fh:
            fh.write(line + "\n")
            fh.flush()

    try:
        await asyncio.sleep(0.5)  # let the tailer open + seek to EOF

        # POSITIVE CONTROL (before outage): the frame reaches the broker.
        _append("88")
        assert await _wait_until(lambda: any(json.loads(pl)["value"] == 88.0
                                             for _, pl in client_a.published)), \
            "tailer never published the pre-outage frame"

        # Broker goes away: unbind the handle (what the manager does on a drop).
        pub.unbind()
        _append("72")
        # The live frame is dropped (no broker), but the DB row is still buffered
        # and the tailer keeps running -- no exception, no restart.
        assert await _wait_until(lambda: any(r[2] == 72.0 for r in w.numerics_buffer)), \
            "DB ingest stopped during the MQTT outage"

        # Broker returns on a fresh client (manager rebinds). SAME tailer resumes.
        client_b = _FakeClient(broker)
        client_b.alive = True
        pub.bind(client_b)
        _append("99")
        assert await _wait_until(lambda: any(json.loads(pl)["value"] == 99.0
                                             for _, pl in client_b.published)), \
            "tailer did not resume publishing after reconnect (the WOR-840 bug)"
        # The dropped "72" frame never reached either client.
        published_vals = [json.loads(pl)["value"]
                          for c in (client_a, client_b) for _, pl in c.published]
        assert 72.0 not in published_vals
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_status_returns_503_and_flag_while_mqtt_disconnected():
    # /api/status must reflect MQTT state so the container health check goes red
    # while the broker is down. db_pool is None here, so the DB probe is swallowed
    # (db_lag/db_size come back null) -- this test only pins the MQTT behaviour.
    w.db_pool = None
    saved = w.mqtt_publisher
    try:
        # NEGATIVE: disconnected -> 503 + mqtt_connected False.
        w.mqtt_publisher = w.MqttPublisher()  # fresh, never connected
        resp = Response()
        body = await w.status(resp)
        assert body["mqtt_connected"] is False
        assert resp.status_code == 503

        # POSITIVE CONTROL: connected -> no 503, flag True. Without this pair a
        # hard-wired 503 would pass the negative case and hide a real regression.
        conn = w.MqttPublisher()
        conn.connected = True
        conn.connects = 1
        w.mqtt_publisher = conn
        resp_ok = Response()
        body_ok = await w.status(resp_ok)
        assert body_ok["mqtt_connected"] is True
        assert resp_ok.status_code != 503
    finally:
        w.mqtt_publisher = saved
