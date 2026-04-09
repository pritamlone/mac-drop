#!/usr/bin/env python3
from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    resources_dir = project_root / "resources"
    destination = resources_dir / "platform-tools"
    archive_path = project_root / ".tmp-platform-tools.zip"

    resources_dir.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        shutil.rmtree(destination)

    print(f"Downloading: {PLATFORM_TOOLS_URL}")
    urllib.request.urlretrieve(PLATFORM_TOOLS_URL, archive_path)

    print("Extracting platform-tools")
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(resources_dir)

    adb_path = resources_dir / "platform-tools" / "adb"
    if not adb_path.exists():
        raise RuntimeError("adb binary not found after extraction")

    adb_path.chmod(adb_path.stat().st_mode | 0o111)
    archive_path.unlink(missing_ok=True)
    print(f"Bundled adb at: {adb_path}")


if __name__ == "__main__":
    main()
