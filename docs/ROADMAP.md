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

### Phase 2.5 — Packaging milestone: one `docker compose up` (also unlocks macOS/Windows)
Goal: the entire product installs with one file and one command on any OS with
Docker — no git clones, no install.sh, no systemd.

1. **Containerize capture + streamer.** Capture container = .NET runtime +
   VSCaptureCLI + keep-alive loop (monitor IP via env var); capture and worker
   share a **named volume** for export files (cleaner than the host directory:
   no root-owned-file traps, no Docker Desktop file-sharing slowness). The
   file-cleanup cron becomes a loop in the capture container. (~2 days)
2. **Publish prebuilt images to GHCR** via GitHub Actions on both repos:
   `ghcr.io/chsbusch-dot/vscc-{capture,worker,streamer,dashboard}`. Tag with
   releases; multi-arch (amd64 + arm64 → Raspberry Pi support for ~free).
3. **Single top-level `docker-compose.yml`** referencing the published images.
   Install story becomes: `curl -O .../docker-compose.yml && MONITOR_IP=... docker compose up -d`.
4. **WSL2 docs page** for Windows (near-zero work: enable systemd in wsl.conf,
   follow Linux instructions — or just use the compose path).
5. `install.sh` remains as the systemd-native Linux option.

Explicitly rejected: fat single-container (or one-per-repo) builds via
supervisord/s6 — they trade away the official EMQX/TimescaleDB image update
path, tangle restart lifecycles (capture restarts by design whenever the
monitor power-cycles; it must not share a container with the database), and
buy nothing the compose file doesn't already deliver. The only future
single-artifact play is an appliance image (Pi SD card / OVA) that wraps the
compose stack.

Native macOS/Windows services (launchd/NSSM): not planned. Only serial/MIB
capture would ever require them (Docker Desktop cannot pass serial ports);
defer until someone actually asks.

### Phase 2.6 — Launch sequence (agreed 2026-06-11)
Done: both repos public + MIT + disclaimers, packaging milestone shipped and
twice smoke-tested from zero, GHCR images public, v0.1.0 tagged, repo
descriptions/topics set, repos pinned on the GitHub profile. Two-repo layout is
deliberate: vscc-mqtt-server is the front door (quick start, screenshot,
compose file); vscc-dashboard-client is the satellite. Cross-repo merge was
rejected (no redirects for merges, distinct toolchains/audiences, licensing
boundaries for a future open-core split).

Remaining, in order:
1. **Live-data screenshot/GIF** from a freshly-installed box (the smoke-test VM
   is armed: power the MP50 on, capture real waveforms) — the "this machine
   installed itself an hour ago" demo claim for the post.
2. **Launch posts** (draft for review before anything goes public):
   master version + a VSCapture-forum variant leading with credit/lineage.
   Targets in order: VSCapture SourceForge forum, r/BMET /
   r/BiomedicalEngineers, VitalDB user group, optionally Show HN.
3. **Post-launch queue:** demo/replay mode first (visitors without hardware
   see living charts), then the dynamic-waveform dashboard refactor when BIS
   capture data exists.

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
- ✅ **Shipped** — Recording sessions: named start/stop with metadata (case ID,
  subject code, notes); session list API.
- ✅ **Shipped** — Annotation API: timestamped event markers (drug given, position
  change, stimulus).
- ✅ **Shipped** — Gap detection + integrity report: sequence tracking, per-signal
  data-loss statistics, clock-offset report per source.
- TimescaleDB continuous aggregates (1 s / 1 min rollups), compression policies,
  configurable retention per signal class. *(1-min aggregate shipped; rest pending.)*
- ✅ **Shipped** — Export API: EDF / Parquet / CSV by time range or session.
  *(VitalDB format still pending.)*
- Import/backfill: VitalDB files, CSVs from other tools.
- ✅ **Shipped** — Replay service: virtual-monitor demo mode (re-publishes a
  recorded session through the real pipeline; no hardware).

**Capture & devices**
- ❌ **Not possible without a VSCapture patch** — Alarm/event capture. VSCapture's
  export stream contains NO alarm data (no alarm fields in the numerics JSON, no
  alarm strings in the raw protocol dump, no alarm export files); the IntelliVue
  protocol carries alarms but VSCaptureCLI never reads them, and its source is not
  public. Surfacing alarms needs a VSCapture decompile-patch (Phase 3 territory),
  not a pipeline parser. Confirmed 2026-06-12.
- ✅ **Shipped** — Dashboard-managed capture config (monitor IP, interval, waveset,
  scale, devid) with live recycle.
- Multi-device topic namespacing: `site/bed/device/signal`.
- Device registry with per-device timezone, labels, expected signals.

**Operations & security**
- EMQX authentication + TLS (per-client credentials) — required beyond the LAN.
- ✅ **Shipped** — Prometheus metrics + status endpoint (capture state, last-data
  age, DB lag, per-source clock offset).
