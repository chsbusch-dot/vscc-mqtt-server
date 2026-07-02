# vscc-mqtt-server

Bridges VSCapture patient-monitor file exports → MQTT + TimescaleDB (FastAPI worker).

## Quality gate (CI)

`.github/workflows/ci.yml` runs on every push and PR (Python 3.11 & 3.12):
**ruff** (lint) · **mypy** (types, `tests/`) · **pytest**.

Config lives in `pyproject.toml`. Before pushing, run locally:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/mypy tests/ && .venv/bin/pytest -q
```

## Conventions

- **New code is held to the full ruleset.** Keep it lint-clean and typed.
- **Legacy files are grandfathered** in `pyproject.toml` (`per-file-ignores` +
  a mypy `ignore_errors` override) so CI is green — e.g.
  `vscc_mqtt_timescale_worker.py` and the `vscc-rawdata-*` scripts. **Do not
  reformat these to "fix" lint**; their cleanup is tracked as its own ticket.
  If you genuinely improve one, remove its ignore entry in the same PR.
- **Tests:** `tests/test_worker.py` is pure unit tests (no I/O).
  `tests/test_api.py` is integration and **skips** unless a worker is reachable
  (`VSCC_TEST_BASE`), so it's CI-safe.

## Workflow

- Branch → implement → tests → PR. **Don't merge your own PR** — leave it for
  Chris to review (an auto-guard enforces this).
- **Tracking is in Linear, team WOR.** File follow-ups there; put `Closes WOR-#`
  in the PR body so it auto-closes on merge.
- Never change repo visibility — only Chris does that.
