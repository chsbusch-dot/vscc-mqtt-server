"""EDF export (worker section 7): one record per monitor frame at the measured
frame period, the ECG's 250 Hz copy rows dropped, short frames padded and lost
frames left as all-zero records.

The fixtures mimic the MP50 capture: a frame of rows about every 1.024 s (ECG
768, pleth 128, resp 64), each 192-row ECG block a 64-row copy of its 128 real
samples at 250 Hz followed by the 128. The EDF is read back with a plain header
parse and, where installed, pyedflib.
"""

import io
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import vscc_mqtt_timescale_worker as w

ECG = "NOM_ECG_ELEC_POTL_II"
PLETH = "NOM_PLETH"
RESP = "NOM_RESP"
FRAME_ROWS = {ECG: 768, PLETH: 128, RESP: 64}
SPR = {ECG: 512, PLETH: 128, RESP: 64}
P = 1.0244
T0 = 1_761_600_000.0

Feed = dict[str, list[tuple[float, list[float]]]]


def _val(n: int) -> float:
    return (n % 800) / 1000 - 0.4


def _ecg_frame(n0: int, n_rows: int) -> tuple[list[float], list[float]]:
    """The first n_rows capture rows of an ECG frame whose real samples start at
    index n0, and the real samples among them."""
    rows: list[float] = []
    for b in range(4):
        real = [_val(n0 + 128 * b + i) for i in range(128)]
        rows += real[::2] + real
    rows = rows[:n_rows]
    return rows, [v for i, v in enumerate(rows) if i % 192 >= 64]


def _session(n: int, stamp: Callable[[int], float],
             rows: Callable[[str, int], int] = lambda pid, k: FRAME_ROWS[pid],
             channels: tuple[str, ...] = (ECG, PLETH, RESP)) -> tuple[Feed, dict[str, list[int]]]:
    """n frames, frame k stamped stamp(k) with rows(pid, k) rows (0 = lost).
    Returns the capture rows per channel and the EDF samples each channel should
    hold, record k for frame k."""
    feed: Feed = {pid: [] for pid in channels}
    want: dict[str, list[int]] = {pid: [] for pid in channels}
    for pid in channels:
        n0 = 0
        for k in range(n):
            m = rows(pid, k)
            if m:
                if pid == ECG:
                    r, real = _ecg_frame(n0, m)
                else:
                    r = real = [_val(n0 + i) for i in range(m)]
                feed[pid].append((stamp(k), r))
            else:
                real = []
            n0 += SPR[pid]
            dig = [round(v * w.EDF_DIG_MAX) for v in real]
            want[pid] += dig + [0] * (SPR[pid] - len(dig))
    return feed, want


