#!/usr/bin/env python3
"""Virtual MP50 — replays a recorded slice into VSCapture's export files so the
whole pipeline (worker → MQTT + TimescaleDB → VSCC Studio) runs live with NO
hardware. Lets forum visitors see real waveforms without a monitor.

Research/education demo only — the bundled slice is de-identified sample data.

It writes the same files the real capture writes, into the shared /data volume:
  - waveforms → NOM_<id>WaveExport.csv   (cols: stamp,rel,SystemLocalTime,value,)
  - numerics  → DataExportVSC.json       (newline-delimited JSON record arrays)
The worker's tailers and waveform auto-discovery then handle everything exactly
as for a live monitor, so the demo exercises the real code paths.
"""
import csv
import gzip
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.getenv("VSC_DATA_DIR", "/data"))
DEMO_FILE = Path(os.getenv("DEMO_DATA", "/app/vscc-demo-data.csv.gz"))
DEVID = os.getenv("DEMO_DEVID", "mp50")
TICK = 0.1                              # write a frame every 100 ms, like the monitor
MAX_BYTES = 8 * 1024 * 1024            # truncate export files past this to bound disk


def vsc_now() -> str:
    # VSCapture stamp format; container runs TZ=UTC so this is UTC (worker
    # parses it with MONITOR_TZ=UTC — they must agree).
    return datetime.now().strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]


def load_events():
    """Returns (waves: physio_id -> [(offset, value)], nums: [(offset, pid, value)], loop_len)."""
    waves = defaultdict(list)
    nums = []
    with gzip.open(DEMO_FILE, "rt") as f:
        for row in csv.DictReader(f):
            off = float(row["offset_s"])
            if row["kind"] == "w":
                waves[row["physio_id"]].append((off, row["value"]))
            else:
                nums.append((off, row["physio_id"], row["value"]))
    for pid in waves:
        waves[pid].sort(key=lambda e: e[0])
    nums.sort(key=lambda e: e[0])
    last = [evs[-1][0] for evs in waves.values()] + ([nums[-1][0]] if nums else [])
    return waves, nums, (max(last) if last else 0.0)


def _append(path: Path, text: str):
    # Truncate when large; the worker detects the shrink and reopens at EOF.
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            open(path, "w").close()
    except OSError:
        pass
    with open(path, "a") as f:
        f.write(text)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    waves, nums, loop_len = load_events()
    if loop_len <= 0:
        raise SystemExit("demo data is empty")
    wave_files = {pid: DATA_DIR / f"{pid}WaveExport.csv" for pid in waves}
    num_file = DATA_DIR / "DataExportVSC.json"
    total = sum(len(v) for v in waves.values()) + len(nums)
    print(f"[Demo] virtual MP50: {total} samples across {len(waves)} waveforms + "
          f"{len({n[1] for n in nums})} numerics, looping a {loop_len:.1f}s slice", flush=True)

    loops = 0
    while True:
        start = time.time()
        wcur = {pid: 0 for pid in waves}
        ncur = 0
        while True:
            target = time.time() - start
            if target >= loop_len:
                break
            for pid, evs in waves.items():
                i = wcur[pid]
                buf = []
                while i < len(evs) and evs[i][0] <= target:
                    stamp = vsc_now()
                    buf.append(f"{stamp},0,{stamp},{evs[i][1]},\n")
                    i += 1
                if buf:
                    _append(wave_files[pid], "".join(buf))
                wcur[pid] = i
            recs = []
            while ncur < len(nums) and nums[ncur][0] <= target:
                stamp = vsc_now()
                recs.append({"Timestamp": stamp, "Relativetimestamp": "0",
                             "SystemLocalTime": stamp, "PhysioID": nums[ncur][1],
                             "Value": nums[ncur][2], "DeviceID": DEVID})
                ncur += 1
            if recs:
                _append(num_file, json.dumps(recs) + "\n")
            time.sleep(TICK)
        loops += 1
        if loops % 10 == 0:
            print(f"[Demo] {loops} loops replayed", flush=True)


if __name__ == "__main__":
    main()
