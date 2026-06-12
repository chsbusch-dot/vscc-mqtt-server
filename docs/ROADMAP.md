# VSCC Roadmap — Product Plan & Feature Brainstorm

Status: working draft, 2026-06-11. Context: open telemetry capture + charting for
Philips MP50 (and later other monitors), positioned for research/education/veterinary
use — explicitly **not a medical device, not for clinical decision-making**.

---

## Part A — Structured plan

### Phase 0 — Positioning & groundwork (before anything public)
- **Audience:** research labs, simulation/training centers, veterinary clinics,
  device tinkerers. Clinical patient monitoring is permanently out of scope
  (FDA 510(k)/CE-MDR territory).
- **Disclaimer everywhere:** "Research and education use only. Not a medical device."
- **License audit:**
  - VSCapture (capture layer): LGPL/GPL — verify exact terms; keep it an isolated
    process (already true) so our code stays untainted.
  - SciChart: commercial license required for any paid product; community edition
    may need a free chart library (uPlot) instead.
  - Our code (worker, streamer, dashboard): our copyright — dual-licensable.
- **Branding:** distinct from Philips/IntelliVue trademarks.

### Phase 1 — Research-grade credibility (harden current stack)
- Timestamp integrity: NTP-anchored host clock, documented monitor-clock drift
  handling (MONITOR_TZ conversion shipped 2026-06-11; add drift reporting).
- Lossless-capture validation: quantify waveform drops at `-waveset 12`
  (the monitor warns about bandwidth); publish loss statistics.
- Export formats researchers use: EDF, Parquet, CSV time-range export,
  VitalDB-compatible output.
- Replay/demo mode: re-publish recorded sessions over MQTT (doubles as the
  no-hardware demo for adoption and as CI fixtures).
- Documentation pass: install, architecture, validation methodology.

### Phase 2 — Community edition launch
- Single-monitor stack, one-command install, demo mode, public repo polish.
- Announce where the users are: VSCapture community, anesthesia-informatics
  forums, r/BMET, biomedical engineering courses.
- Treat "can you set this up for our lab?" emails as pricing research.

### Phase 3 — Capture rebuild (own the whole stack)
Clean-room from the published Philips IntelliVue Data Export Protocol guide —
**not** translated from VSCapture's GPL source.
- 3a. Offline decoder against the recorded raw corpus (`MPrawoutput.txt`, 435 MB)
  validated byte-for-byte against VSCapture's own CSV/JSON from the same session.
- 3b. Live association: handshake, MDS event, 1 s numerics polling.
- 3c. Waveforms: waveset selection, sample-array decode, scaled/calibrated values.
- 3d. Integration: one service publishing UTC-stamped MQTT directly (waveforms
  included) — retires the file layer, cleanup cron, PTY wrapper, and the
  stale-association class of bugs (proper disassociation on shutdown).
- Payoff: no GPL constraint on the capture layer; fixes weird-character labels,
  timestamps, and lifecycle at the root.

### Phase 4 — Multi-vendor support (driver architecture)
- Protocol-agnostic capture core + per-vendor drivers; replay harness feeds
  recorded streams over UDP / virtual serial (`socat`) for development and CI.
- Community pipeline: users contribute raw captures from their hardware; we build
  decoders against recordings; they beta-test live.
- Priority by research demand: Philips IntelliVue (done in Phase 3) → GE Datex
  S/5 + GE Dash (serial; specs findable; ~$100–500 used on eBay when needed) →
  HL7 vendors (Mindray, Draeger HL7 — testable from spec + HL7 simulators, no
  hardware required) → others on request.

### Phase 5 — Monetization (open-core)
- **Community (free, OSS):** single monitor, capture → MQTT → TimescaleDB →
  dashboard, demo mode.
- **Enterprise (paid):** multi-monitor/multi-bed fleet, auth/RBAC + audit logs,
  data-integrity reports, de-identification/PHI tooling, research exports,
  REDCap integration, long-term archival, support SLAs, managed installs.
- Services-first revenue (setup, support, custom integrations) before license
  keys. Precedent that this niche pays: ixTrend (ixellence) sells commercial
  IntelliVue export software. Likely-paying segments: grant-funded labs, CROs,
  simulation centers, veterinary chains.

---

## Part B — Feature brainstorm

### MQTT server / backend
**Data & sessions**
- Recording sessions: named start/stop with metadata (case ID, subject code,
  notes); session list API.
- Annotation API: timestamped event markers (drug given, position change,
  stimulus) — essential for research analysis.
- Gap detection + integrity report: sequence tracking, per-signal data-loss
  statistics, clock-offset report per source.
- TimescaleDB continuous aggregates (1 s / 1 min rollups), compression policies,
  configurable retention per signal class.
- Export API: EDF / Parquet / CSV / VitalDB by time range or session.
- Import/backfill: VitalDB files, CSVs from other tools.
- Replay service: re-publish any stored session over MQTT at 1×/N× speed
  (demo mode, teaching, dashboard development).

