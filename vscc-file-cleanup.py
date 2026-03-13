import os
import subprocess
from pathlib import Path

# --- CONFIGURATION ---
# The directory where VSCapture exports its data
BASE_DIR = Path(__file__).resolve().parent
CAPTURE_DIR = os.getenv("VSC_CAPTURE_DIR", BASE_DIR / "VSCapture")

# Max file size in bytes (20 MB)
MAX_SIZE_BYTES = 20 * 1024 * 1024

# List of data files to check and truncate.
# Note: We are only targeting data outputs (.csv, .json, .txt).
# Truncating binaries, DLLs, or runtime configs would corrupt them.
FILES_TO_CLEAN = [
    "NOM_ECG_ELEC_POTL_IIWaveExport.csv",
    "NOM_EEG_ELEC_POTL_CRTXWaveExport.csv",
    "NOM_PLETHWaveExport.csv",
    "NOM_RESPWaveExport.csv",
    "MPrawoutput.txt",
    "DataExportVSC.json",
]
# ---------------------

def cleanup_files():
    """
    Checks specified files in the capture directory and truncates them
    if they exceed MAX_SIZE_BYTES, keeping the most recent data.
    """
    print(f"[{Path(__file__).name}] Starting file cleanup check in: {CAPTURE_DIR}")
    
    if not CAPTURE_DIR.exists() or not CAPTURE_DIR.is_dir():
        print(f"Warning: Capture directory not found at '{CAPTURE_DIR}'. Skipping cleanup.")
        return

    for filename in FILES_TO_CLEAN:
        file_path = CAPTURE_DIR / filename
        
        if not file_path.exists():
            continue

        try:
            current_size = file_path.stat().st_size

            if current_size > MAX_SIZE_BYTES:
                print(f"File '{filename}' is {current_size / 1024 / 1024:.2f} MB, which exceeds the {MAX_SIZE_BYTES / 1024 / 1024:.0f} MB limit. Truncating...")
                
                # Define a temporary file path
                temp_path = file_path.with_suffix(f"{file_path.suffix}.tmp")

                # Use shell's `tail` and `mv` for efficient, atomic truncation
                # This is safer than reading/writing in Python for large files
                cmd = f"tail -c {MAX_SIZE_BYTES} '{file_path}' > '{temp_path}' && mv '{temp_path}' '{file_path}'"
                
                # Execute the command in a shell
                process = subprocess.run(cmd, shell=True, capture_output=True, text=True)

                if process.returncode == 0:
                    print(f"Successfully truncated '{filename}'.")
                else:
                    print(f"Error truncating '{filename}': {process.stderr}")

        except FileNotFoundError:
            # This can happen in a race condition if the file is deleted while the script runs.
            print(f"File '{filename}' not found during processing. Skipping.")
        except Exception as e:
            print(f"An unexpected error occurred while processing '{filename}': {e}")

if __name__ == "__main__":
    print("========================================")
    cleanup_files()
    print("========================================")
