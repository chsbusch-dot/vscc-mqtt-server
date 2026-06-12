"""Derived ECG metrics: R-peak detection (Pan-Tompkins) and HRV.

Pure NumPy/SciPy, no I/O — so it unit-tests against synthetic ECG and the
worker can call analyze_ecg() with samples pulled from TimescaleDB.

Gap handling is first-class: VSCapture sessions have data gaps (capture
restarts, monitor off), so an RR interval that spans a gap is not a real
beat-to-beat interval. SDNN is computed over all physiological NN intervals;
the successive-difference metrics (RMSSD, pNN50, Poincaré SD1) are computed
only WITHIN contiguous runs, never across a gap — the methodologically
correct treatment.
"""
from typing import Dict, Any, List, Tuple, Optional
import numpy as np
from scipy.signal import butter, filtfilt, find_peaks
from scipy.ndimage import uniform_filter1d, maximum_filter1d, median_filter

# Physiological RR bounds (seconds): 30–200 bpm. Anything outside is an
# artifact or spans a gap, and is excluded from HRV.
RR_MIN_S = 0.30
RR_MAX_S = 2.00
# Standard HRV artifact correction: reject an NN interval deviating more than
# this fraction from the local median (ectopic beats, missed/extra detections).
ARTIFACT_TOL = 0.20
MAX_POINCARE_POINTS = 2000


def _accept_mask(rr: np.ndarray) -> np.ndarray:
    """Accept physiological NN intervals that are not local outliers. Gap-spanning
    intervals fail the physiological test; ectopics and detection errors fail the
    local-median test — both required before computing HRV."""
    phys = (rr >= RR_MIN_S) & (rr <= RR_MAX_S)
    accept = phys.copy()
    idx = np.flatnonzero(phys)
    if idx.size >= 5:
        vals = rr[idx]
        med = median_filter(vals, size=5, mode="nearest")
        accept[idx] = np.abs(vals - med) <= ARTIFACT_TOL * med
    return accept


def reconstruct_sample_times(times: np.ndarray) -> Tuple[np.ndarray, float]:
    """VSCapture stamps a whole frame of samples with ONE timestamp (≈256
    samples per frame, frames irregularly spaced with data gaps). Per-sample
    timing must therefore be reconstructed: each frame's samples are spread
    uniformly over the frame's nominal duration, and a real time gap is left
    where the monitor dropped out — so an RR interval that spans a gap stays
    huge and is excluded downstream rather than silently bridging the gap.

    Returns (per-sample times, estimated true sample rate fs)."""
    times = np.asarray(times, dtype=float)
    n = times.size
    if n < 2:
        return times, 0.0
    # True sample rate is robustly samples / active-seconds (the per-sample
    # stamps are frame-quantized, so their spacing is not the rate). This
    # matches the signal's nominal rate even with gaps and bursty stamping.
    active_seconds = max(1, np.unique(np.floor(times)).size)
    fs = n / active_seconds
    if not np.isfinite(fs) or fs <= 0:
        fs = 500.0

    # Within a contiguous run the monitor samples uniformly at fs, so per-sample
    # timing is a SMOOTH index clock (i/fs) — NOT the frame stamps, whose ms
    # jitter and irregular spacing would otherwise inject spurious beat-to-beat
    # variability into the RR series. The clock only jumps at a REAL data gap:
    # a frame-to-frame stamp jump far larger than the frame's samples account
    # for. RR intervals straddling such a jump stay large and are excluded.
    starts = np.concatenate(([0], np.flatnonzero(np.diff(times) > 0) + 1))
    counts = np.diff(np.concatenate((starts, [n])))
    t0 = times[starts]
    step = 1.0 / fs
    out = np.empty(n)
    clock = float(t0[0])
    nframes = starts.size
    for i in range(nframes):
        s, c = starts[i], counts[i]
        out[s:s + c] = clock + np.arange(c) * step
        clock = out[s + c - 1] + step
        if i + 1 < nframes:
            gap = t0[i + 1] - t0[i]
            if gap > max(3.0 * c * step, 1.0):   # real gap: honor it
                clock = float(t0[i + 1])
    return out, fs


def detect_r_peaks(values: np.ndarray, fs: float) -> np.ndarray:
    """Pan-Tompkins style QRS detector. Returns sample indices of R-peaks.

    Bandpass 5–15 Hz → derivative → square → moving-window integrate → peak
    pick with a 250 ms refractory period and an ADAPTIVE (rolling) height, so
    detection survives 45 min of non-stationary, bursty, gappy data where a
    single global threshold would miss most beats."""
    if fs < 20 or values.size < int(fs):
        return np.array([], dtype=int)

    nyq = fs / 2.0
    lo, hi = 5.0 / nyq, min(15.0 / nyq, 0.99)
    b, a = butter(1, [lo, hi], btype="band")
    filtered = filtfilt(b, a, values)

    diff = np.ediff1d(filtered, to_begin=0.0)
    squared = diff * diff
    integrated = uniform_filter1d(squared, max(1, int(0.150 * fs)))

    # Automatic gain control: threshold tracks the local QRS amplitude (rolling
    # max over ~2 s) at 0.40, with a global floor so flat/no-signal stretches and
    # gaps don't produce noise detections. A 300 ms refractory (≤200 bpm) keeps
    # T-waves from being counted as beats — validated against real MP50 ECG.
    local_max = maximum_filter1d(integrated, max(1, int(2.0 * fs)))
    floor = 0.05 * np.percentile(integrated, 99)
    threshold = np.maximum(0.40 * local_max, floor)
    distance = max(1, int(0.30 * fs))
    peaks, _ = find_peaks(integrated, height=threshold, distance=distance)

    # Refine each detection to the true R-peak (local max of the bandpassed
    # signal nearby); the MWI peak lags the QRS by ~half the window.
    search = max(1, int(0.05 * fs))
    refined = [max(0, p - search) + int(np.argmax(filtered[max(0, p - search):min(values.size, p + search)]))
               for p in peaks]
    return np.unique(np.array(refined, dtype=int)) if refined else np.array([], dtype=int)


