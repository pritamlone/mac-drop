# mac-drop

`mac-drop` is a Finder-style desktop app for transferring files and folders between macOS (Intel + Apple Silicon) and Android devices over ADB.

## Goals

- Finder-like dual-pane file browser
- Local Mac files on one side, Android files on the other
- Upload/download folders recursively via `adb push` / `adb pull`
- Built-in ADB management so users do not need manual setup

## Current MVP Features

- Device discovery (`adb devices`)
- Local file browsing
- Android file browsing (`/sdcard` default)
- Upload selected local files/folders to Android
- Download selected Android files/folders to Mac
- Create and delete folders on Android
- Auto-discover `adb` from:
  - `MAC_DROP_ADB_PATH`
  - bundled `resources/platform-tools/adb`
  - system `PATH`
  - auto-download from official Google platform-tools URL if missing

## Project Layout

- `src/mac_drop/main.py`: app entrypoint
- `src/mac_drop/main_window.py`: Finder-like UI
- `src/mac_drop/adb_service.py`: ADB discovery, download, and commands
- `src/mac_drop/models.py`: shared data model
- `src/mac_drop/workers.py`: background worker helpers
- `scripts/fetch_adb.py`: prefetch ADB binary into project resources

## Run (Development)

```bash
cd "/Users/pritamrameshlone/Documents/New project/mac-drop"
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python run.py
```

## Bundle ADB Ahead Of Time

To ship with bundled `adb` in this project folder:

```bash
python scripts/fetch_adb.py
```

This will place platform-tools in:

- `resources/platform-tools/`

## Notes

- On first run, if no `adb` is found, `mac-drop` tries to download official platform-tools automatically.
- Android path parsing varies by vendor/ROM; listing parser is best-effort and tuned for common Android shell output.
