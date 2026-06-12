"""Unit tests for the HRV signal-processing module.

Runnable standalone (`python test_vscc_hrv.py`) or under pytest. Uses synthetic
ECG with a known heart rate and injected RR variability, so the asserts pin the
detector and the HRV math to ground truth.
"""
import numpy as np
import vscc_hrv as h


def _synth_ecg(fs=250.0, duration=120.0, mean_rr=0.8, sdnn_s=0.03,
               gap=None, seed=1):
    """Synthetic ECG: Gaussian QRS spikes at a known mean RR with variability,
    plus baseline wander and noise. Optionally drops a `gap`=(start,end) window."""
    rng = np.random.RandomState(seed)
    rr = mean_rr + rng.normal(0, sdnn_s, int(duration / mean_rr) + 10)
    beats = np.cumsum(rr)
    beats = beats[beats < duration]
    t = np.arange(0, duration, 1 / fs)
    ecg = np.zeros_like(t)
    for b in beats:
        ecg += 1.5 * np.exp(-((t - b) ** 2) / (2 * 0.012 ** 2))
    ecg += 0.05 * np.sin(2 * np.pi * 0.3 * t) + rng.normal(0, 0.01, t.size)
    keep = np.ones(t.size, bool)
    if gap:
        keep = ~((t >= gap[0]) & (t < gap[1]))
        beats = beats[(beats < gap[0]) | (beats >= gap[1])]
    return t[keep], ecg[keep], len(beats)


def test_known_heart_rate():
    t, ecg, n_beats = _synth_ecg(mean_rr=0.8)  # 75 bpm
    r = h.analyze_ecg(t, ecg, fs=250.0)
    assert r["ok"]
    assert abs(r["hrv"]["mean_hr_bpm"] - 75) < 3
    assert abs(r["r_peaks"] - n_beats) <= 2


def test_sdnn_tracks_injected_variability():
    # Low variability → low SDNN; high variability → high SDNN.
    t1, e1, _ = _synth_ecg(sdnn_s=0.01, seed=2)
    t2, e2, _ = _synth_ecg(sdnn_s=0.06, seed=3)
    sdnn_low = h.analyze_ecg(t1, e1, fs=250.0)["hrv"]["sdnn_ms"]
    sdnn_high = h.analyze_ecg(t2, e2, fs=250.0)["hrv"]["sdnn_ms"]
    assert sdnn_low < sdnn_high
    assert 5 < sdnn_low < 20
    assert 40 < sdnn_high < 80


def test_gap_does_not_create_phantom_interval():
    # A 20 s dropout must not produce a single huge RR bridging it.
    t, ecg, _ = _synth_ecg(duration=120.0, gap=(50.0, 70.0))
    r = h.analyze_ecg(t, ecg, fs=250.0)
    assert r["ok"]
    # No accepted NN interval should approach the 20 s gap length.
    assert r["hrv"]["mean_rr_ms"] < 1500
    assert r["hrv"]["sd1_ms"] is not None


def test_flatline_returns_no_beats():
    t = np.arange(0, 30, 1 / 250.0)
    flat = np.zeros_like(t)
    r = h.analyze_ecg(t, flat, fs=250.0)
    assert not r["ok"] or r["hrv"].get("insufficient")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all HRV tests passed")
