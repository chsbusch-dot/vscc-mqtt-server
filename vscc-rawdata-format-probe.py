#!/usr/bin/env python3
"""
vscc-rawdata-format-probe.py — Identify and summarize the *format* of VSCapture
export files.

Where vscc-rawdata-spotcheck.py answers "is there live data in here?", this
answers "what IS this file?" — useful when deciding how to parse an export or
feed it into a pipeline. It reports for:

  * DataExportVSC.json — framing (single JSON array vs. line-delimited arrays /
    NDJSON), UTF-8 BOM, record count, per-record schema, batch sizes, DeviceIDs,
    distinct PhysioIDs, and the captured time span.

  * MPrawoutput.txt — the raw Philips IntelliVue / IEEE-11073 Data Export stream:
    hex-dump structure, BOM, frame (line) count, frame-size distribution (so you
    can see the waveform sample-array frames vs. numerics/keepalive), total
    decoded protocol bytes, and presence of the IEEE/Philips OID arc.

Companion to vscc-rawdata-spotcheck.py (same capture-dir convention).

Usage:
    python3 vscc-rawdata-format-probe.py                 # known files in capture dir
    python3 vscc-rawdata-format-probe.py FILE [FILE ...] # specific files

The capture directory defaults to ./VSCapture and can be overridden with the
VSC_CAPTURE_DIR environment variable (same as vscc-file-cleanup.py).
"""

import collections
import json
import os
import sys

# IEEE 11073 / Philips OID arc (1.2.840.x...) seen in IntelliVue export frames.
OID_ARC = bytes.fromhex("2A8648CE14")


def describe_json(path):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    print(f"\n{'='*74}\n{name}   ({size/1e6:.1f} MB)\n{'='*74}")

    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw[:3] == b"\xef\xbb\xbf"
    text = raw[3:].decode("utf-8", "replace") if bom else raw.decode("utf-8", "replace")
    print(f"UTF-8 BOM: {bom}")

    # Framing: a single JSON document, or one JSON value per line (NDJSON /
    # line-delimited arrays, which VSCapture emits — one array per flush).
    records = []
    batch_sizes = []
    try:
        doc = json.loads(text)
        if isinstance(doc, list):
            records = doc
            framing = "single JSON array"
        else:
            records = [doc]
            framing = "single JSON object"
    except json.JSONDecodeError:
        nlines = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            nlines += 1
            val = json.loads(line)
            if isinstance(val, list):
                batch_sizes.append(len(val))
                records.extend(val)
            else:
                records.append(val)
        framing = f"line-delimited JSON {'arrays' if batch_sizes else 'objects'} (NDJSON, {nlines:,} lines)"

    print(f"framing: {framing}")
    print(f"total records: {len(records):,}")
    if batch_sizes:
        print(f"records per line/batch: min {min(batch_sizes)}, "
              f"max {max(batch_sizes)}, avg {sum(batch_sizes)/len(batch_sizes):.1f}")
    if not records:
        print("  (no records)")
        return
    print(f"record schema: {list(records[0].keys())}")

    devs = collections.Counter(r.get("DeviceID") for r in records)
    pids = collections.Counter(r.get("PhysioID") for r in records)
    print(f"DeviceID counts: {dict(devs)}")
    print(f"distinct PhysioIDs: {len(pids)}")
    times = [r.get("SystemLocalTime") for r in records if r.get("SystemLocalTime")]
    if times:
        print(f"time span (SystemLocalTime): {times[0]} -> {times[-1]}")
    # show one record and the first with a real (non '-') value
    print("\nfirst record:")
    print(json.dumps(records[0], indent=2))
    for r in records:
        if str(r.get("Value")).strip() not in ("-", "", "None"):
            print("first record with a numeric Value:")
            print(json.dumps(r, indent=2))
            break


def describe_mpraw(path):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    print(f"\n{'='*74}\n{name}   ({size/1e6:.0f} MB)\n{'='*74}")

    with open(path, "rb") as fh:
        head = fh.read(8192)
    bom = head[:3] == b"\xef\xbb\xbf"
    htxt = head[3:].decode("ascii", "replace") if bom else head.decode("ascii", "replace")
    print(f"UTF-8 BOM: {bom}")

    first_line = htxt.splitlines()[0] if htxt.splitlines() else ""
    line_toks = [t for t in first_line.split("-") if t]
    toks = [t for t in htxt.replace("\r", "").replace("\n", "-").split("-") if t]
    is_hex = all(len(t) == 2 and all(c in "0123456789abcdefABCDEF" for c in t)
                 for t in toks[:60])
    print(f"encoding: dash-separated {'2-char HEX bytes' if is_hex else 'UNKNOWN tokens'}")
    preview = "-".join(line_toks[:24])
    print(f"first frame (hex): {preview}{'...' if len(line_toks) > 24 else ''}")

    # frame (line) size histogram + totals — one streaming pass
    buckets = collections.Counter()
    nframes = 0
    total_bytes = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            nframes += 1
            b = (len(line) + 1) // 3  # "XX-" ~= 3 chars per byte
            total_bytes += b
            if b < 20:
                buckets["<20  (keepalive/ack)"] += 1
            elif b < 60:
                buckets["20-59  (small)"] += 1
            elif b < 150:
                buckets["60-149  (numerics)"] += 1
            elif b < 400:
                buckets["150-399  (medium)"] += 1
            else:
                buckets[">=400  (wave sample-arrays)"] += 1

    print(f"frames (lines): {nframes:,}")
    print(f"decoded protocol bytes: ~{total_bytes/1e6:.0f} MB")
    print("frame-size distribution:")
    for k in ["<20  (keepalive/ack)", "20-59  (small)", "60-149  (numerics)",
              "150-399  (medium)", ">=400  (wave sample-arrays)"]:
        if buckets.get(k):
            print(f"   {k:32} {buckets[k]:>9,}")

    # OID-arc presence on a sampled chunk (decoding 145 MB fully is wasteful)
    sample = bytes(int(t, 16) for t in toks if len(t) == 2 and is_hex)
    with open(path, "rb") as fh:
        chunk = fh.read(4_000_000)
    try:
        hx = "".join(c for c in chunk.decode("ascii", "ignore") if c in "0123456789abcdefABCDEF")
        if len(hx) % 2:
            hx = hx[:-1]
        present = OID_ARC in bytes.fromhex(hx)
    except Exception:
        present = OID_ARC in sample
    print(f"IEEE/Philips OID arc 2A-86-48-CE-14 present (first ~4MB): {present}")
    print("=> raw IEEE-11073 IntelliVue Data Export stream; CSV/JSON are decoded from this.")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.getenv("VSC_CAPTURE_DIR", os.path.join(BASE_DIR, "VSCapture"))
KNOWN = ["DataExportVSC.json", "MPrawoutput.txt"]


def probe(path):
    if not os.path.exists(path):
        print(f"\n!! not found: {path}")
        return
    low = path.lower()
    if low.endswith(".json"):
        describe_json(path)
    elif "mprawoutput" in os.path.basename(low) or low.endswith(".txt"):
        describe_mpraw(path)
    elif low.endswith(".csv"):
        print(f"\n{os.path.basename(path)}: waveform CSV — use vscc-rawdata-spotcheck.py")
    else:
        print(f"\n?? unhandled file type, skipping: {path}")


def main():
    files = sys.argv[1:] or [os.path.join(CAPTURE_DIR, f) for f in KNOWN]
    for path in files:
        probe(path)


if __name__ == "__main__":
    main()
