"""Regression tests for the pure logic in the MQTT/TimescaleDB worker.

These cover the parts most likely to silently break a live capture: waveform
file -> PhysioID -> topic mapping (the auto-discovery path) and VSCapture
timestamp parsing (local-wall-clock -> UTC conversion).

They deliberately avoid the async I/O paths (file tailing, MQTT publish, DB
insert); those need integration fixtures and are tracked separately.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import vscc_mqtt_timescale_worker as w

# --- physio_id_from_wave_file -------------------------------------------------

@pytest.mark.parametrize("filename,expected", [
    ("NOM_PLETHWaveExport.csv", "NOM_PLETH"),
    ("NOM_ECG_ELEC_POTL_IIWaveExport.csv", "NOM_ECG_ELEC_POTL_II"),
    ("NOM_EEG_ELEC_POTL_CRTXWaveExport.csv", "NOM_EEG_ELEC_POTL_CRTX"),
])
def test_physio_id_from_wave_file_standard(filename, expected):
    assert w.physio_id_from_wave_file(Path(filename)) == expected


def test_physio_id_from_wave_file_nonstandard_falls_back_to_stem():
    # A file that doesn't match the WaveExport.csv suffix falls back to the stem.
    assert w.physio_id_from_wave_file(Path("random.csv")) == "random"


# --- wave_topic ---------------------------------------------------------------

@pytest.mark.parametrize("physio_id,expected", [
    # Known aliases keep the existing short dashboard topics.
    ("NOM_ECG_ELEC_POTL_II", "mp50/HF-ECG"),
    ("NOM_PLETH", "mp50/HF-PLETH"),
    ("NOM_RESP", "mp50/HF-RESP"),
])
def test_wave_topic_known_aliases(physio_id, expected):
    assert w.wave_topic(physio_id) == expected


def test_wave_topic_unknown_nom_id_slugifies():
    # A brand-new module (e.g. BIS connected mid-session) must still get a topic
    # with no code change -- this is the whole point of auto-discovery. Neuro/EEG
    # was dropped from the community (MMS-only) build, so EEG now takes this path.
    assert w.wave_topic("NOM_BIS_INDEX") == "mp50/HF-BIS_INDEX"
    assert w.wave_topic("NOM_EEG_ELEC_POTL_CRTX") == "mp50/HF-EEG_ELEC_POTL_CRTX"


def test_wave_topic_non_nom_id_slugifies():
    assert w.wave_topic("CUSTOM_SIGNAL") == "mp50/HF-CUSTOM_SIGNAL"


# --- wave_config --------------------------------------------------------------

def test_wave_config_shape():
    cfg = w.wave_config(Path("/data/NOM_PLETHWaveExport.csv"))
    assert cfg["type"] == "csv"
    assert cfg["topic"] == "mp50/HF-PLETH"
    assert cfg["physio_id"] == "NOM_PLETH"
    assert cfg["path"] == Path("/data/NOM_PLETHWaveExport.csv")


# --- parse_vsc_timestamp ------------------------------------------------------

def test_parse_timestamp_with_millis_converts_la_to_utc():
    # 12:00:00 PDT (June, UTC-7) -> 19:00:00 UTC.
    got = w.parse_vsc_timestamp("11-06-2026 12:00:00.500")
    assert got == datetime(2026, 6, 11, 19, 0, 0, 500000, tzinfo=UTC)
    assert got.tzinfo == UTC


def test_parse_timestamp_without_millis():
    got = w.parse_vsc_timestamp("11-06-2026 12:00:00")
    assert got == datetime(2026, 6, 11, 19, 0, 0, tzinfo=UTC)


def test_parse_timestamp_winter_offset():
    # January is PST (UTC-8): 12:00 PST -> 20:00 UTC. Guards the DST boundary.
    got = w.parse_vsc_timestamp("15-01-2026 12:00:00")
    assert got == datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["", "not-a-date", "2026-06-11 12:00:00", None])
def test_parse_timestamp_invalid_returns_none(bad):
    assert w.parse_vsc_timestamp(bad) is None
