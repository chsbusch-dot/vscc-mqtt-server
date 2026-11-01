# MP50 data: what more can vscc show? (research, 2026-10)

WOR-987. A ranked, evidence-backed list of insights vscc could derive from the
ECG, SpO2, pleth, respiration and EEG data the MP50 already sends, tested against
our own recordings. This is a proposal to pick from. Nothing here is built.

## Framing

- **Personal wellness and research only.** Not diagnosis, not a medical device,
  not for clinical decisions (same line as `docs/ROADMAP.md`). Every number below
  would ship labelled "research estimate", never as a finding or an alarm.
- **No personal physiological values in this file.** The repo is public, so this
  doc carries method-level evidence only: sample rates, frame counts, detector
  agreement, differences between independent implementations, artifact shares and
  coincidence ratios. The per-night values (heart rate, HRV, SpO2 statistics,
  event counts) are in the private WOR-987 notes folder, outside the repo.
- **What "supports it" means here:** the signal exists at a usable rate in our
  stored data and a scratch prototype reproduced the method with a differential
  check against a second, independent implementation or the monitor's own
  numeric. Each claim points to a measurement ID (M1..M12) in
  [Measurements](#measurements), which gives the command.

## Premise: what data we actually have

### Where it lives

| Store | Content | Lifetime |
|---|---|---|
| TimescaleDB `patient_waveforms` on .212 | raw waveform rows | 72 h retention (policy `drop_after 72:00:00`, next run 2026-11-01 05:21Z) [M1] |
| TimescaleDB `patient_numerics` | ~1 Hz numerics | 72 h retention [M1] |
| `patient_waveforms_1min` (continuous aggregate) | per-minute min/max/avg | follows its source |
| Session packages, `/srv/vscc-data/sessions/<id>/` | `waveforms.parquet`, `numerics.parquet`, EDF | durable, the only long-term raw source |

Analysis below used copies of session packages 5, 12, 14 and 15 (read-only,
in a scratchpad). Sessions 12, 14 and 15 are full nights (8.5 h, 8.2 h and 7.2 h).
Session 5 is a 6-minute recording from June, the only one with EEG.

### Channels and measured sample rates

What the DB holds right now [M1]:

| physio_id | first | last | rows/s in a 10-min window |
|---|---|---|---|
| NOM_ECG_ELEC_POTL_II | 2026-10-25 00:54Z | 2026-10-28 12:59Z | 748.80 (includes copy rows, see below) |
| NOM_ECG_ELEC_POTL_I | 2026-10-27 05:45Z | 2026-10-27 13:56Z | (session 14 only) |
| NOM_PLETH | 2026-10-25 03:06Z | 2026-10-28 12:59Z | 124.80 |
| NOM_RESP | 2026-10-25 00:54Z | 2026-10-28 12:59Z | 62.40 |
| NOM_EEG_ELEC_POTL_CRTX | not present | | |

Real sample rates, by monitor frame, from the session packages [M2]. One MP50
frame lasts about 1.0245 s. In packages written before WOR-976 every ECG frame
holds 768 rows in 4 blocks of 192, and rows 0..63 of each block are a 250 Hz copy;
dropping them leaves 512 real samples per frame. Ingest drops the copies since
WOR-976, so new packages hold the 512 directly.

| Session | Channel | Frames | Samples/frame | Rate |
|---|---|---|---|---|
| 12 | ECG lead II | 29,823 | 512 | 499.67 Hz |
| 12 | Pleth | 29,824 | 128 | 124.92 Hz |
| 12 | RESP (impedance) | 29,824 | 64 | 62.46 Hz |
| 14 | ECG lead I | 28,588 | 512 | 496.77 Hz |
| 14 | Pleth | 28,684 | 128 | 124.19 Hz |
| 15 | ECG lead II | 25,411 | 512 | 499.73 Hz |
| 15 | Pleth | 25,412 | 128 | 124.94 Hz |
| 15 | RESP | 25,412 | 64 | 62.47 Hz |
| 5 | ECG lead II | 377 | 512 | 499.77 Hz |
| 5 | EEG (CRTX) | 377 | 128 | 124.94 Hz |

Session 14's span-per-frame is 0.6 % longer than the others (1.0307 s), which
is what missing frames inside the span look like. Analysis on it is windowed, so
the drift stays inside a window. The exported ECG lead switched to lead I that
night (88 frames of lead II).

Numerics present on every night [M2], each at about 1 Hz:
`NOM_ECG_CARD_BEAT_RATE`, `NOM_PLETH_PULS_RATE`, `NOM_PULS_OXIM_SAT_O2`
(0.1 % resolution), `NOM_PULS_OXIM_PERF_REL` (perfusion index, PI),
`NOM_RESP_RATE`, `NOM_ECG_V_P_C_CNT` (PVC count per minute).

### Does raw EEG survive? No usable EEG exists.

- The DB has no EEG rows [M1]. The 1-minute aggregate has EEG for 2026-06-13
  only (1,009 minutes) [M1]: at 01..02Z the max hits 4095, and from 03:00Z every
  minute spans exactly -62.5..62.5, which is the channel's rail-to-rail range.
- Session 5 (June, 6 minutes) holds raw EEG [M3]: 48,256 rows at 125.27 rows/s,
  127 distinct values in 1.01 uV steps over -62.5..62.5, 3.2 % of samples on the
  rails, and the monitor's own `NOM_EEG_BIS_SIG_QUAL_INDEX` is 0 in all 377
  values. A signal-quality index of zero means the monitor saw no valid EEG
  (no electrodes, or electrodes off).

**Verdict:** every EEG idea below needs a new recording with the EEG module and
electrodes applied, accepted only when BIS SQI stays above zero.
See [EEG: what a new recording needs](#eeg-what-a-new-recording-needs).

### Timing caveat

Row stamps are MQTT arrival times, not sample times. Samples are placed by frame
order (frame k, sample j of n: t = t0 + (k + j/n) T), and channels are paired
burst by burst (nearest stamp within 50 ms). Cross-channel timing (ECG to pleth)
is therefore exact within a frame and relative, not absolute: any fixed device
delay is unknown. This matters for pulse arrival time only.

### What is missing

- No accelerometer or actigraphy on the monitor: movement can only be inferred
  from ECG and pleth artifacts. An Apple Watch (Series 10) is worn at night and
  records movement and its own sleep-stage estimate. That estimate is not PSG,
  but it is the one independent reference for sleep timing available (row 10).
- No airflow, no effort belts, no EEG/EOG/EMG on the recorded nights: nothing that
  sleep medicine scores events or stages from. Snoring is recorded separately by
  a phone app (Sleep Cycle), which gives sound events, not airflow.
- NBP is available on the monitor but was not cycling at night, so pulse arrival
  time (row 9) cannot be calibrated. A cuff reading at bedtime and one on waking
  would anchor the trend at both ends without inflating the cuff during sleep.
- SpO2 averaging time on the monitor is unknown. It changes desaturation counts
  materially [Zafar 2005].
- Monitor modules that would close some of these gaps are listed in
  [Hardware that would close the gaps](#hardware-that-would-close-the-gaps).

## Ranked proposals

Ranked by what the user gains times how well our data supports it, minus effort.
Effort: **S** about a day, **M** two to five days, **L** more than a week or needs
new data to validate. Where: **Live** dashboard, overnight **Report**, **EDF**
export (annotations or derived channels).

Revised on 2026-10-31 after validation on public reference databases: the
irregular-rhythm flag [M10] and the QT trend [M11] moved up from 8 and 9 to 5
and 6, so pulse rate variability, breathing rate and pulse arrival time move
down to 7, 8 and 9. Cardiopulmonary coupling was tested [M12] and stays at 10.
The Top 3 is unchanged.

| # | What it tells the user | Method + citation | Signals, minimum rate | Our data supports it? | Effort | Where |
|---|---|---|---|---|---|---|
| 0 | Prerequisite: which minutes of each trace are trustworthy | ECG SQI from beat-detector agreement and template match [Li 2008], [Orphanidou 2015], [Zhao 2018], [Behar 2013]; PPG SQI [Elgendi 2016]; PI gate on SpO2 [Lima 2002] | ECG 250 Hz, pleth 125 Hz, PI numeric | **Yes.** Zhao 2018 rates 94/101 and 82/86 ten-minute windows "Excellent" [M4]. The PI gate catches a probe artifact in session 14 [M7] | S | Live (quality badge), Report (masking), EDF (annotation) |
| 1 | Overnight heart: hourly heart rate and HRV (RMSSD, SDNN, LF/HF), PVC-free | R-peaks, then Task Force HRV metrics [Task Force 1996], [Shaffer 2017]; norms [Nunan 2010]; rate effects [Kwon 2018], [Ellis 2015]; tools [NeuroKit2], [HeartPy], [wfdb XQRS], [Vest 2018] | ECG, 250 to 500 Hz recommended [Task Force 1996]; ours 500 Hz | **Yes.** R-peak F1 NeuroKit2 vs XQRS median 0.998 / 1.000 (S12 / S15), HR vs monitor median abs. difference 0.34 / 0.32 bpm, RMSSD NeuroKit2 vs HeartPy median 2.6 / 2.3 ms [M4] [M5] | M | Report, EDF (R-peak annotations) |
| 2 | Overnight SpO2: oxygen desaturation index (3 % and 4 %), time below 90 %, desaturation area, nadir, with artifact seconds removed | AASM desaturation rule [Berry 2012]; hypoxic burden [Azarbarzin 2019]; oximetry reviews [Netzer 2001], [Chiner 1999], [Hang 2015]; reference toolbox [pobm, Levy 2021] | SpO2 numeric 1 Hz, PI numeric | **Partly.** Own detector and pobm both run; event counts differ by a factor 2 to 4 and event-level match within 30 s is 0 to 50 %, yet both put each night in the same severity band [M7]. Detector choice must be fixed and stated | M | Report, Live (PI quality) |
| 3 | Restless or disturbed stretches of the night (a timeline, not sleep stages) | Motion share from ECG artifact; pleth vs ECG rate mismatch; HR surge plus perfusion drop as an autonomic arousal marker, the PAT principle [Herscovici 2007], [Bar 2003] | ECG 250 Hz, PR/HR/PI numerics 1 Hz | **Yes, as a proxy.** HR surges come with a PI drop of 30 % or more in 88 / 88 / 89 % of cases vs 38 / 33 / 43 % at random times (S12 / S14 / S15), a 2.1 to 2.7 times enrichment [M9]; motion share 0.1 to 0.4 % of frames, rate mismatch 3.5 to 4.7 % of seconds [M8] | S | Report (timeline), EDF (annotations) |
| 4 | Extra beats: PVC burden per hour and per night | Burden = PVCs / all beats [Baman 2010]; context in healthy adults [Kennedy 1985], [Hingorani 2016], [Niwano 2009], [ESC 2022] | `NOM_ECG_V_P_C_CNT` (per-minute count); beat-level check needs ECG 250 Hz | **Partly.** The monitor's PVC counter is present on every night (29,522 values in S12) [M2]. No independent beat classifier was run, so the counter is unverified | S (counter), M (verified) | Report |
| 5 | Irregular rhythm flag (atrial fibrillation pattern) | RR irregularity tests [Tateno 2001], [Dash 2009], [Lake 2011], [Sarkar 2008]; reference data [afdb, Moody 1983], [Goldberger 2000] | R-peaks from ECG 250 Hz | **Yes for [Dash 2009], as a flag for review, not for COSEn.** On the public AF database Dash 2009 reaches Se 0.89 / Sp 0.91 and COSEn Se 0.93 / Sp 0.95 (25 records, leave-one-record-out thresholds); our own R-peak chain moves Se by 0.05 at most. On our nights (23.9 h, no AF assumed) Dash flags 0.2 windows per hour, every one where the two beat detectors disagree; COSEn flags 3.4 to 4.3 per hour, mostly with no PVC and no outlier beat, so it needs an ECG review before it is used. AF rhythm spliced into a night is caught 43 / 43 by both [M10] | M | Report (flag, with the ECG strip) |
| 6 | QT interval trend (QTc, Fridericia) | Wavelet delineation [Martinez 2004]; tangent T end [Postema 2008]; correction formulas [Luo 2004], [Vandenberk 2016]; measurement standards [Rautaharju 2009]; reference data [QTDB, Laguna 1997] | ECG 250 Hz or more, one lead | **Yes, as a trend with a two-method gate.** On the public QT database neither delineator alone meets the 30.6 ms T-end tolerance; kept only where NeuroKit2 dwt and an own tangent method agree within 30 ms (56 % of beats), the T-end error is -13.6 ± 23.5 ms and passes. On our nights the two agree to a median 3 to 10 ms, 250 Hz matches 500 Hz, and the night recorded on lead I is the weak one. Fridericia fits our QT-RR relation, Bazett over-corrects [M11] | M | Report |
| 7 | HRV when the ECG is off: pulse rate variability from the pleth | Pleth beat detection [Charlton 2022]; PRV vs HRV [Schaefer 2013], [Mejia-Mejia 2020]; minimum PPG rate [Choi 2017] | Pleth 125 Hz | **Yes, with a known bias.** PRV/HRV ratio: SDNN 1.02 / 1.03, RMSSD 1.13 / 1.17, Spearman 0.97 / 0.95 (S12 / S15) [M5] | S | Report (fallback, labelled) |
| 8 | Breathing rate overnight, and when the monitor's value is doubtful | ECG-derived respiration (EDR) and impedance RESP spectral peak [Charlton 2016], [Charlton 2018], [Karlen 2013] | ECG 250 Hz, RESP 62.5 Hz | **Weak.** EDR within 2 breaths/min of `NOM_RESP_RATE` in 76 / 48 % of windows, RESP waveform 80 / 45 % (S12 / S15) [M4]. No airflow reference, so it is unknown which side is wrong | M | Report (quality flag) |
| 9 | Overnight vascular trend: pulse arrival time (ECG R-peak to pleth upstroke) | PAT/PTT theory and limits [Mukkamala 2015], [Payne 2006], [Gesche 2012]; foot markers [Chiu 1991], [Gao 2016] | ECG 500 Hz, pleth 125 Hz on one frame axis | **As an uncalibrated trend only.** Per-beat noise 9 to 10 ms; two independent upstroke markers differ by a stable 44.0 ms (sd 5.0) / 46.5 ms (sd 4.3); Spearman(PAT, HR) -0.28 / 0.05 [M6]. No overnight NIBP to calibrate, pre-ejection period unknown | M | Report (trend), EDF (derived channel) |
| 10 | Sleep stability from heart and breathing coupling (cardiopulmonary coupling) | CPC spectrogram [Thomas 2005], [Thomas 2007], [Thomas 2014]; class thresholds [Thomas patent US 7,734,334]; reference data [slpdb, Ichimaru 1999], [Goldberger 2000] | R-peaks + EDR from ECG 250 Hz | **Partly, as a nightly share, not per window.** On the public sleep database the share of high-frequency coupling is higher in N2+N3 than in REM for 13 / 13 subjects and than in wake for 12 / 13, and stage labels shuffled in time do not reproduce the gap; a single window separates N2+N3 from wake and REM only weakly (AUC 0.61). It works from EDR, not from the RESP channel. On our nights EDR- and RESP-based labels agree at kappa 0.18 to 0.53. The classes describe spectral shape, not proven coupling: noise also lands in the stable class, so the row 0 quality mask comes first [M12] | L | Report (nightly share, marked experimental) |
| 11 | Sleep stages from HRV | Autonomic staging [Radha 2019], [Fonseca 2020], [Sridhar 2020], [Walch 2019], [Hedner 2011] | R-peaks; published systems also use an accelerometer | **Not recommended.** Published agreement with PSG is moderate (kappa roughly 0.6 to 0.7) and leans on motion data we lack; no PSG to validate | L | none |
| 12 | Sleep apnea screening | ECG-based [Penzel 2002], [Varon 2015]; oximetry-based [Behar 2019], [Behar 2023]; guideline [Kapur 2017] | ECG, SpO2 | **Out of scope.** This is a diagnostic claim; vscc must not make it. Row 2's numbers stay descriptive | n/a | none |
| 13 | Sleep stages, spindles and slow waves from EEG | Automatic staging [YASA, Vallat 2021], [DeepSleepNet, Supratak 2017]; spindles [Warby 2014]; single-channel home EEG [Finan 2016] | EEG 100 Hz or more, valid electrodes | **No.** No usable EEG exists [M3]. Needs a new recording | L | Report, EDF |

## Top 3

Each item is a scratch-to-product path with its own verification. Differential
checks run two independent implementations on the same data and compare.

### 1. Overnight heart report (rows 0 + 1, plus the row 4 counter)

**What the user gets:** one page per night: hourly heart rate, RMSSD and SDNN,
the PVC count per hour from the monitor, and the share of each hour that passed
the ECG quality gate. LF/HF only with a caveat (it is contested as a "balance"
measure [Shaffer 2017]).

**First prototype step:** a pure function `ecg_hourly(frames, period) -> rows`
in a new module, fed from a session package, using NeuroKit2 for R-peaks and the
Task Force metrics on 5-minute windows that pass the Zhao 2018 SQI. Output goes
to a JSON next to the package; no UI yet.

**Verification:**
- Differential R-peaks: XQRS (wfdb) vs NeuroKit2 on every window, F1 at 50 ms
  tolerance at least 0.99, else the window is dropped and counted. On S12, 85 %
  of windows already clear 0.99 (median 0.998) [M5].
- Differential HR: computed HR vs the monitor's `NOM_ECG_CARD_BEAT_RATE`, median
  absolute difference under 1 bpm (measured 0.34 / 0.32) [M4].
- Differential HRV: RMSSD from NeuroKit2 vs HeartPy (median difference 2.6 / 2.3 ms
  today [M4]) and, once added, vs the PhysioNet Cardiovascular Signal Toolbox
  [Vest 2018] on one exported night.
- Known-answer test: `neurokit2.ecg_simulate` at a set heart rate and HRV
  through the same pipeline in `tests/`, pure unit test, no I/O.

### 2. Overnight SpO2 summary with artifact gating (rows 0 + 2)

**What the user gets:** ODI3 and ODI4 per hour and per night, time below 90 %,
desaturation area, nadir, each shown with and without artifact seconds, and a
plain note that oximeter averaging changes these numbers [Zafar 2005].

**First prototype step:** fix the detector to the AASM-style rule [Berry 2012]
(drop of 3 % or 4 % from the preceding 120 s maximum, held 10 s), add the PI gate
(exclude seconds where PI collapses relative to its own running median, or where
pleth pulse rate departs from ECG heart rate), and emit per-hour rows.

**Verification:**
- Differential: own detector vs pobm `DesaturationsMeasures` on the same gated
  series. Today they disagree on counts by a factor 2 to 4 [M7], so the first
  job is to explain that gap (pobm's relative threshold and its handling of
  0.1 % steps) before either number ships. Agreement target: same per-hour count
  within 1 event for 90 % of hours.
- Synthetic injection: add known dips (depth 2, 3, 4, 6 %, durations 5, 10, 30 s)
  to a flat real segment; the detector must find exactly the ones that meet the
  rule. This is the only ground truth we can create without a reference oximeter.
- Artifact control: session 14's sub-90 % seconds come with PI collapsing by two
  orders of magnitude while pleth rate still equals ECG rate [M7]. The gate must
  remove them, and must remove 0 % of seconds on a clean night (S12 and S15:
  0 % removed today).

### 3. Restless-night timeline (rows 0 + 3)

**What the user gets:** a 5-minute strip across the night showing motion
(ECG artifact share), pleth/ECG rate mismatch and "HR surge with vasoconstriction"
markers, plus the same markers as EDF annotations. Explicitly not sleep staging.

**First prototype step:** promote `feas_sleep.py` and `feas_arousal.py` [M8] [M9]
into one pure function over numerics plus ECG frame ranges, emitting events
(onset, kind, strength) and the 5-minute strip.

**Verification:**
- Random-time control in every run: the surge-plus-PI-drop coincidence must stay
  well above the same test at random times (today 2.1 to 2.7 times [M9]). If a
  night falls to near 1, its markers are noise and the report says so.
- Internal consistency: motion share and rate mismatch should co-vary across
  5-minute bins (Spearman 0.57 / 0.46 / 0.21 on S12 / S14 / S15 today [M8]).
- External check, the only real one: one or two nights with an independent
  wearable (accelerometer-based sleep tracker) or a written wake diary, compared
  bin by bin. Until that exists the timeline is a proxy and is labelled so.

## The sleep question

Our data cannot stage sleep. Staging needs EEG (absent), or at least HRV plus
motion validated against PSG (no motion sensor, no PSG). What it can show
honestly is a restlessness and arousal proxy (Top 3, item 3) and how heart rate,
HRV and SpO2 move through the night (items 1 and 2). A real hypnogram needs the
EEG recording below. Cardiopulmonary coupling (row 10) does not change this:
on a public sleep database it separates deep sleep from REM and wake over a
whole night, not window by window [M12].

## EEG: what a new recording needs

- An EEG source connected with a sensor applied. The June recording carries
  `NOM_EEG_BIS_SIG_QUAL_INDEX`, a BIS numeric, which points to a BIS module in a
  slot that night with no sensor on; a BIS forehead sensor would give one frontal
  channel. The 2-channel EEG module is the alternative (see
  [Hardware](#hardware-that-would-close-the-gaps)). A frontal montage is enough
  for single-channel staging [Finan 2016], [YASA, Vallat 2021].
- Accept the recording only if `NOM_EEG_BIS_SIG_QUAL_INDEX` stays above 0 and
  the rail share (samples at +/-62.5) stays near 0 %. Today: SQI 0 throughout and
  3.2 % on the rails [M3].
- 125 Hz is enough for staging and spindles (spindles are 11 to 16 Hz). The
  +/-62.5 uV range is tight for slow waves (often above 75 uV peak to peak), so
  check clipping in the first deep-sleep stretch.
- Then YASA's pretrained staging on the EEG channel is the first prototype, and
  its hypnogram becomes the reference that rows 3 and 11 can be validated against.

## Hardware that would close the gaps

The MP40/MP50 takes plug-in modules in its slots, plus extensions on the
measurement server [Philips MP40/50 IFU]. Ranked by what each adds to the rows
above. Part numbers come from manual copies hosted by third parties: check each
against the Instructions for Use for the monitor's software revision before
buying, and expect VSCapture to need the new waves added to its request list.

| Module | What it adds | Rows it helps | Caveats |
|---|---|---|---|
| Microstream CO2 extension (M3015A/B) on the measurement server, with an oral-nasal sampling cannula [Philips HCM4686A] | CO2 waveform, end-tidal CO2 and an airway respiration rate: the airflow-like channel the kit lacks | 8 (shows which breathing rate is wrong), 10 (a real breathing input), 3 (pauses in breathing, described, not scored) | Mounts on the M3001A measurement server; single-use sampling lines |
| BIS sensor strips for the BIS module, or the 2-channel EEG module M1027A with cup electrodes | Frontal EEG (BIS) or a chosen montage, for example one central EEG and one eye channel (M1027A) | 13 directly; its hypnogram then validates 3, 10 and 11 | The +/-62.5 uV range may clip slow waves (see above) |
| Temperature module M1029A with two skin probes | Distal-to-proximal skin temperature gradient, which rises before sleep onset [Krauchi 1999] | Sleep-onset timing next to 3 | The watch already records wrist temperature, so the gain is small |

Not worth buying for this use: transcutaneous gas (M1018A, a heated sensor that
needs calibration gas), invasive pressure, cardiac output, a second SpO2. The
SvO2 module (M1021A) does not work in the MP40/MP50 at all.

## Measurements

All read-only. DB queries ran on .212 against the production TimescaleDB; the
analysis scripts ran in a scratch Python 3.12 venv (pandas, numpy, scipy,
neurokit2 0.2.12, heartpy, wfdb, pyedflib, pobm 1.2.0) on copies of session
packages. Scripts and their full outputs are in the private WOR-987 notes folder,
not in this repo.

**M1, DB inventory and retention** (re-run 2026-10-31):

```bash
ssh chris@192.168.1.212 'docker exec vscc-timescaledb psql -U postgres -d telemetry -At \
  -c "select physio_id, min(time), max(time), count(*) from patient_waveforms group by 1 order by 1" \
  -c "select physio_id, round(count(*)/600.0,2) from patient_waveforms where time >= '"'"'2026-10-28 08:00Z'"'"' and time < '"'"'2026-10-28 08:10Z'"'"' group by 1 order by 1" \
  -c "select count(*), min(bucket), max(bucket) from patient_waveforms_1min where physio_id = '"'"'NOM_EEG_ELEC_POTL_CRTX'"'"'" \
  -c "select hypertable_name, config->>'"'"'drop_after'"'"', next_start from timescaledb_information.jobs where proc_name = '"'"'policy_retention'"'"'"'
```

Result: four waveform physio_ids, no EEG; 748.80 / 124.80 / 62.40 rows/s for
ECG II / pleth / RESP; EEG aggregate 1,009 minutes, all on 2026-06-13; both
hypertables `72:00:00`, next run 2026-11-01 05:21Z.

**M2, frame rates per package:** `python load.py 5 12 14 15`. Splits each channel
into bursts (stamp step over 0.3 s), cuts whole frames (768 ECG rows, 128 pleth,
64 RESP, 128 EEG), drops ECG copy rows, rate = samples per frame / (span / frames).
Numerics ids and counts from the same cache:
`python -c 'import numpy as np; z=np.load("cache/12.npz", allow_pickle=True); print(dict(zip(*np.unique(z["num_id"], return_counts=True))))'`
(S12: 29,324 to 29,522 values per id; S14 and S15 carry the same six ids).

**M3, EEG premise:** `python eeg_check.py 5`. Output:
`EEG rows 48,256 rows/s 125.27 distinct 127 step 1.01 range -62.5..62.5 at rails 3.2 %`,
`NOM_EEG_BIS_SIG_QUAL_INDEX count 377 min 0.0 max 0.0`.

**M4, ECG feasibility:** `python feas_ecg.py 12` and `15`, 10-minute windows
(101 on S12, 86 on S15). NeuroKit2 (neurokit method) vs HeartPy R-peaks; HR vs
the monitor numeric; RMSSD both ways; Zhao 2018 quality class; EDR and RESP
Welch peak vs `NOM_RESP_RATE`.

| | S12 | S15 |
|---|---|---|
| F1 NeuroKit2 vs HeartPy, median (p10) | 0.964 (0.573) | 0.976 (0.914) |
| Zhao 2018 "Excellent" windows | 94 / 101 | 82 / 86 |
| abs(HR - monitor), median (p90) | 0.34 (0.79) bpm | 0.32 (0.76) bpm |
| abs(RMSSD NeuroKit2 - HeartPy), median | 2.6 ms | 2.3 ms |
| EDR within 2 br/min of monitor | 76 % | 48 % |
| RESP waveform within 2 br/min | 80 % | 45 % |

HeartPy is the weaker detector here (low p10 on S12), which is why XQRS became
the second reference.

**M5, second R-peak reference and PRV:** `python feas_qrs2.py 12` and `15`
(26 and 22 windows). XQRS vs NeuroKit2 F1 at 50 ms: S12 median 0.9984, p10 0.9874,
85 % of windows at 0.99 or more; S15 median 1.0000, p10 0.9984. PRV (pleth beats)
vs HRV on the same beats: RMSSD ratio 1.13 / 1.17, SDNN ratio 1.02 / 1.03,
Spearman 0.97 / 0.95.

**M6, pulse arrival time:** `python feas_pat.py 12` and `15`. Burst-wise frame
map between ECG and pleth; per beat, two pleth markers (max first derivative with
parabolic sub-sample, and intersecting-tangent foot). Per-beat noise 9 to 10 ms;
marker difference 44.0 ms (sd 5.0) / 46.5 ms (sd 4.3); ensemble lag about 585 ms
(includes unknown device delays); Spearman(PAT, HR) -0.28 / 0.05.

**M7, desaturations:** `python feas_odi.py 12 15 14` and
`python feas_odi.py --pi-gate 12 15 14`. Own detector (3 % below the preceding
120 s max, held 10 s) vs pobm 1.2.0 `DesaturationsMeasures` on the SpO2 numeric
rounded to integers (pobm needs integer input; ours has 0.1 % steps). Counts
differ by a factor 2 to 4, event match within 30 s is 0 to 50 %, both detectors
give the same severity band on every night. The PI gate removes 0 % of seconds
on S12 and S15 and 2.33 % on S14, where every sub-90 % reading coincides with PI
collapsing by two orders of magnitude while pleth rate equals ECG rate (a probe
artifact, not a desaturation).

**M8, restlessness proxy:** `python feas_sleep.py 12 14 15`. Per night: motion
share (ECG frames whose p1..p99 range exceeds twice the 5-minute rolling median)
0.4 / 0.4 / 0.1 %; pleth vs ECG rate mismatch over 5 bpm 4.7 / 4.0 / 3.5 % of
seconds; 5-minute Spearman between the two 0.57 / 0.46 / 0.21.

**M9, arousal coincidence:** `python feas_arousal.py 12 14 15`. HR surge onset =
1 Hz HR rises 10 bpm or more over the 2-minute median within 15 s (60 or more
surges per night). PI drop = PI falls to 70 % or less of its 60 s median within
30 s after. Control: same test at 2,000 random times at least 120 s from any
onset (seed 987).

| | Surges with PI drop | Random times with PI drop | Ratio |
|---|---|---|---|
| S12 | 88 % | 38 % | 2.3 |
| S14 | 88 % | 33 % | 2.7 |
| S15 | 89 % | 43 % | 2.1 |

A PI drop alone is common (a third of random times), so it is a weak marker on
its own. The combination with an HR surge is the signal.

**M10, irregular-rhythm flag:** `python row8_afdb_ann.py`, `row8_afdb_chain.py`,
`row8_nights.py`, `row8_nights_diag.py`. Reference data: PhysioNet afdb 1.0.0
[afdb, Moody 1983], all 86 files SHA256-checked. Dash 2009 (AND rule over
RMSSD / mean RR, Shannon entropy and turning point ratio) and COSEn [Lake 2011],
both re-implemented from the papers, on 64-RR windows (about 1 min) stepped by 32.
The published thresholds are not in the abstracts, so they were fitted on afdb
with leave-one-record-out; the pooled set applied to our nights is RMSSD / mean
over 0.13, entropy over 0.675, turning point ratio 0.46 to 0.95, COSEn over -1.559.

| | Dash 2009 | COSEn |
|---|---|---|
| afdb, annotated beats, 25 records, 38,023 windows: Se / Sp / PPV | 0.894 / 0.913 / 0.888 | 0.928 / 0.952 / 0.938 |
| afdb full chain, 5 records, Se (annotated beats / NeuroKit2 / XQRS) | 0.965 / 0.919 / 0.963 | 0.944 / 0.939 / 0.945 |
| same 5 records, Sp | 0.775 / 0.777 / 0.757 | 0.908 / 0.908 / 0.904 |
| our nights S12, S14, S15 (23.9 h), flagged windows per hour | 0.2 | 3.4 to 4.3 |
| flagged windows with a PVC within 30 s | 2 of 4 | 2.5 to 4.9 % |

Kappa between the two detectors is 0.81 on afdb and 0.71 to 0.75 on the full
chain; the five chain records are ectopy-rich (one has Sp 0.40), so the chain Sp
is a lower bound. On our nights the four Dash flags all sit in windows where
NeuroKit2 and XQRS disagree on the beat count, so they are peak-detection
artifacts; requiring both detectors to agree removes them. Only about 3 % of all
windows contain a PVC, so PVCs explain almost none of the COSEn flags, and 88 % of
the COSEn flags without a PVC have no outlier RR (diffuse irregularity, cause not
resolved). COSEn flags cluster in time rather than spreading over the night, and
the NeuroKit2 and XQRS versions agree only at kappa 0.33 on a 30 s grid; Dash and
COSEn agree at kappa about 0. Controls in the same run: AF RR intervals from
afdb spliced into night 15 through the nights code path are flagged 43 / 43 by
both detectors; HR from our R-peaks vs the monitor r 0.94, median absolute
difference 0.47 bpm. Caveats: the nights have no rhythm reference, so "false
alarm" assumes no AF; afdb beat files carry no ectopy labels; the PVC numeric is
a rolling 60 s count, so overlap was tested three ways (raw, event, event ± 30 s).

**M11, QT delineation:** `bash row9_run_all.sh` (QT database, then our nights,
about 9 minutes). Reference data: PhysioNet QTDB [QTDB, Laguna 1997], 326 files
SHA256-checked; manual q1c annotations, channel 0, 3,542 beats in 103 records (two
records have no T annotation). Error = automatic minus manual, in ms.

| Delineator | QRS onset: n, mean ± SD | T end: n, mean ± SD | QT: n, mean ± SD |
|---|---|---|---|
| NeuroKit2 dwt [Martinez 2004] | 3,226, -63.5 ± 54.7 | 2,931, -44.5 ± 92.0 | 2,739, +23 ± 105 |
| NeuroKit2 cwt | 1,273, -32.0 ± 26.3 | 2,395, +42.8 ± 113.7 | 897, +92 ± 132 |
| Own (slope onset, tangent T end [Postema 2008]) | 3,447, +5.2 ± 13.5 | 3,336, -14.6 ± 78.7 | 3,336, -19.8 ± 79.5 |
| Consensus (own onset, T end where dwt and own agree within 30 ms) | | 56 % of beats, -13.6 ± 23.5 | -18.7 ± 25.9 |

Tolerance: SD of the T-end error at most 30.6 ms, the value [Martinez 2004] uses.
Every single delineator fails it on raw SD (gross failures on noisy QTDB records;
robust SD 24 to 26 ms); the consensus passes, with 84 % of its beats within
± 30.6 ms. It would fail a stricter SD of 15.3 ms. Our nights (283 five-minute
windows on S12, S14, S15, at 500 Hz and decimated to 250 Hz): dwt delineates
98 % or more of beats, cwt 15 to 17 % on two nights and none at 250 Hz (it
depends on the sampling rate); dwt vs own T end median absolute difference
3 / 10 / 8 ms, window Spearman 0.90 / 0.31 / 0.93; the consensus gate keeps
99 / 68 / 99 % of beats (S14 is lead I). The dwt QRS onset sits about 112 ms
early on S12 and S15, so QT uses the own onset. 500 vs 250 Hz: T end median
absolute difference 1 to 2 ms, Spearman 0.98 to 1.00. Correction choice: a pooled
log-log fit of QT on RR gives an exponent whose 95 % CI contains Fridericia's 1/3
and excludes Bazett's 1/2; the residual slope of QTc on RR is +19 ms/s
[-13, +49] for Fridericia and -49 ms/s [-81, -18] for Bazett. Caveats: the
monitor's ECG filter mode is unknown and can reshape the T wave; RR varies only
4 to 5 % across windows, so the exponent CI is wide; QTDB is clinical, noisy and of
mixed rhythm.

**M12, cardiopulmonary coupling:** `python row10_test_cpc.py` (synthetic tests),
`row10_night.py beats|cpc <night>`, `agree 12 15`, `agree 14`, `respcheck`;
reference data `bash row10_dl_slpdb.sh`, `row10_slpdb.py rec <record>` per
record, then `row10_slpdb.py summary 2000`. Reference data: PhysioNet slpdb 1.0.0
[slpdb, Ichimaru 1999], 18 records with sleep stages scored every 30 s. Method
after [Thomas 2005]: the normal-to-normal RR series and EDR from R-wave amplitude,
both resampled at 2 Hz; 1,024-sample windows (8.5 min) stepped by 256 (about
2 min); coherence times cross-spectral power; bands VLF 0.004 to 0.01, LF 0.01 to
0.1, HF 0.1 to 0.4 Hz. The paper gives no class thresholds, so they come from the
patent [Thomas patent US 7,734,334], applied in order: high-frequency coupling
(HFC, the stable-sleep class) if HF at least 0.02 and LF/HF at most 1.5; else LFC
if LF at least 0.2 and LF/HF at least 2; else VLFC if VLF at least 0.05 and
VLF/(LF+HF) at least 0.2; else other. The patent states the VLF ratio both as a
minimum and as a maximum (the minimum was used), and the spectrum normalisation is
not published (assumed: divide by the total over 0.004 to 0.4 Hz). No open
reference implementation was found. The coherence code matches scipy to 1e-14 on
synthetic signals.

| Check | Result |
|---|---|
| slpdb beats (NeuroKit2) vs the database annotations | F1 0.992 to 1.000 |
| slpdb, 2,346 windows, EDR input: HFC share by stage | N2+N3 0.47, wake 0.27, REM 0.15, N1 0.10 (annotated beats: 0.48 / 0.29 / 0.21 for N2+N3 / wake / REM) |
| per subject, 13 records with 5 or more windows per group | N2+N3 above REM in 13 / 13 (Wilcoxon p 0.0002), above wake in 12 / 13 (p 0.0024) |
| per window, log(HF/LF), N2+N3 vs wake and REM | AUC 0.61 |
| controls: stage labels shuffled in time, 2,000 draws each | N2+N3 minus wake/REM gap +0.05 under epoch permutation and +0.03 under a circular shift of at least 30 min, vs +0.23 observed (p 0.0005, the floor for 2,000 draws); AUC null 0.53 |
| slpdb with the RESP channel instead of EDR | HFC share 0.56 to 0.67 in every stage, gap -0.003: no stage contrast |
| our nights, EDR vs RESP labels, 4 classes (S12 / S14 / S15) | agreement 44 / 72 / 53 %, kappa 0.18 / 0.53 / 0.29; window Spearman of log(HF/LF) 0.78 / 0.81 / 0.73; hourly HFC-share Spearman 0.55 / 0.87 / 0.79 |
| our nights, RESP waveform rate within 2 breaths/min of the monitor numeric | 73 / 61 / 40 % of windows |

So the coupling measure carries stage information over a night (more
high-frequency coupling in N2+N3), which supports a nightly share, but a single
window says little, so it gives no hypnogram. It needs EDR: with the RESP channel
the stable class saturates. On our nights the two inputs agree only moderately,
best on S14, and the RESP waveform is weakest on S15. Caveats: the classes are
spectral shapes under the assumed normalisation, so a pure-noise window is
labelled HFC and two independent signals still show a mean coherence of about
0.33 with three sub-windows; slpdb subjects are sleep apnea patients, not healthy
sleepers; our nights have no PSG. A comparison with the Apple Watch sleep stages
is pending the Health export, and that is a consumer estimate, not PSG.

## References

Every link was checked to resolve (doi.org handle API, with a fake-DOI control
that must fail) and its Crossref title matched to the cited paper. The afdb dataset DOI is
registered with DataCite, so its title was matched there; the patent has no
DOI, so its Google Patents page was checked for the title.

- [Azarbarzin 2019] The hypoxic burden of sleep apnoea predicts cardiovascular disease-related mortality. Eur Heart J. https://doi.org/10.1093/eurheartj/ehy624
- [Baman 2010] Relationship between burden of premature ventricular complexes and left ventricular function. Heart Rhythm. https://doi.org/10.1016/j.hrthm.2010.03.036
- [Bar 2003] Evaluation of a portable device based on peripheral arterial tone for unattended home sleep studies. Chest. https://doi.org/10.1378/chest.123.3.695
- [Behar 2013] ECG signal quality during arrhythmia and its application to false alarm reduction. IEEE TBME. https://doi.org/10.1109/TBME.2013.2240452
- [Behar 2019] Feasibility of single channel oximetry for mass screening of obstructive sleep apnea. EClinicalMedicine. https://doi.org/10.1016/j.eclinm.2019.05.015
- [Behar 2023] Deep learning for obstructive sleep apnea diagnosis based on single channel oximetry. Nat Commun. https://doi.org/10.1038/s41467-023-40604-3
- [Berry 2012] Rules for scoring respiratory events in sleep: update of the 2007 AASM Manual. J Clin Sleep Med. https://doi.org/10.5664/jcsm.2172
- [Charlton 2016] An assessment of algorithms to estimate respiratory rate from the electrocardiogram and photoplethysmogram. Physiol Meas. https://doi.org/10.1088/0967-3334/37/4/610
- [Charlton 2018] Breathing rate estimation from the electrocardiogram and photoplethysmogram: a review. IEEE Rev Biomed Eng. https://doi.org/10.1109/RBME.2017.2763681
- [Charlton 2022] Detecting beats in the photoplethysmogram: benchmarking open-source algorithms. Physiol Meas. https://doi.org/10.1088/1361-6579/ac826d
- [Chiner 1999] Nocturnal oximetry for the diagnosis of the sleep apnoea hypopnoea syndrome. Thorax. https://doi.org/10.1136/thx.54.11.968
- [Chiu 1991] Determination of pulse wave velocities with computerized algorithms. Am Heart J. https://doi.org/10.1016/0002-8703(91)90153-9
- [Choi 2017] Photoplethysmography sampling frequency: pilot assessment of how low can we go to analyze pulse rate variability with reliability? Physiol Meas. https://doi.org/10.1088/1361-6579/aa5efa
- [Dash 2009] Automatic real time detection of atrial fibrillation. Ann Biomed Eng. https://doi.org/10.1007/s10439-009-9740-z
- [Elgendi 2016] Optimal signal quality index for photoplethysmogram signals. Bioengineering. https://doi.org/10.3390/bioengineering3040021
- [Ellis 2015] A careful look at ECG sampling frequency and R-peak interpolation on short-term measures of heart rate variability. Physiol Meas. https://doi.org/10.1088/0967-3334/36/9/1827
- [ESC 2022] 2022 ESC Guidelines for the management of patients with ventricular arrhythmias and the prevention of sudden cardiac death. Eur Heart J. https://doi.org/10.1093/eurheartj/ehac262
- [Finan 2016] Validation of a wireless, self-application, ambulatory electroencephalographic sleep monitoring device in healthy volunteers. J Clin Sleep Med. https://doi.org/10.5664/jcsm.6262
- [Fonseca 2020] Automatic sleep staging using heart rate variability, body movements, and recurrent neural networks in a sleep disordered population. Sleep. https://doi.org/10.1093/sleep/zsaa048
- [Gao 2016] Comparison of noninvasive pulse transit time estimates as markers of blood pressure using invasive pulse transit time measurements as a reference. Physiol Rep. https://doi.org/10.14814/phy2.12768
- [Gesche 2012] Continuous blood pressure measurement by using the pulse transit time: comparison to a cuff-based method. Eur J Appl Physiol. https://doi.org/10.1007/s00421-011-1983-3
- [Goldberger 2000] PhysioBank, PhysioToolkit, and PhysioNet: components of a new research resource for complex physiologic signals. Circulation. https://doi.org/10.1161/01.CIR.101.23.e215
- [Hang 2015] Validation of overnight oximetry to diagnose patients with moderate to severe obstructive sleep apnea. BMC Pulm Med. https://doi.org/10.1186/s12890-015-0017-z
- [HeartPy] van Gent et al. HeartPy: a novel heart rate algorithm for the analysis of noisy signals. Transp Res F. https://doi.org/10.1016/j.trf.2019.09.015; and Analysing noisy driver physiology real-time using off-the-shelf sensors: heart rate analysis software from the Taking the Fast Lane project. J Open Res Softw. https://doi.org/10.5334/jors.241
- [Hedner 2011] Sleep staging based on autonomic signals: a multi-center validation study. J Clin Sleep Med. https://doi.org/10.5664/JCSM.1078
- [Herscovici 2007] Detecting REM sleep from the finger: an automatic REM sleep algorithm based on peripheral arterial tone and actigraphy. Physiol Meas. https://doi.org/10.1088/0967-3334/28/2/002
- [Hingorani 2016] Arrhythmias seen in baseline 24-hour Holter ECG recordings in healthy normal volunteers during phase 1 clinical trials. J Clin Pharmacol. https://doi.org/10.1002/jcph.679
- [slpdb, Ichimaru 1999] Development of the polysomnographic database on CD-ROM. Psychiatry Clin Neurosci. https://doi.org/10.1046/j.1440-1819.1999.00527.x
- [Kapur 2017] Clinical practice guideline for diagnostic testing for adult obstructive sleep apnea. J Clin Sleep Med. https://doi.org/10.5664/jcsm.6506
- [Karlen 2013] Multiparameter respiratory rate estimation from the photoplethysmogram. IEEE TBME. https://doi.org/10.1109/TBME.2013.2246160
- [Kennedy 1985] Long-term follow-up of asymptomatic healthy subjects with frequent and complex ventricular ectopy. N Engl J Med. https://doi.org/10.1056/NEJM198501243120401
- [Krauchi 1999] Warm feet promote the rapid onset of sleep. Nature. https://doi.org/10.1038/43366
- [Kwon 2018] Electrocardiogram sampling frequency range acceptable for heart rate variability analysis. Healthc Inform Res. https://doi.org/10.4258/hir.2018.24.3.198
- [QTDB, Laguna 1997] A database for evaluation of algorithms for measurement of QT and other waveform intervals in the ECG. Computers in Cardiology. https://doi.org/10.1109/CIC.1997.648140
- [Lake 2011] Accurate estimation of entropy in very short physiological time series: the problem of atrial fibrillation detection in implanted ventricular devices. Am J Physiol Heart. https://doi.org/10.1152/ajpheart.00561.2010
- [Li 2008] Robust heart rate estimation from multiple asynchronous noisy sources using signal quality indices and a Kalman filter. Physiol Meas. https://doi.org/10.1088/0967-3334/29/1/002
- [Lima 2002] Use of a peripheral perfusion index derived from the pulse oximetry signal as a noninvasive indicator of perfusion. Crit Care Med. https://doi.org/10.1097/00003246-200206000-00006
- [Luo 2004] A comparison of commonly used QT correction formulae: the effect of heart rate on the QTc of normal ECGs. J Electrocardiol. https://doi.org/10.1016/j.jelectrocard.2004.08.030
- [Martinez 2004] A wavelet-based ECG delineator: evaluation on standard databases. IEEE TBME. https://doi.org/10.1109/TBME.2003.821031
- [Mejia-Mejia 2020] Pulse rate variability in cardiovascular health: a review on its applications and relationship with heart rate variability. Physiol Meas. https://doi.org/10.1088/1361-6579/ab998c
- [afdb, Moody 1983] MIT-BIH Atrial Fibrillation Database (Moody and Mark, A new method for detecting atrial fibrillation using R-R intervals, Computers in Cardiology 1983, which has no DOI). PhysioNet. https://doi.org/10.13026/C2MW2D
- [Mukkamala 2015] Toward ubiquitous blood pressure monitoring via pulse transit time: theory and practice. IEEE TBME. https://doi.org/10.1109/TBME.2015.2441951
- [Netzer 2001] Overnight pulse oximetry for sleep-disordered breathing in adults: a review. Chest. https://doi.org/10.1378/chest.120.2.625
- [NeuroKit2] Makowski et al. NeuroKit2: a Python toolbox for neurophysiological signal processing. Behav Res Methods. https://doi.org/10.3758/s13428-020-01516-y; code https://github.com/neuropsychology/NeuroKit
- [Niwano 2009] Prognostic significance of frequent premature ventricular contractions originating from the ventricular outflow tract in patients with normal left ventricular function. Heart. https://doi.org/10.1136/hrt.2008.159558
- [Nunan 2010] A quantitative systematic review of normal values for short-term heart rate variability in healthy adults. Pacing Clin Electrophysiol. https://doi.org/10.1111/j.1540-8159.2010.02841.x
- [Orphanidou 2015] Signal-quality indices for the electrocardiogram and photoplethysmogram: derivation and applications to wireless monitoring. IEEE J Biomed Health Inform. https://doi.org/10.1109/JBHI.2014.2338351
- [Payne 2006] Pulse transit time measured from the ECG: an unreliable marker of beat-to-beat blood pressure. J Appl Physiol. https://doi.org/10.1152/japplphysiol.00657.2005
- [Penzel 2002] Systematic comparison of different algorithms for apnoea detection based on electrocardiogram recordings. Med Biol Eng Comput. https://doi.org/10.1007/BF02345072
- [Philips HCM4686A] Philips Microstream NIV Line, adult, 2 m (capnography sampling line). https://www.usa.philips.com/healthcare/product/HCM4686A/microstream-niv-line-adult-2m-capnography
- [Philips MP40/50 IFU] Philips IntelliVue MP40/MP50 Instructions for Use and Service Guide (copies hosted by a third party behind a bot check, so these two links were not machine-checked; confirm part numbers in the official IFU). https://manualmachine.com/philips/intellivuemp50/7011125-user-manual and https://manualmachine.com/philips/intellivuemp40/7455580-service-manual/
- [pobm, Levy 2021] Digital oximetry biomarkers for assessing respiratory function: standards of measurement, physiological interpretation, and clinical use. npj Digit Med. https://doi.org/10.1038/s41746-020-00373-5; package https://pypi.org/project/pobm/
- [Postema 2008] Accurate electrocardiographic assessment of the QT interval: teach the tangent. Heart Rhythm. https://doi.org/10.1016/j.hrthm.2008.03.037
- [Radha 2019] Sleep stage classification from heart-rate variability using long short-term memory neural networks. Sci Rep. https://doi.org/10.1038/s41598-019-49703-y
- [Rautaharju 2009] AHA/ACCF/HRS recommendations for the standardization and interpretation of the electrocardiogram, part IV: the ST segment, T and U waves, and the QT interval. Circulation. https://doi.org/10.1161/CIRCULATIONAHA.108.191096
- [Sarkar 2008] A detector for a chronic implantable atrial tachyarrhythmia monitor. IEEE TBME. https://doi.org/10.1109/TBME.2007.903707
- [Schaefer 2013] How accurate is pulse rate variability as an estimate of heart rate variability? Int J Cardiol. https://doi.org/10.1016/j.ijcard.2012.03.119
- [Shaffer 2017] An overview of heart rate variability metrics and norms. Front Public Health. https://doi.org/10.3389/fpubh.2017.00258
- [Sridhar 2020] Deep learning for automated sleep staging using instantaneous heart rate. npj Digit Med. https://doi.org/10.1038/s41746-020-0291-x
- [DeepSleepNet, Supratak 2017] DeepSleepNet: a model for automatic sleep stage scoring based on raw single-channel EEG. IEEE TNSRE. https://doi.org/10.1109/TNSRE.2017.2721116
- [Task Force 1996] Heart rate variability: standards of measurement, physiological interpretation and clinical use. Circulation. https://doi.org/10.1161/01.CIR.93.5.1043
- [Tateno 2001] Automatic detection of atrial fibrillation using the coefficient of variation and density histograms of RR and deltaRR intervals. Med Biol Eng Comput. https://doi.org/10.1007/BF02345439
- [Thomas 2005] An electrocardiogram-based technique to assess cardiopulmonary coupling during sleep. Sleep. https://doi.org/10.1093/sleep/28.9.1151
- [Thomas 2007] Differentiating obstructive from central and complex sleep apnea using an automated electrocardiogram-based method. Sleep. https://doi.org/10.1093/sleep/30.12.1756
- [Thomas 2014] Relationship between delta power and the electrocardiogram-derived cardiopulmonary spectrogram: possible implications for assessing the effectiveness of sleep. Sleep Med. https://doi.org/10.1016/j.sleep.2013.10.002
- [Thomas patent US 7,734,334] Assessment of sleep quality and sleep disordered breathing based on cardiopulmonary coupling. US patent 7,734,334 B2. https://patents.google.com/patent/US7734334B2/en
- [YASA, Vallat 2021] An open-source, high-performance tool for automated sleep staging. eLife. https://doi.org/10.7554/eLife.70092
- [Vandenberk 2016] Which QT correction formulae to use for QT monitoring? J Am Heart Assoc. https://doi.org/10.1161/JAHA.116.003264
- [Varon 2015] A novel algorithm for the automatic detection of sleep apnea from single-lead ECG. IEEE TBME. https://doi.org/10.1109/TBME.2015.2422378
- [Vest 2018] An open source benchmarked toolbox for cardiovascular waveform and interval analysis. Physiol Meas. https://doi.org/10.1088/1361-6579/aae021
- [Walch 2019] Sleep stage prediction with raw acceleration and photoplethysmography heart rate data derived from a consumer wearable device. Sleep. https://doi.org/10.1093/sleep/zsz180
- [Warby 2014] Sleep-spindle detection: crowdsourcing and evaluating performance of experts, non-experts and automated methods. Nat Methods. https://doi.org/10.1038/nmeth.2855
- [wfdb XQRS] wfdb-python, `wfdb.processing.XQRS`. https://github.com/MIT-LCP/wfdb-python
- [Zafar 2005] Choice of oximeter affects apnea-hypopnea index. Chest. https://doi.org/10.1378/chest.127.1.80
- [Zhao 2018] SQI quality evaluation mechanism of single-lead ECG signal based on simple heuristic fusion and fuzzy comprehensive evaluation. Front Physiol. https://doi.org/10.3389/fphys.2018.00727
