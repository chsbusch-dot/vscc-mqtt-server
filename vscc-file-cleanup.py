import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# --- CONFIGURATION ---
# The directory where VSCapture exports its data. The capture container sets
# VSC_CAPTURE_DIR=/data. Wrapped in Path() because os.getenv returns a str.
BASE_DIR = Path(__file__).resolve().parent
CAPTURE_DIR = Path(os.getenv("VSC_CAPTURE_DIR") or BASE_DIR / "VSCapture")

# Max file size in bytes (20 MB)
MAX_SIZE_BYTES = 20 * 1024 * 1024

# The capture counts as live while any export file was written within this many
# seconds (DataExportVSC.json is written every numerics interval while the
# monitor is associated). A truncation restarts VSCapture, which would cost a
# live recording a ~3 min gap, so the run is skipped and the first idle run
# after the recording ends does the work.
LIVE_WINDOW_S = 600

# Data files to check and truncate.
# Note: We are only targeting data outputs (.csv, .json, .txt).
# Truncating binaries, DLLs, or runtime configs would corrupt them.
# Wave exports are matched by pattern because the exported ECG lead can
# switch (lead I as well as lead II), and each lead gets its own file.
WAVE_PATTERN = "*WaveExport.csv"
OTHER_FILES = [
    "MPrawoutput.txt",
    "DataExportVSC.json",
]
# ---------------------


def export_files(capture_dir: Path) -> list[Path]:
    """The export files present in capture_dir, wave files first."""
    waves = sorted(capture_dir.glob(WAVE_PATTERN))
    others = [capture_dir / name for name in OTHER_FILES if (capture_dir / name).exists()]
    return waves + others


def capture_is_live(files: list[Path], now: float | None = None) -> bool:
    """True when any of the files was modified within LIVE_WINDOW_S."""
    now = time.time() if now is None else now
    for file_path in files:
        try:
            if now - file_path.stat().st_mtime < LIVE_WINDOW_S:
                return True
        except FileNotFoundError:
            continue
    return False


def restart_capture() -> None:
    # Both the cron job and the service run under the same user, so pkill does not need sudo
    subprocess.run("pkill -f VSCaptureCLI.dll", shell=True)


def cleanup_files(
    capture_dir: Path = CAPTURE_DIR,
    max_bytes: int = MAX_SIZE_BYTES,
    now: float | None = None,
    restart: Callable[[], None] = restart_capture,
) -> list[str]:
    """
    Checks the export files in the capture directory and truncates them
    if they exceed max_bytes, keeping the most recent data.
    Returns the names of the truncated files.
    """
    print(f"[{Path(__file__).name}] Starting file cleanup check in: {capture_dir}")

    if not capture_dir.is_dir():
        print(f"Warning: Capture directory not found at '{capture_dir}'. Skipping cleanup.")
        return []

    files = export_files(capture_dir)
    if capture_is_live(files, now):
        print(f"Capture is live (an export file was written in the last {LIVE_WINDOW_S} s). Skipping cleanup.")
        return []

    truncated: list[str] = []

    for file_path in files:
        filename = file_path.name
        try:
            current_size = file_path.stat().st_size

            if current_size > max_bytes:
                print(f"File '{filename}' is {current_size / 1024 / 1024:.2f} MB, which exceeds the {max_bytes / 1024 / 1024:.0f} MB limit. Truncating...")

                # Define a temporary file path
                temp_path = file_path.with_suffix(f"{file_path.suffix}.tmp")

                # Use shell's `tail` and `mv` for efficient, atomic truncation
                # This is safer than reading/writing in Python for large files
                cmd = f"tail -c {max_bytes} '{file_path}' > '{temp_path}' && mv '{temp_path}' '{file_path}'"

                # Execute the command in a shell
                process = subprocess.run(cmd, shell=True, capture_output=True, text=True)

                if process.returncode == 0:
                    print(f"Successfully truncated '{filename}'.")
                    truncated.append(filename)
                else:
                    print(f"Error truncating '{filename}': {process.stderr}")

        except FileNotFoundError:
            # This can happen in a race condition if the file is deleted while the script runs.
            print(f"File '{filename}' not found during processing. Skipping.")
        except Exception as e:
            print(f"An unexpected error occurred while processing '{filename}': {e}")

    # If files were replaced, VSCapture is now writing to orphaned file handles.
    # We must gracefully kill the VSCapture process so the capture loop
    # can catch the exit and automatically restart it to bind to the new file inodes.
    if truncated:
        print("Files were truncated. Restarting VSCapture process to bind to new files...")
        restart()

    return truncated


if __name__ == "__main__":
    print("========================================")
    cleanup_files()
    print("========================================")