def _export(tmp_path: Path, feed: Feed) -> tuple[Path, dict[str, Any]]:
    chans = []
    for pid, frames in feed.items():
        ch = w._EdfChannel(pid, 1.0, io.BytesIO())
        for s, rows in frames:
            for j, v in enumerate(rows):
                ch.add(s + 0.001 * (j * 4 // len(rows)), v)
        ch.flush()
        chans.append(ch)
    out = tmp_path / "waveforms.edf"
    with open(out, "wb") as o:
        stats = w._edf_write(o, chans, "X", "VSCC session 0 (times UTC) test")
    return out, stats


def _parse(path: Path) -> tuple[dict[str, Any], dict[str, list[int]]]:
    """Header fields and each channel's samples, from the raw bytes."""
    raw = path.read_bytes()
    hdr_bytes, n_rec, ns = int(raw[184:192]), int(raw[236:244]), int(raw[252:256])
    assert hdr_bytes == 256 * (ns + 1)
    labels = [raw[256 + 16 * i:272 + 16 * i].decode().strip() for i in range(ns)]
    at = 256 + ns * 216
    spr = [int(raw[at + 8 * i:at + 8 * i + 8]) for i in range(ns)]
    assert len(raw) == hdr_bytes + 2 * n_rec * sum(spr)
    data = array("h")
    data.frombytes(raw[hdr_bytes:])
    sig: dict[str, list[int]] = {lb: [] for lb in labels}
    pos = 0
    for _ in range(n_rec):
        for lb, n in zip(labels, spr, strict=True):
            sig[lb] += data[pos:pos + n].tolist()
            pos += n
    head = {"n_records": n_rec, "duration": raw[244:252].decode().strip(),
            "labels": labels, "spr": spr, "start": raw[168:184].decode()}
    return head, sig


def _label(pid: str) -> str:
    return pid.removeprefix("NOM_")


def _check_counts(stats: dict[str, Any], sig: dict[str, list[int]]) -> None:
    """Real samples in the EDF == capture rows minus copy rows, per channel."""
    for c in stats["channels"]:
        assert c["samples"] == c["source_rows"] - c["copy_rows_dropped"]
        assert len(sig[c["label"]]) == c["samples"] + c["padded_samples"] + c["flat_records"] * c["samples_per_record"]


# --- helpers -------------------------------------------------------------------

def test_edf_real_rows() -> None:
    assert w._edf_real_rows(768, True) == 512
    assert w._edf_real_rows(767, True) == 511
    assert w._edf_real_rows(384, True) == 256
    assert w._edf_real_rows(64, True) == 0
    assert w._edf_real_rows(100, True) == 36
    assert w._edf_real_rows(128, False) == 128


def test_edf_duration_fills_the_8_char_field() -> None:
    assert w._edf_duration(1.0244) == "1.024400"
    assert w._edf_duration(1.02443049) == "1.024430"
    assert w._edf_duration(10.5) == "10.50000"
    assert w._edf_duration(99999.25) == "99999.25"


# --- export ----------------------------------------------------------------------

def test_ecg_copy_rows_dropped_at_true_rate(tmp_path: Path) -> None:
    feed, want = _session(10, lambda k: T0 + k * P, channels=(ECG, PLETH))
    path, stats = _export(tmp_path, feed)
    head, sig = _parse(path)

    assert head["n_records"] == 10
    assert head["duration"] == "1.024400"
    assert head["labels"] == ["ECG_ELEC_POTL_II", "PLETH"]
    assert head["spr"] == [512, 128]
    assert sig == {_label(pid): v for pid, v in want.items()}

    ecg, pleth = stats["channels"]
    assert ecg["source_rows"] == 7680
    assert ecg["copy_rows_dropped"] == 2560
    assert ecg["samples"] == 5120
    assert pleth["copy_rows_dropped"] == 0
    assert ecg["rate_hz"] == round(512 / 1.0244, 3)
    assert stats["record_duration_s"] == 1.0244
    _check_counts(stats, sig)


def test_gap_free_capture_has_no_padding(tmp_path: Path) -> None:
    # Arrival jitter of +-30 ms, two frames 0.6 and 0.7 s late, and frame 30 so
    # late that frame 31 lands in the same burst: still one record per frame.
    late = {10: 0.6, 20: 0.7, 30: 0.8}

    def stamp(k: int) -> float:
        jitter = 0.0 if k in (0, 39) else ((k * 37) % 61 - 30) / 1000
        return T0 + k * P + jitter + late.get(k, 0.0)

    feed, want = _session(40, stamp)
    path, stats = _export(tmp_path, feed)
    head, sig = _parse(path)

    assert head["n_records"] == 40
    assert head["duration"] == "1.024400"
    assert sig == {_label(pid): v for pid, v in want.items()}
    for c in stats["channels"]:
        assert c["frames"] == 40
        assert (c["padded_samples"], c["flat_records"], c["missing_frames"]) == (0, 0, 0)
    _check_counts(stats, sig)


def test_period_far_from_nominal_adds_no_gaps(tmp_path: Path) -> None:
    # 1.030 s frames over 2000 frames drift 12 s off the nominal 1.024 grid.
    feed, want = _session(2000, lambda k: T0 + k * 1.03 + ((k * 7) % 11 - 5) / 1000, channels=(PLETH,))
    path, stats = _export(tmp_path, feed)
    head, sig = _parse(path)

    assert head["n_records"] == 2000
    assert abs(stats["frame_period_s"] - 1.03) < 1e-5
    assert stats["channels"][0]["flat_records"] == 0
    assert sig["PLETH"] == want[PLETH]


def test_partial_and_missing_frames(tmp_path: Path) -> None:
    # Frame 4 arrives half (ECG 384 rows, pleth 64, resp 32), frame 8 is lost on
    # every channel, and the last ECG frame is one row short (767).
    def rows(pid: str, k: int) -> int:
        if k == 8:
            return 0
        if k == 4:
            return FRAME_ROWS[pid] // 2
        if k == 11 and pid == ECG:
            return 767
        return FRAME_ROWS[pid]

    feed, want = _session(12, lambda k: T0 + k * P, rows)
    path, stats = _export(tmp_path, feed)
    head, sig = _parse(path)

    assert head["n_records"] == 12
    assert head["duration"] == "1.024400"
    assert sig == {_label(pid): v for pid, v in want.items()}
    assert sig["ECG_ELEC_POTL_II"][8 * 512:9 * 512] == [0] * 512

    by = {c["label"]: c for c in stats["channels"]}
    assert by["ECG_ELEC_POTL_II"]["padded_samples"] == 256 + 1
    assert by["PLETH"]["padded_samples"] == 64
    assert by["RESP"]["padded_samples"] == 32
    for c in stats["channels"]:
        assert (c["frames"], c["flat_records"], c["missing_frames"]) == (11, 1, 1)
    _check_counts(stats, sig)


def test_pyedflib_reads_the_true_rates(tmp_path: Path) -> None:
    pyedflib = pytest.importorskip("pyedflib")
    feed, want = _session(6, lambda k: T0 + k * P)
    path, stats = _export(tmp_path, feed)

    f = pyedflib.EdfReader(str(path))
    try:
        assert f.datarecords_in_file == 6
        assert f.getSignalLabels() == ["ECG_ELEC_POTL_II", "PLETH", "RESP"]
        for i, (pid, spr) in enumerate(SPR.items()):
            assert f.getSampleFrequency(i) == pytest.approx(spr / 1.0244)
            assert f.readSignal(i, digital=True).tolist() == want[pid]
        assert f.getFileDuration() == pytest.approx(6 * 1.0244)
    finally:
        f.close()
