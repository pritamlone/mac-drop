from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size_bytes: int | None
    modified: datetime | None


@dataclass(slots=True)
class AdbDevice:
    serial: str
    status: str
    details: str
    display_name: str = ""
