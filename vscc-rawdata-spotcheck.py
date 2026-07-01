#!/usr/bin/env python3
"""
vscc-rawdata-spotcheck.py — Decide whether VSCapture exports contain real
vital-sign data, a flatline, or noise WITHOUT reading the whole file.

The VSCaptureCLI tool produces multi-GB waveform CSVs. Reading one whole just
to learn "is there a real trace in here?" is wasteful. This tool answers that
two ways:

  * fast sampler (default) — seek to N evenly-spaced byte offsets, read a small
    window at each, and compute stats on the value column. Sub-second even on a
    2 GB file. Caveat: if signal is sparse/intermittent it can be missed.

  * streaming profile (--profile) — one pass, constant memory, reporting the
    distinct-value count per segment. Slower but CANNOT miss intermittent
    signal. Use when you need certainty.

Companion to vscc-file-cleanup.py (same target files, same capture dir).

Usage:
    python3 vscc-rawdata-spotcheck.py                 # all known files in capture dir
    python3 vscc-rawdata-spotcheck.py FILE [FILE ...] # specific files
    python3 vscc-rawdata-spotcheck.py --profile FILE  # definitive streaming scan

The capture directory defaults to ./VSCapture and can be overridden with the
VSC_CAPTURE_DIR environment variable (same convention as vscc-file-cleanup.py).
"""

import argparse
import math
import os

# ----- statistics on one window of numeric samples -------------------------

