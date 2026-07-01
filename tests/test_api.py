"""
Integration smoke tests for the worker's REST API — regression protection for the
new validation / aggregation / error branches.

These run against a *running* worker (the FastAPI app + a live TimescaleDB), since
the endpoints are thin wrappers over real SQL. Point them at any instance:

    pip install -r requirements-dev.txt
    VSCC_TEST_BASE=http://localhost:8001 pytest tests/ -q

If no worker is reachable, the suite skips. Mutating tests only exercise the
*rejection* paths (bad input), so they never persist test data.
"""
import os
import httpx
import pytest

BASE = os.environ.get("VSCC_TEST_BASE", "http://localhost:8001")


@pytest.fixture(scope="module")
def client():
    try:
        c = httpx.Client(base_url=BASE, timeout=60)
        c.get("/api/sessions").raise_for_status()
    except Exception:
        pytest.skip(f"no worker reachable at {BASE}")
    yield c
    c.close()


@pytest.fixture(scope="module")
def longest_session(client):
    closed = [s for s in client.get("/api/sessions").json() if s.get("ended_at")]
    if not closed:
        return None
    return max(closed, key=lambda s: s["ended_at"] - s["started_at"])


# --- settings validation (M2): reject before persisting ---

@pytest.mark.parametrize("payload,key", [
    ({"retention_hours": "abc"}, "retention_hours"),
    ({"retention_hours": "-4"}, "retention_hours"),
    ({"session_gap_minutes": "abc"}, "session_gap_minutes"),
    ({"session_gap_minutes": "0"}, "session_gap_minutes"),
])
def test_settings_rejects_bad_values_without_persisting(client, payload, key):
    before = client.get("/api/settings").json()[key]
    body = client.put("/api/settings", json=payload).json()
    assert body["ok"] is False
    assert client.get("/api/settings").json()[key] == before  # not persisted


def test_settings_accepts_valid(client):
    cur = client.get("/api/settings").json()
    body = client.put("/api/settings", json={"session_gap_minutes": str(cur["session_gap_minutes"])}).json()
    assert body["ok"] is True


# --- historic range clamp (L4) ---

@pytest.mark.parametrize("rng", ["-5", "0", "999999"])
def test_historic_range_clamped(client, rng):
    r = client.get(f"/api/historic/{rng}")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# --- annotation validation (L5): clean errors, not 500s ---

def test_annotation_bad_time_returns_ok_false(client):
    body = client.post("/api/annotations", json={"label": "x", "time": "notanumber"}).json()
    assert body.get("ok") is False


def test_annotation_nonexistent_session_returns_ok_false(client):
    r = client.post("/api/annotations", json={"label": "x", "session_id": 999999999})
    assert r.status_code == 200
    assert r.json().get("ok") is False


def test_annotation_missing_label_returns_ok_false(client):
    body = client.post("/api/annotations", json={}).json()
    assert body.get("ok") is False


# --- aggregation dispatch + raw DoS clamp (M1) ---

def test_agg_dispatch(client, longest_session):
    if not longest_session:
        pytest.skip("no closed session to replay")
    sid = longest_session["id"]
    for agg in ("1min", "5min"):
        d = client.get(f"/api/sessions/{sid}/data", params={"agg": agg}).json()
        assert d["aggregated_waveforms"] is True


def test_unknown_agg_is_safe(client, longest_session):
    if not longest_session:
        pytest.skip("no closed session")
    d = client.get(f"/api/sessions/{longest_session['id']}/data", params={"agg": "garbage"}).json()
    assert "waveforms" in d and "numerics" in d  # no server error


def test_windowed_load_returns_only_the_window(client, longest_session):
    if not longest_session:
        pytest.skip("no closed session")
    sid = longest_session["id"]
    start, end = longest_session["started_at"], longest_session["ended_at"]
    if end - start < 200:
        pytest.skip("need a session longer than the test windows")
    # Compare two raw windows both narrow enough to stay raw (no downshift), so the
    # only variable is the window width: a wider window must return more rows, and
    # each window's data must be confined to it.
    narrow = client.get(f"/api/sessions/{sid}/data",
                        params={"agg": "raw", "from_ts": start + 60, "to_ts": start + 90}).json()   # 30s
    wide = client.get(f"/api/sessions/{sid}/data",
                      params={"agg": "raw", "from_ts": start + 60, "to_ts": start + 180}).json()    # 120s
    assert narrow["aggregated_waveforms"] is False and wide["aggregated_waveforms"] is False
    assert len(narrow["waveforms"]) < len(wide["waveforms"])
    if narrow["waveforms"]:
        ts = [r["time"] for r in narrow["waveforms"]]
        assert min(ts) >= start + 59 and max(ts) <= start + 91  # confined to its window

    # Out-of-range window clamps to the session rather than erroring.
    clamped = client.get(f"/api/sessions/{sid}/data",
                         params={"agg": "1min", "from_ts": 0, "to_ts": start + 180})
    assert clamped.status_code == 200 and "waveforms" in clamped.json()


def test_raw_with_huge_max_raw_minutes_is_clamped(client, longest_session):
    if not longest_session:
        pytest.skip("no closed session")
    span_min = (longest_session["ended_at"] - longest_session["started_at"]) / 60
    if span_min <= 60:
        pytest.skip("need a >60-min session to prove the clamp downshifts raw")
    d = client.get(f"/api/sessions/{longest_session['id']}/data",
                   params={"agg": "raw", "max_raw_minutes": 99999999}).json()
    assert d["aggregated_waveforms"] is True  # clamp forced a downshift, not millions of rows


# --- quality completeness cap (L6) ---

def test_quality_completeness_never_exceeds_100(client, longest_session):
    if not longest_session:
        pytest.skip("no closed session")
    q = client.get(f"/api/sessions/{longest_session['id']}/quality").json()
    for w in q.get("waveforms", []):
        if w["completeness_pct"] is not None:
            assert 0.0 <= w["completeness_pct"] <= 100.0