- Watchdog notifications: ✅ gap watchdog logs capture-liveness transitions;
  webhook/email/Telegram delivery still pending.
- De-identification pipeline for shared datasets.
- One-command install/update; versioned releases.

### Dashboard — features
- Multi-bed overview grid (enterprise flagship view).
- Session browser + DB-backed replay with scrubbing and speed control. *(Session
  browser + load/replay shipped; scrubbing/speed pending.)*
- ✅ **Shipped** — Chart annotations: add/list/delete event markers from the UI.
  *(Live-chart vertical-line overlay still pending.)*
- ❌ **Not possible** — Alarm history lane (depends on alarm capture above).
- ✅ **Shipped** — Health status indicator.
- Saved/custom layouts; touch/tablet mode; PDF/PNG case report export.
- Signal-quality indicators (lead-off, gap shading on the trace).

### Dashboard — advanced calculations
(Compute server-side in the worker — Python/NumPy/SciPy — publish as
`mp50/derived/*` topics and store in DB so every client benefits; dashboard-only
math is fine for display-time transforms.)

**ECG**
- R-peak detection (Pan-Tompkins) → beat-to-beat HR.
- QT/QTc trending; simple arrhythmia flags (research-only labeling).

**PPG / PLETH**
- Pleth Variability Index (respiratory variation in pleth amplitude).
- Pulse Transit Time (ECG R-peak → pleth foot) — research surrogate for BP trend.
- Perfusion index trending; pulse deficit (ECG HR vs pleth pulse).

**Respiration**
- Rate from waveform; apnea/irregularity flags (research-only); RSA estimate.

**Hemodynamics**
- MAP trend bands, shock index (HR/SBP), rate-pressure product.

**Cross-signal & statistical**
- Generic FFT/PSD viewer for any waveform window.
- Rolling percentile bands; short-horizon trend forecast.
- Time-in-range metrics (e.g., SpO₂ < 90 % duration) and per-session summary
  statistics.
- ML hooks: export sliding windows to ONNX models (research models only, e.g.
  hypotension-prediction-style indices) with results as another derived topic.

### Planned refactor — dynamic waveform registry (dashboard)
Status: deferred until informed by real capture data; do on a feature branch
(`feat/dynamic-waveforms`), never blind to `main`.

The dashboard's state model is hardcoded to exactly five waveforms:

```ts
export type WaveformId = 'VitalSigns' | 'ECG' | 'Pleth' | 'Resp';  // closed union
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

### Suggested quick wins (high value / low effort) — ✅ ALL SHIPPED 2026-06-12
1. ✅ Disconnect/gap watchdog notification (backend) — logs capture-liveness transitions.
2. ✅ EDF + Parquet export endpoint — instantly research-credible.
3. ✅ Replay/demo mode — unlocks no-hardware demos and CI.
4. ✅ Chart annotations — add/list/delete event markers from the UI.

All four validated against the live MP50 on 2026-06-12 (and the released images
deployed to the capture box). Alarms are the one explicitly-blocked item (see
Capture & devices).

### Prioritized next features (2026-06-12)
A curated, prioritized cut of the brainstorm above — sorted by effort-vs-impact.
The fast wins all derive new insight from signals we already capture.

**🔥 Top picks — cheap now, high wow**
1. **Pulse Transit Time (PTT)** — delay from the ECG R-peak to the pleth foot; a
   cuffless surrogate for a blood-pressure *trend* from two signals we already
   have. Reuses the R-peak detector. High demo value, hot research topic.

**Strong — slightly bigger**
2. **Pleth Variability Index (PVI)** — respiratory variation in pleth amplitude →
   fluid-responsiveness research metric.
3. **ECG/Pleth-derived respiration (EDR)** — recover respiratory rate from a signal
   that doesn't directly measure it; validate against the real RESP channel.
4. **QT/QTc trending** — R- and T-wave detection → drug-safety-style ECG monitoring.

**Research-credibility / workflow (broad forum-launch appeal)**
5. **PDF/PNG case-report export** — charts + session stats +
   annotations in one shareable report. Highly practical for researchers/vets.
6. **Session replay with scrubber + speed control** — drag a timeline, 1×/4×/16×.
   Demo-friendly, commonly requested.
7. **VitalDB import/export** — interop with the big open physiological dataset;
   instant credibility + a community hook for the forum posts.
8. **Time-in-range / event stats** — e.g. "SpO₂ < 90 % for 4 min", per-session
    summary. Easy, useful.

**Polish**
9. Signal-quality / gap shading on the traces;
    saved/custom layouts; de-identification-for-sharing pipeline.

Suggested order: **PTT → PDF report export** (BP-trend wow, and the thing that
makes a case actually shareable).