def window_stats(values):
    n = len(values)
    if n == 0:
        return None
    vmin = min(values)
    vmax = max(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(var)
    uniq = len(set(values))

    # first differences: structure vs. noise
    diffs = [values[i + 1] - values[i] for i in range(n - 1)]
    if diffs:
        dmean = sum(diffs) / len(diffs)
        dstd = math.sqrt(sum((d - dmean) ** 2 for d in diffs) / len(diffs))
    else:
        dstd = 0.0
    # roughness = std(diff)/std(signal): ~0 smooth/periodic, ~1.4 white noise
    roughness = (dstd / std) if std > 1e-12 else 0.0
    # direction changes per sample: oscillation density
    dirchg = 0
    for i in range(1, len(diffs)):
        if (diffs[i] > 0) != (diffs[i - 1] > 0) and diffs[i] != 0 and diffs[i - 1] != 0:
            dirchg += 1
    dirchg_rate = dirchg / len(diffs) if diffs else 0.0

    return dict(n=n, min=vmin, max=vmax, mean=mean, std=std, ptp=vmax - vmin,
                uniq=uniq, roughness=roughness, dirchg=dirchg_rate)

def classify(s, flat_eps):
    """Per-window label from the stats dict.

    A real physiologic waveform must (a) take many distinct values and
    (b) actually oscillate. A dead trace that toggles between 1-2 adjacent
    quantization levels is NOT signal — it's a flatline with dither.
    """
    if s is None:
        return "EMPTY"
    if s["uniq"] <= 1 or s["ptp"] <= flat_eps:
        return "FLAT"
    # toggles between only a couple of levels and/or never really oscillates:
    # a dead lead with LSB dither, not a waveform
    if s["uniq"] < 5 or s["dirchg"] < 0.02:
        return "NEAR-FLAT"
    # genuine variation present
    if s["roughness"] > 0.7 and s["dirchg"] > 0.4:
        return "NOISE?"
    return "SIGNAL"

# ----- value extraction ----------------------------------------------------

def parse_value_csv(line):
    """VSCapture wave CSV: ts,relclock,localtime,VALUE, -> field index 3."""
    parts = line.rstrip("\r\n").split(",")
    if len(parts) < 4:
        return None
    raw = parts[3].strip()
    if raw == "" or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None

# ----- seek-based window sampler -------------------------------------------

def sample_windows(path, n_windows, win_lines, parser):
    size = os.path.getsize(path)
    results = []
    with open(path, "rb") as fh:
        for w in range(n_windows):
            # spread offsets across the file; leave room at the end for a window
            frac = w / max(n_windows - 1, 1)
            off = int(frac * size * 0.999)
            fh.seek(off)
            if off > 0:
                fh.readline()  # discard partial line
            vals, missing, raw_lines = [], 0, 0
            for _ in range(win_lines):
                bline = fh.readline()
                if not bline:
                    break
                raw_lines += 1
                try:
                    line = bline.decode("utf-8", "replace")
                except Exception:
                    continue
                v = parser(line)
                if v is None:
                    missing += 1
                else:
                    vals.append(v)
            results.append((off, raw_lines, missing, window_stats(vals)))
    return size, results

# ----- per-file report (fast sampler) --------------------------------------

def report_csv(path, n_windows, win_lines, flat_eps):
    name = os.path.basename(path)
    size, results = sample_windows(path, n_windows, win_lines, parse_value_csv)
    print(f"\n{'='*78}\n{name}   ({size/1e6:.0f} MB)\n{'='*78}")
    print(f"{'offset%':>7} {'n':>5} {'min':>9} {'max':>9} {'mean':>9} "
          f"{'std':>9} {'p2p':>9} {'uniq':>5} {'rough':>6} {'osc':>5}  verdict")
    labels = []
    for off, raw_lines, missing, s in results:
        pct = 100.0 * off / size if size else 0
        if s is None:
            print(f"{pct:6.1f}% {0:5d} {'-':>9} {'-':>9} {'-':>9} {'-':>9} "
                  f"{'-':>9} {'-':>5} {'-':>6} {'-':>5}  EMPTY/NO-DATA")
            labels.append("EMPTY")
            continue
        lab = classify(s, flat_eps)
        labels.append(lab)
        print(f"{pct:6.1f}% {s['n']:5d} {s['min']:9.3g} {s['max']:9.3g} "
              f"{s['mean']:9.3g} {s['std']:9.3g} {s['ptp']:9.3g} {s['uniq']:5d} "
              f"{s['roughness']:6.2f} {s['dirchg']:5.2f}  {lab}")
    sig = labels.count("SIGNAL")
    flat = labels.count("FLAT")
    near = labels.count("NEAR-FLAT")
    noise = labels.count("NOISE?")
    empty = labels.count("EMPTY")
    tot = len(labels)
    print(f"\n  summary: {sig} signal / {near} near-flat / {flat} flat / "
          f"{noise} noise? / {empty} empty  (of {tot} windows)")
    if sig >= max(2, tot * 0.15):
        verdict = "HAS REAL DATA (live waveform present in part of the file)"
    elif noise > sig and noise >= max(2, tot * 0.15):
        verdict = "MOSTLY NOISE — inspect before trusting"
    elif flat + near + empty >= tot * 0.95:
        verdict = "FLATLINE / DEAD TRACE — no usable waveform"
    else:
        verdict = "MIXED — partial signal, review windows above"
    print(f"  >>> {verdict}")
    print(f"  (note: fast sampler can miss sparse/intermittent signal; "
          f"re-run with --profile to be certain)")
    return verdict

# ----- streaming flatness profile (definitive; one pass, constant memory) --

def profile_csv(path, seg_lines):
    """Read the file ONCE, line by line, and report per-segment variability.
    Constant memory (only distinct values within the current segment are held).
    Unlike the seek-sampler this CANNOT miss sparse/intermittent signal."""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    print(f"\n{'='*78}\n{name}   ({size/1e6:.0f} MB)  [streaming profile, "
          f"{seg_lines:,}-line segments]\n{'='*78}")
    print(f"{'segment':>7} {'lines':>20} {'distinct':>8} {'min':>10} "
          f"{'max':>10}  live?")
    live_segments = 0
    total_distinct = set()
    seg = 0
    seen = set()
    lo = 1
    mn = float("inf")
    mx = float("-inf")
    n = 0

    def flush(seg, lo, hi, seen, mn, mx):
        nonlocal live_segments
        d = len(seen)
        live = d > 5
        if live:
            live_segments += 1
        print(f"{seg:7d} {f'{lo:,}-{hi:,}':>20} {d:8d} "
              f"{mn:10.4g} {mx:10.4g}  {'<-- LIVE' if live else ''}")

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n += 1
            v = parse_value_csv(line)
            cur = (n - 1) // seg_lines
            if cur != seg:
                flush(seg, lo, n - 1, seen, mn, mx)
                seen = set()
                lo = n
                mn = float("inf")
                mx = float("-inf")
                seg = cur
            if v is not None:
                if len(total_distinct) < 100000:
                    total_distinct.add(v)
                seen.add(v)
                if v < mn:
                    mn = v
                if v > mx:
                    mx = v
        flush(seg, lo, n, seen, mn, mx)

    td = len(total_distinct)
    print(f"\n  total samples: {n:,}   distinct values (whole file): "
          f"{td if td < 100000 else '100000+'}")
    if live_segments == 0 and td <= 3:
        print(f"  >>> FLATLINE / DEAD TRACE — file is essentially constant")
    elif live_segments == 0:
        print(f"  >>> NEAR-FLAT — only a few quantization levels, no real waveform")
    else:
        print(f"  >>> HAS REAL DATA — {live_segments} live segment(s); "
              f"see rows marked LIVE above")

# ----- JSON numerics sampler (lightweight, record-based) -------------------

def report_json(path, n_windows, win_bytes=200_000):
    """Sample the numeric/parameter JSON: count records whose Value is a real
    number vs. '-'. Doesn't parse the whole array; reads byte windows and
    regex-scans for "PhysioID":"..","Value":".." pairs."""
    import re
    from collections import defaultdict
    name = os.path.basename(path)
    size = os.path.getsize(path)
    pat = re.compile(r'"PhysioID":"([^"]*)","Value":"([^"]*)"')
    print(f"\n{'='*78}\n{name}   ({size/1e6:.1f} MB)  [JSON numerics]\n{'='*78}")
    real = defaultdict(int)
    dash = defaultdict(int)
    samples = defaultdict(list)
    with open(path, "rb") as fh:
        for w in range(n_windows):
            off = int((w / max(n_windows - 1, 1)) * max(size - win_bytes, 0))
            fh.seek(off)
            chunk = fh.read(win_bytes).decode("utf-8", "replace")
            for pid, val in pat.findall(chunk):
                v = val.strip()
                if v in ("", "-"):
                    dash[pid] += 1
                else:
                    real[pid] += 1
                    if len(samples[pid]) < 3:
                        samples[pid].append(v)
    pids = sorted(set(real) | set(dash))
    if not pids:
        print("  no PhysioID/Value pairs found in sampled windows")
        return
    print(f"{'PhysioID':38} {'real':>6} {'dash':>6}   examples")
    any_real = False
    for pid in pids:
        r, d = real[pid], dash[pid]
        if r:
            any_real = True
        ex = ", ".join(samples[pid][:3])
        print(f"{pid:38} {r:6d} {d:6d}   {ex}")
    print(f"\n  >>> {'HAS NUMERIC VITALS' if any_real else 'ALL VALUES BLANK/-  (no numerics)'}")

# ----- main ----------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.getenv("VSC_CAPTURE_DIR", os.path.join(BASE_DIR, "VSCapture"))
KNOWN = [
    "NOM_ECG_ELEC_POTL_IIWaveExport.csv",
    "NOM_PLETHWaveExport.csv",
    "NOM_RESPWaveExport.csv",
    "DataExportVSC.json",
]

def main():
    ap = argparse.ArgumentParser(
        description="Spot-check VSCapture exports for real vitals vs. flatline/noise.")
    ap.add_argument("files", nargs="*",
                    help="files to check (default: known exports in capture dir)")
    ap.add_argument("--windows", type=int, default=20,
                    help="number of seek windows for the fast sampler")
    ap.add_argument("--win-lines", type=int, default=3000,
                    help="lines read per seek window")
    ap.add_argument("--flat-eps", type=float, default=1e-9,
                    help="peak-to-peak below this counts as flat")
    ap.add_argument("--profile", action="store_true",
                    help="definitive single-pass streaming scan (slower, "
                         "catches sparse/intermittent signal the sampler misses)")
    ap.add_argument("--seg-lines", type=int, default=250000,
                    help="segment size for --profile mode")
    args = ap.parse_args()

    files = args.files or [os.path.join(CAPTURE_DIR, f) for f in KNOWN]
    for path in files:
        if not os.path.exists(path):
            print(f"\n!! not found: {path}")
            continue
        if path.lower().endswith(".json"):
            report_json(path, max(args.windows, 8))
        elif path.lower().endswith(".csv"):
            if args.profile:
                profile_csv(path, args.seg_lines)
            else:
                report_csv(path, args.windows, args.win_lines, args.flat_eps)
        else:
            print(f"\n?? unhandled file type, skipping: {path}")

if __name__ == "__main__":
    main()