def _segments(rr: np.ndarray, valid: np.ndarray) -> List[np.ndarray]:
    """Split the RR series into contiguous runs of physiological intervals."""
    segs, cur = [], []
    for i, ok in enumerate(valid):
        if ok:
            cur.append(rr[i])
        elif cur:
            segs.append(np.array(cur)); cur = []
    if cur:
        segs.append(np.array(cur))
    return segs


def compute_hrv(rr_seconds: np.ndarray) -> Dict[str, Any]:
    """Time-domain HRV + Poincaré from an RR series (seconds), gap- and
    artifact-aware (see _accept_mask)."""
    accept = _accept_mask(rr_seconds)
    nn = rr_seconds[accept]
    if nn.size < 2:
        return {"beats": int(nn.size), "insufficient": True}

    nn_ms = nn * 1000.0
    # Successive differences only within contiguous runs of accepted intervals
    # (never across a gap, ectopic, or detection error).
    segs = _segments(rr_seconds, accept)
    succ = np.concatenate([np.diff(s * 1000.0) for s in segs if s.size >= 2]) \
        if any(s.size >= 2 for s in segs) else np.array([])

    sdnn = float(np.std(nn_ms, ddof=1))
    rmssd = float(np.sqrt(np.mean(succ ** 2))) if succ.size else None
    pnn50 = float(100.0 * np.mean(np.abs(succ) > 50.0)) if succ.size else None
    sd1 = float(np.sqrt(0.5) * np.std(succ, ddof=1)) if succ.size > 1 else None
    sd2 = float(np.sqrt(max(0.0, 2.0 * sdnn ** 2 - (sd1 ** 2 if sd1 else 0.0)))) \
        if sd1 is not None else None

    return {
        "beats": int(nn.size),
        "mean_hr_bpm": round(60.0 / float(np.mean(nn)), 1),
        "mean_rr_ms": round(float(np.mean(nn_ms)), 1),
        "sdnn_ms": round(sdnn, 1),
        "rmssd_ms": round(rmssd, 1) if rmssd is not None else None,
        "pnn50_pct": round(pnn50, 1) if pnn50 is not None else None,
        "sd1_ms": round(sd1, 1) if sd1 is not None else None,
        "sd2_ms": round(sd2, 1) if sd2 is not None else None,
        "insufficient": False,
    }


def poincare_points(rr_seconds: np.ndarray) -> List[Tuple[float, float]]:
    """(RR[n], RR[n+1]) pairs in ms for plotting, within-run, downsampled."""
    pairs = []
    for seg in _segments(rr_seconds, _accept_mask(rr_seconds)):
        ms = seg * 1000.0
        pairs.extend(zip(ms[:-1], ms[1:]))
    if len(pairs) > MAX_POINCARE_POINTS:
        idx = np.linspace(0, len(pairs) - 1, MAX_POINCARE_POINTS).astype(int)
        pairs = [pairs[i] for i in idx]
    return [(round(float(x), 1), round(float(y), 1)) for x, y in pairs]


def analyze_ecg(times: np.ndarray, values: np.ndarray,
                fs: Optional[float] = None) -> Dict[str, Any]:
    """Full pipeline: ECG samples → R-peaks → RR series → HRV + Poincaré."""
    times = np.asarray(times, dtype=float)
    values = np.asarray(values, dtype=float)
    # Reconstruct per-sample times from the frame-quantized stamps (and the
    # true fs) unless the caller already supplied a sample rate.
    recon_times, est_fs = reconstruct_sample_times(times)
    if fs is None:
        fs = est_fs
        times = recon_times
    if fs < 20 or values.size < int(fs):
        return {"ok": False, "error": "not enough ECG data to analyze", "fs_hz": round(fs, 1)}

    peaks = detect_r_peaks(values, fs)
    if peaks.size < 3:
        return {"ok": False, "error": "no reliable R-peaks detected", "fs_hz": round(fs, 1)}

    peak_times = times[peaks]
    rr = np.diff(peak_times)
    accept = _accept_mask(rr)

    hrv = compute_hrv(rr)
    return {
        "ok": True,
        "fs_hz": round(fs, 1),
        "r_peaks": int(peaks.size),
        "rr_intervals": int(rr.size),
        "rr_accepted": int(np.sum(accept)),
        "rr_rejected": int(np.sum(~accept)),
        "hrv": hrv,
        "poincare": poincare_points(rr),
        "first_beat": float(peak_times[0]),
        "last_beat": float(peak_times[-1]),
    }
