"""Unit tests for vscc-file-cleanup.py, the capture's hourly export truncation.

The script name has hyphens, so it is loaded by path. Every test passes its own
restart callable, so no test ever runs the real `pkill`.
"""

import importlib.util
import os
import time
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "vscc-file-cleanup.py"
OLD = time.time() - 3600  # an hour ago: the capture is idle


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vscc_file_cleanup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fc() -> ModuleType:
    return _load()


def _write(file_path: Path, data: bytes, mtime: float = OLD) -> None:
    file_path.write_bytes(data)
    os.utime(file_path, (mtime, mtime))


class _Restart:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# --- configuration ------------------------------------------------------------

def test_capture_dir_env_is_a_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # os.getenv returns a str; the old code then raised AttributeError on .exists().
    monkeypatch.setenv("VSC_CAPTURE_DIR", str(tmp_path))
    module = _load()
    assert module.CAPTURE_DIR == tmp_path
    assert isinstance(module.CAPTURE_DIR, Path)


def test_capture_dir_default_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VSC_CAPTURE_DIR", raising=False)
    module = _load()
    assert module.CAPTURE_DIR == SCRIPT.parent / "VSCapture"


# --- export_files -------------------------------------------------------------

def test_export_files_matches_every_lead_and_skips_runtime_files(fc: ModuleType, tmp_path: Path) -> None:
    for name in [
        "NOM_ECG_ELEC_POTL_IIWaveExport.csv",
        "NOM_ECG_ELEC_POTL_IWaveExport.csv",
        "NOM_PLETHWaveExport.csv",
        "NOM_RESPWaveExport.csv",
        "MPrawoutput.txt",
        "DataExportVSC.json",
        "VSCaptureCLI.dll",
        "VSCaptureCLI.runtimeconfig.json",
        "vscc-capture-config.conf",
    ]:
        _write(tmp_path / name, b"x")
    names = [f.name for f in fc.export_files(tmp_path)]
    assert names == [
        "NOM_ECG_ELEC_POTL_IIWaveExport.csv",
        "NOM_ECG_ELEC_POTL_IWaveExport.csv",
        "NOM_PLETHWaveExport.csv",
        "NOM_RESPWaveExport.csv",
        "MPrawoutput.txt",
        "DataExportVSC.json",
    ]


# --- capture_is_live ----------------------------------------------------------

def test_capture_is_live_window(fc: ModuleType, tmp_path: Path) -> None:
    data = tmp_path / "DataExportVSC.json"
    now = 1_000_000.0
    _write(data, b"{}", mtime=now - fc.LIVE_WINDOW_S + 5)
    assert fc.capture_is_live([data], now) is True
    os.utime(data, (now - fc.LIVE_WINDOW_S - 5,) * 2)
    assert fc.capture_is_live([data], now) is False


def test_capture_is_live_uses_newest_file(fc: ModuleType, tmp_path: Path) -> None:
    # A wave file still being written counts even if the JSON went quiet.
    now = 1_000_000.0
    stale = tmp_path / "DataExportVSC.json"
    fresh = tmp_path / "NOM_RESPWaveExport.csv"
    _write(stale, b"{}", mtime=now - 3600)
    _write(fresh, b"1", mtime=now - 30)
    assert fc.capture_is_live([stale, fresh], now) is True


def test_capture_is_live_ignores_vanished_file(fc: ModuleType, tmp_path: Path) -> None:
    assert fc.capture_is_live([tmp_path / "gone.csv"], 1_000_000.0) is False


# --- cleanup_files ------------------------------------------------------------

def test_cleanup_truncates_idle_files_and_restarts_once(fc: ModuleType, tmp_path: Path) -> None:
    lead_i = tmp_path / "NOM_ECG_ELEC_POTL_IWaveExport.csv"
    lead_ii = tmp_path / "NOM_ECG_ELEC_POTL_IIWaveExport.csv"
    small = tmp_path / "DataExportVSC.json"
    _write(lead_i, b"0123456789abcdef")
    _write(lead_ii, b"ABCDEFGHIJKLMNOP")
    _write(small, b"tiny")
    restart = _Restart()

    truncated = fc.cleanup_files(tmp_path, max_bytes=6, restart=restart)

    assert truncated == ["NOM_ECG_ELEC_POTL_IIWaveExport.csv", "NOM_ECG_ELEC_POTL_IWaveExport.csv"]
    assert lead_i.read_bytes() == b"abcdef"  # the newest bytes survive
    assert lead_ii.read_bytes() == b"KLMNOP"
    assert small.read_bytes() == b"tiny"
    assert not list(tmp_path.glob("*.tmp"))
    assert restart.calls == 1


def test_cleanup_skips_while_capture_is_live(fc: ModuleType, tmp_path: Path) -> None:
    wave = tmp_path / "NOM_ECG_ELEC_POTL_IIWaveExport.csv"
    _write(wave, b"0123456789abcdef", mtime=time.time())
    restart = _Restart()

    assert fc.cleanup_files(tmp_path, max_bytes=6, restart=restart) == []
    assert wave.read_bytes() == b"0123456789abcdef"
    assert restart.calls == 0


def test_cleanup_without_oversized_files_does_not_restart(fc: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path / "NOM_PLETHWaveExport.csv", b"123")
    restart = _Restart()

    assert fc.cleanup_files(tmp_path, max_bytes=6, restart=restart) == []
    assert restart.calls == 0


def test_cleanup_missing_dir_is_a_no_op(fc: ModuleType, tmp_path: Path) -> None:
    restart = _Restart()
    assert fc.cleanup_files(tmp_path / "absent", restart=restart) == []
    assert restart.calls == 0
