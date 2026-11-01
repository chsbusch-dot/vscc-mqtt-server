"""Keep-aware retention and the session keep flag (WOR-1015).

Two layers:
  * pure tests of the install SQL, apply_retention's call sequence and the keep
    API, against fake connections (no database);
  * integration tests against a real TimescaleDB, skipped unless
    VSCC_TEST_TSDB_DSN points at one (CI runs a TimescaleDB service). Each test
    gets its own throwaway database. The SQL plan is checked against the Python
    reference rule below, so the decision rule has two independent
    implementations.
"""
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

import vscc_mqtt_timescale_worker as w

ROOT = Path(__file__).resolve().parent.parent


# --- Reference decision rule (Python twin of vscc_retention_plan) ---

@dataclass
class Sess:
    started_at: datetime
    ended_at: datetime | None
    keep: bool


def reference_action(range_start: datetime, range_end: datetime, sessions: list[Sess],
                     drop_after: timedelta, as_of: datetime) -> str:
    """A chunk [range_start, range_end) past the window is dropped unless a kept
    session overlaps it. ended_at is inclusive; an open session runs on."""
    if range_end > as_of - drop_after:
        return "in-window"
    for s in sessions:
        end = s.ended_at or datetime.max.replace(tzinfo=UTC)
        if s.keep and s.started_at < range_end and end >= range_start:
            return "keep"
    return "drop"


D = datetime(2026, 1, 10, tzinfo=UTC)
DAY = timedelta(days=1)
H72 = timedelta(hours=72)


def test_reference_rule_in_window_chunk_is_untouched():
    assert reference_action(D - DAY, D, [], H72, D + timedelta(hours=71)) == "in-window"


def test_reference_rule_old_chunk_without_kept_session_drops():
    unkept = Sess(D - DAY + timedelta(hours=1), D - DAY + timedelta(hours=2), keep=False)
    assert reference_action(D - DAY, D, [unkept], H72, D + H72) == "drop"


def test_reference_rule_kept_session_keeps_its_chunk_however_old():
    kept = Sess(D - DAY + timedelta(hours=1), D - DAY + timedelta(hours=2), keep=True)
    assert reference_action(D - DAY, D, [kept], timedelta(hours=1), D + timedelta(days=400)) == "keep"


def test_reference_rule_boundaries():
    # Ends exactly at the chunk start: its last row lives in the chunk -> keep.
    ends_at_start = Sess(D - timedelta(minutes=30), D, keep=True)
    assert reference_action(D, D + DAY, [ends_at_start], H72, D + 10 * DAY) == "keep"
    # Starts exactly at the chunk end: no row in the chunk -> drop.
    starts_at_end = Sess(D + DAY, D + DAY + timedelta(hours=1), keep=True)
    assert reference_action(D, D + DAY, [starts_at_end], H72, D + 10 * DAY) == "drop"


def test_reference_rule_open_kept_session_keeps_every_chunk_from_its_start():
    open_kept = Sess(D + timedelta(hours=5), None, keep=True)
    assert reference_action(D, D + DAY, [open_kept], H72, D + 10 * DAY) == "keep"
    assert reference_action(D + 3 * DAY, D + 4 * DAY, [open_kept], H72, D + 10 * DAY) == "keep"
    assert reference_action(D - DAY, D, [open_kept], H72, D + 10 * DAY) == "drop"


# --- Install SQL ---

def test_schema_statements_add_keep_and_daily_chunks_without_stock_policy():
    stmts = [" ".join(s.split()) for s in w.SCHEMA_STATEMENTS]
    keep_idx = stmts.index("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS keep BOOLEAN NOT NULL DEFAULT FALSE")
    sessions_idx = next(i for i, s in enumerate(stmts) if s.startswith("CREATE TABLE IF NOT EXISTS sessions"))
    assert sessions_idx < keep_idx
    for table in w.RETENTION_TABLES:
        assert f"SELECT set_chunk_time_interval('{table}', INTERVAL '1 day')" in stmts
    assert not any("add_retention_policy" in s for s in stmts)


def test_init_sql_adds_keep_and_daily_chunks_without_stock_policy():
    sql = (ROOT / "vscc-init-timescaledb.sql").read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS keep BOOLEAN NOT NULL DEFAULT FALSE" in code
    for table in w.RETENTION_TABLES:
        assert f"SELECT set_chunk_time_interval('{table}', INTERVAL '1 day');" in code
    assert "add_retention_policy" not in code


class _Ctx:
    def __init__(self, value: Any = None, on_enter: Any = None):
        self.value, self.on_enter = value, on_enter

    async def __aenter__(self):
        if self.on_enter:
            self.on_enter()
        return self.value

    async def __aexit__(self, *exc):
        return False


