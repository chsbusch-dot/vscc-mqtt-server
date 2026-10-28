"""Unit tests for the websocket streamer's file tailer (WOR-974).

The capture cleanup (vscc-file-cleanup.py) replaces DataExportVSC.json with
tail + mv, which gives the path a new inode. These drive tail_file_and_broadcast()
against a temp file and record what it broadcasts, so no websocket client is
needed. The script name has hyphens, so it is loaded by path.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "vscc-websocket-streamer.py"
TIMEOUT = 5.0  # generous ceiling so CI scheduling jitter doesn't flake the tests


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vscc_websocket_streamer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(value: int) -> str:
    return json.dumps({"PhysioID": "NOM_TEST", "Value": str(value), "SystemLocalTime": "28-10-2026 12:00:00.000"}) + "\n"


def _append(file_path: Path, value: int) -> None:
    with file_path.open("a") as f:
        f.write(_record(value))


class _Recorder:
    """Stands in for manager.broadcast and keeps the broadcast values."""

    def __init__(self) -> None:
        self.values: list[float] = []

    async def __call__(self, message: str) -> None:
        self.values.append(json.loads(message)["value"])


@pytest.fixture
def streamer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[ModuleType, Path, _Recorder]:
    module = _load()
    data = tmp_path / "DataExportVSC.json"
    monkeypatch.setattr(module, "FILE_PATH", data)
    recorder = _Recorder()
    monkeypatch.setattr(module.manager, "broadcast", recorder)
    return module, data, recorder


async def _until(predicate: Callable[[], bool], feed: Callable[[], None]) -> None:
    """Call feed() every 0.1 s until predicate() holds (bounded).

    Feeding repeatedly makes the tests independent of when the tailer reaches EOF
    or notices a change: a line written just before it (re)opens and seeks to the
    end is skipped, and the next one is read.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while not predicate():
        assert loop.time() < deadline, "timed out waiting for the tailer"
        feed()
        await asyncio.sleep(0.1)


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_follows_a_file_replaced_by_mv(streamer: tuple[ModuleType, Path, _Recorder]) -> None:
    module, data, rec = streamer
    data.write_text(_record(1))  # history from before the tailer started: never streamed
    task = asyncio.create_task(module.tail_file_and_broadcast())
    try:
        await _until(lambda: 2 in rec.values, lambda: _append(data, 2))

        # What the cleanup does: tail into a temp file, then mv it over the original
        tmp = data.with_name(data.name + ".tmp")
        tmp.write_text(_record(3))
        old_ino = data.stat().st_ino
        os.replace(tmp, data)
        assert data.stat().st_ino != old_ino

        await _until(lambda: 4 in rec.values, lambda: _append(data, 4))
        assert 1 not in rec.values  # the old file is not replayed
        assert 3 not in rec.values  # nor the replacement's stale tail
    finally:
        await _stop(task)


async def test_rereads_after_truncation_in_place(streamer: tuple[ModuleType, Path, _Recorder]) -> None:
    module, data, rec = streamer
    data.write_text(_record(1) * 50)
    task = asyncio.create_task(module.tail_file_and_broadcast())
    try:
        await _until(lambda: 2 in rec.values, lambda: _append(data, 2))

        old_ino = data.stat().st_ino
        data.write_text("")  # truncate, same inode
        assert data.stat().st_ino == old_ino

        await _until(lambda: 5 in rec.values, lambda: _append(data, 5))
        assert 1 not in rec.values
    finally:
        await _stop(task)