**Capture & devices**
- Alarm/event capture from the monitor (IntelliVue exports alarms; current
  pipeline ignores them) — display + log only, never alerting.
- Multi-device topic namespacing: `site/bed/device/signal`.
- Device registry with per-device timezone, labels, expected signals.

**Operations & security**
- EMQX authentication + TLS (per-client credentials) — required beyond the LAN.
- Prometheus metrics + status endpoint (capture state, last-data age, DB lag).
- Watchdog notifications: webhook/email/Telegram when monitor offline, data gap,
  or disk pressure.
- De-identification pipeline for shared datasets.
- One-command install/update; versioned releases.

### Dashboard — features
- Multi-bed overview grid (enterprise flagship view).
- Session browser + DB-backed replay with scrubbing and speed control.
- Chart annotations (render the annotation API; add events from the UI).
- Alarm history lane under the charts.
- Saved/custom layouts; touch/tablet mode; PDF/PNG case report export.
- Signal-quality indicators (lead-off, gap shading on the trace).

### Dashboard — advanced calculations
(Compute server-side in the worker — Python/NumPy/SciPy — publish as
`mp50/derived/*` topics and store in DB so every client benefits; dashboard-only
math is fine for display-time transforms.)

**ECG**
- R-peak detection (Pan-Tompkins) → beat-to-beat HR.
- HRV suite: SDNN, RMSSD, pNN50, LF/HF (Welch PSD), Poincaré plot
  (extends the existing PPI plot).
- QT/QTc trending; simple arrhythmia flags (research-only labeling).

**PPG / PLETH**
- Pleth Variability Index (respiratory variation in pleth amplitude).
- Pulse Transit Time (ECG R-peak → pleth foot) — research surrogate for BP trend.
- Perfusion index trending; pulse deficit (ECG HR vs pleth pulse).

**Respiration**
- Rate from waveform; apnea/irregularity flags (research-only); RSA estimate.

**EEG / BIS**
- Full spectrogram view (extend the existing toggle), band-power trends
  (delta/theta/alpha/beta already exported), spectral edge frequency,
  burst-suppression ratio plotting.

**Hemodynamics**
- MAP trend bands, shock index (HR/SBP), rate-pressure product.

**Cross-signal & statistical**
- Generic FFT/PSD viewer for any waveform window.
- Rolling percentile bands; z-score anomaly highlighting; short-horizon trend
  forecast.
- Time-in-range metrics (e.g., SpO₂ < 90 % duration) and per-session summary
  statistics.
- ML hooks: export sliding windows to ONNX models (research models only, e.g.
  hypotension-prediction-style indices) with results as another derived topic.

### Planned refactor — dynamic waveform registry (dashboard)
Status: deferred until informed by real capture data; do on a feature branch
(`feat/dynamic-waveforms`), never blind to `main`.

The dashboard's state model is hardcoded to exactly five waveforms:

```ts
export type WaveformId = 'VitalSigns' | 'ECG' | 'EEG' | 'Pleth' | 'Resp';  // closed union
globalWaveformToggles: Record<WaveformId, boolean>   // toggles keyed by those 5
providerMappings:      Record<WaveformId, ...>       // topics keyed by those 5
fileInputs / uploadProgress: Record<WaveformId, ...> // ditto
```

The MQTT handler routes by exact topic→WaveformId match (`Sidebar.tsx:314`) and
drops anything unknown; SciChart series are created from a fixed `physio_id`
list. The backend already auto-discovers new waveform exports and publishes
them (`mp50/HF-<PhysioID>`), so a new module (e.g. BIS) flows end-to-end until
it hits the dashboard and is silently dropped.

Required changes (multi-file, core state model):
1. Wildcard MQTT subscribe (`mp50/#` is already the default — keep).
2. Open the closed `WaveformId` union into a dynamic registry populated from
   incoming topics/physio_ids (with `PHYSIO_META` as the display-name catalog
   and a sane fallback for unknown ids).
3. Dynamic SciChart series creation/teardown per registered waveform
   (follow `ChartContainer.tsx` lifecycle rules; fifoCapacity per sample rate).
4. Dynamic UI toggles generated from the registry.

Risk controls (why this is sequenced, not immediate):
- SciChart rendering cannot be verified headless; a compiling-but-broken change
  could silently kill the live dashboard. Build-gate catches compile errors only.
- Design should be informed by REAL topic names/physio_ids (e.g. the actual BIS
  export filename) from a capture session with the new modules attached — the
  worker already captures, stores, and publishes every wave, so no data is lost
  while the dashboard refactor waits.

Agreed sequence: capture session with new modules → inspect real topics →
refactor on `feat/dynamic-waveforms` → human-in-the-loop visual test against the
live stack → merge.

### Suggested quick wins (high value / low effort)
1. Disconnect/gap watchdog notification (backend) — operational pain solved.
2. EDF + Parquet export endpoint — instantly research-credible.
3. HRV metrics + Poincaré from existing ECG stream — flagship "advanced" demo.
4. Replay/demo mode — unlocks no-hardware demos and CI.
5. Chart annotations — most-requested research feature in comparable tools.