class RecordingConn:
    def __init__(self, row: dict[str, Any] | None = None):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.in_transaction = False
        self.row = row

    def _begin(self) -> None:
        self.in_transaction = True

    def transaction(self):
        return _Ctx(on_enter=self._begin)

    async def execute(self, query: str, *args: Any) -> str:
        assert self.in_transaction, "retention install must run inside one transaction"
        self.calls.append((" ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        self.calls.append((" ".join(query.split()), args))
        return self.row


class RecordingPool:
    def __init__(self, conn: RecordingConn):
        self.conn = conn

    def acquire(self):
        return _Ctx(self.conn)


async def test_apply_retention_installs_one_keep_aware_job(monkeypatch):
    conn = RecordingConn()
    monkeypatch.setattr(w, "db_pool", RecordingPool(conn))
    await w.apply_retention(72)
    queries = [q for q, _ in conn.calls]
    assert queries[0].startswith("CREATE OR REPLACE FUNCTION vscc_retention_plan(")
    assert queries[1].startswith("CREATE OR REPLACE PROCEDURE vscc_retention_job(")
    for table in w.RETENTION_TABLES:
        assert f"SELECT remove_retention_policy('{table}', if_exists => TRUE)" in queries
    delete = next(i for i, q in enumerate(queries) if "delete_job" in q)
    adds = [i for i, q in enumerate(queries) if "add_job" in q]
    assert len(adds) == 1 and delete < adds[0]
    assert conn.calls[delete][1] == ("vscc_retention_job",)
    assert json.loads(conn.calls[adds[0]][1][0]) == {"drop_after": "72 hours"}
    assert not any("add_retention_policy" in q for q in queries)


async def test_apply_retention_rejects_non_positive_hours(monkeypatch):
    conn = RecordingConn()
    monkeypatch.setattr(w, "db_pool", RecordingPool(conn))
    for bad in (0, -5):
        with pytest.raises(ValueError):
            await w.apply_retention(bad)
    assert conn.calls == []


# --- Keep API ---

SESSION_ROW = {"id": 7, "label": "", "subject_code": "", "notes": "",
               "started_at": D, "ended_at": D + timedelta(hours=1), "keep": True}


def test_session_dict_reports_keep():
    assert w._session_dict(SESSION_ROW)["keep"] is True
    assert w._session_dict({**SESSION_ROW, "keep": False})["keep"] is False


@pytest.mark.parametrize("bad", ["true", 1, None])
async def test_update_session_rejects_non_boolean_keep(monkeypatch, bad):
    conn = RecordingConn()
    monkeypatch.setattr(w, "db_pool", RecordingPool(conn))
    res = await w.update_session(7, {"keep": bad})
    assert res == {"ok": False, "error": "keep must be true or false"}
    assert conn.calls == []


async def test_update_session_sets_keep(monkeypatch):
    conn = RecordingConn(row=SESSION_ROW)
    monkeypatch.setattr(w, "db_pool", RecordingPool(conn))
    res = await w.update_session(7, {"keep": True})
    assert conn.calls == [("UPDATE sessions SET keep = $2 WHERE id = $1 RETURNING *", (7, True))]
    assert res["keep"] is True


async def test_delete_session_refuses_kept_session(monkeypatch):
    conn = RecordingConn(row=SESSION_ROW)
    monkeypatch.setattr(w, "db_pool", RecordingPool(conn))
    res = await w.delete_session(7)
    assert res == {"ok": False, "error": "session is kept — clear keep before deleting it"}
    assert all(not q.startswith("DELETE") for q, _ in conn.calls)


# --- Integration: real TimescaleDB ---

TSDB_DSN = os.environ.get("VSCC_TEST_TSDB_DSN")
needs_tsdb = pytest.mark.skipif(not TSDB_DSN, reason="VSCC_TEST_TSDB_DSN not set (no TimescaleDB to test against)")


def _dsn_for(dbname: str) -> str:
    parts = urlsplit(TSDB_DSN or "")
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture
async def tsdb(monkeypatch):
    dbname = f"vscc_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(TSDB_DSN)
    await admin.execute(f"CREATE DATABASE {dbname}")
    pool = await asyncpg.create_pool(_dsn_for(dbname), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        monkeypatch.setattr(w, "db_pool", pool)
        await w.ensure_schema(pool)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f"DROP DATABASE {dbname} WITH (FORCE)")
        await admin.close()


async def _jobs(conn) -> list[str]:
    return [r["proc_name"] for r in await conn.fetch("SELECT proc_name FROM timescaledb_information.jobs")]


@needs_tsdb
async def test_apply_retention_twice_leaves_one_job_and_no_stock_policy(tsdb):
    async with tsdb.acquire() as conn:
        # A pre-WOR-1015 install carries the stock policies.
        for table in w.RETENTION_TABLES:
            await conn.execute(f"SELECT add_retention_policy('{table}', INTERVAL '12 hours')")
        assert (await _jobs(conn)).count("policy_retention") == 2
    await w.apply_retention(72)
    await w.apply_retention(72)
    async with tsdb.acquire() as conn:
        jobs = await _jobs(conn)
        assert jobs.count("vscc_retention_job") == 1
        assert "policy_retention" not in jobs
        config = await conn.fetchval(
            "SELECT config FROM timescaledb_information.jobs WHERE proc_name = 'vscc_retention_job'")
        assert json.loads(config) == {"drop_after": "72 hours"}
        intervals = await conn.fetch(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_name IN ('patient_numerics', 'patient_waveforms')")
        assert [r["time_interval"] for r in intervals] == [DAY, DAY]


async def _chunk_rows(conn) -> dict[tuple[str, datetime], int]:
    out = {}
    for c in await conn.fetch(
            "SELECT hypertable_name, range_start, range_end FROM timescaledb_information.chunks "
            "WHERE hypertable_name IN ('patient_numerics', 'patient_waveforms')"):
        n = await conn.fetchval(
            f"SELECT count(*) FROM {c['hypertable_name']} WHERE time >= $1 AND time < $2",
            c["range_start"], c["range_end"])
        out[(c["hypertable_name"], c["range_start"])] = n
    return out


@needs_tsdb
async def test_retention_job_keeps_kept_drops_unkept_and_spares_the_window(tsdb):
    await w.apply_retention(72)
    async with tsdb.acquire() as conn:
        # Pause the scheduled job so only the explicit CALLs below run it.
        await conn.execute("SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
                           "WHERE proc_name = 'vscc_retention_job'")
        now = await conn.fetchval("SELECT now()")
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        def day(n: int, hours: float = 1.0) -> datetime:
            return today - n * DAY + timedelta(hours=hours)

        sessions = [
            Sess(day(10, 1), day(10, 2), keep=True),     # day -10: kept, closed
            Sess(day(8, 1), day(8, 2), keep=False),      # day -8: unkept
            Sess(day(6, 1), None, keep=True),            # day -6 on: kept, still open
            Sess(day(13, 23.5), day(12, 0), keep=True),  # ends exactly at day -12's start
            Sess(day(13, 0), day(13, 0.5), keep=False),  # day -14 is next to nothing kept
        ]
        for s in sessions:
            await conn.execute("INSERT INTO sessions (started_at, ended_at, keep) VALUES ($1, $2, $3)",
                               s.started_at, s.ended_at, s.keep)
        stamps = [day(n, 1.5) for n in (14, 13, 12, 10, 8, 6)] + [day(12, 0), now]
        for table in w.RETENTION_TABLES:
            await conn.executemany(f"INSERT INTO {table} (time, physio_id, value) VALUES ($1, 'X', 1)",
                                   [(t,) for t in stamps])
        before = await _chunk_rows(conn)

        plan = await conn.fetch("SELECT * FROM vscc_retention_plan('72 hours')")
        expected = {(p["hypertable"], p["range_start"]):
                    reference_action(p["range_start"], p["range_end"], sessions, H72, now) for p in plan}
        got = {(p["hypertable"], p["range_start"]): p["action"] for p in plan}
        assert got == expected
        by_day = {round((today - k[1]) / DAY): a for k, a in got.items() if k[0] == "patient_waveforms"}
        assert by_day == {14: "drop", 13: "keep", 12: "keep", 10: "keep", 8: "drop", 6: "keep", 0: "in-window"}

        # The keep-aware drop set never exceeds what the stock policy would drop.
        stock = set()
        for table in w.RETENTION_TABLES:
            stock |= {r[0] for r in await conn.fetch(
                "SELECT show_chunks($1::text::regclass, older_than => now() - interval '72 hours')::text", table)}
        assert {p["chunk"] for p in plan if p["action"] == "drop"} <= stock

        await conn.execute("""CALL vscc_retention_job(0, '{"drop_after": "72 hours"}')""")
        after = await _chunk_rows(conn)
        survivors = {k: n for k, n in before.items() if got[k] != "drop"}
        assert after == survivors

        # Lowering retention never drops a kept chunk.
        await conn.execute("""CALL vscc_retention_job(0, '{"drop_after": "1 hour"}')""")
        lowered = await _chunk_rows(conn)
        for k, action in got.items():
            if action == "keep":
                assert lowered[k] == before[k]


@needs_tsdb
@pytest.mark.parametrize("config", ['{}', '{"drop_after": "0 hours"}', '{"drop_after": "-1 day"}'])
async def test_retention_job_refuses_a_missing_or_non_positive_window(tsdb, config):
    await w.apply_retention(72)
    async with tsdb.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="drop_after must be a positive interval"):
            await conn.execute(f"CALL vscc_retention_job(0, '{config}')")
