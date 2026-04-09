from __future__ import annotations

import os
import posixpath
import re
import select
import shlex
import shutil
import subprocess
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import AdbDevice, FileEntry

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"


class AdbError(RuntimeError):
    pass


class AdbService:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.cache_root = Path.home() / ".mac-drop" / "platform-tools"
        self._adb_path: Path | None = None
        self._adb_source = "unknown"

    @property
    def adb_source(self) -> str:
        return self._adb_source

    def ensure_ready(self) -> str:
        adb_path = self.resolve_adb_path()
        self.run(["start-server"], check=False, timeout=20)
        return str(adb_path)

    def resolve_adb_path(self) -> Path:
        if self._adb_path and self._adb_path.exists():
            return self._adb_path

        env_path = os.getenv("MAC_DROP_ADB_PATH")
        candidates = []
        if env_path:
            candidates.append((Path(env_path).expanduser(), "MAC_DROP_ADB_PATH"))

        bundled_candidates = [
            self.project_root / "resources" / "platform-tools" / "adb",
            self.project_root / "resources" / "platform-tools" / "platform-tools" / "adb",
        ]
        candidates.extend((p, "bundled") for p in bundled_candidates)

        cached_adb = self.cache_root / "platform-tools" / "adb"
        candidates.append((cached_adb, "cached"))

        which_adb = shutil.which("adb")
        if which_adb:
            candidates.append((Path(which_adb), "system-path"))

        for candidate, source in candidates:
            if candidate.exists() and candidate.is_file():
                candidate.chmod(candidate.stat().st_mode | 0o111)
                self._adb_path = candidate
                self._adb_source = source
                return candidate

        downloaded = self._download_platform_tools()
        self._adb_path = downloaded
        self._adb_source = "auto-downloaded"
        return downloaded

    def _download_platform_tools(self) -> Path:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        zip_path = self.cache_root / "platform-tools-latest-darwin.zip"

        try:
            urllib.request.urlretrieve(PLATFORM_TOOLS_URL, zip_path)
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(self.cache_root)
        except Exception as exc:
            raise AdbError(
                "Unable to find or download adb. Set MAC_DROP_ADB_PATH or run scripts/fetch_adb.py."
            ) from exc

        adb_path = self.cache_root / "platform-tools" / "adb"
        if not adb_path.exists():
            raise AdbError("Downloaded platform-tools but adb binary was not found.")

        adb_path.chmod(adb_path.stat().st_mode | 0o111)
        return adb_path

    def run(
        self,
        args: list[str],
        device: str | None = None,
        timeout: int | None = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        adb_path = self.resolve_adb_path()
        cmd: list[str] = [str(adb_path)]
        if device:
            cmd.extend(["-s", device])
        cmd.extend(args)

        completed = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

        if check and completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            details = stderr or stdout or f"exit code {completed.returncode}"
            raise AdbError(f"adb command failed: {' '.join(cmd)}\n{details}")

        return completed

    def list_devices(self) -> list[AdbDevice]:
        completed = self.run(["devices", "-l"], check=True)
        devices: list[AdbDevice] = []

        for raw in completed.stdout.splitlines()[1:]:
            line = raw.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            status = parts[1]
            details = " ".join(parts[2:])
            display_name = self._resolve_device_name(serial, status, details)
            devices.append(AdbDevice(serial=serial, status=status, details=details, display_name=display_name))

        return devices

    def _resolve_device_name(self, serial: str, status: str, details: str) -> str:
        detail_map = self._parse_detail_map(details)
        from_details = self._compose_name(
            detail_map.get("manufacturer", ""),
            detail_map.get("marketname", ""),
            detail_map.get("model", ""),
            detail_map.get("device", ""),
        )
        if from_details:
            return from_details

        if status != "device":
            return ""

        props = self._read_device_properties(
            serial,
            [
                "ro.product.manufacturer",
                "ro.product.marketname",
                "ro.product.model",
                "ro.product.device",
            ],
        )
        return self._compose_name(
            props.get("ro.product.manufacturer", ""),
            props.get("ro.product.marketname", ""),
            props.get("ro.product.model", ""),
            props.get("ro.product.device", ""),
        )

    def _parse_detail_map(self, details: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for token in details.split():
            if ":" not in token:
                continue
            key, value = token.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                parsed[key] = value
        return parsed

    def _read_device_properties(self, serial: str, keys: list[str]) -> dict[str, str]:
        props: dict[str, str] = {}
        for key in keys:
            try:
                result = self.run(
                    ["shell", "getprop", key],
                    device=serial,
                    timeout=5,
                    check=False,
                )
            except Exception:
                continue
            if result.returncode != 0:
                continue
            value = (result.stdout or "").strip()
            if value:
                props[key] = value
        return props

    def _compose_name(self, manufacturer: str, marketname: str, model: str, device_name: str) -> str:
        manufacturer = manufacturer.strip()
        marketname = marketname.strip()
        model = model.strip().replace("_", " ")
        device_name = device_name.strip().replace("_", " ")

        chosen = marketname or model or device_name
        if not chosen:
            return ""
        if manufacturer and manufacturer.lower() not in chosen.lower():
            return f"{manufacturer} {chosen}"
        return chosen

    def list_remote_dir(self, remote_path: str, device: str) -> list[FileEntry]:
        remote_path = self._normalize_remote_path(remote_path)
        entries = self._list_remote_dir_ls(remote_path, device)
        if not entries:
            try:
                entries = self._list_remote_dir_find(remote_path, device)
            except AdbError:
                entries = []
        if not entries and remote_path == "/storage/emulated/0":
            try:
                alt_entries = self._list_remote_dir_ls("/sdcard", device)
                if not alt_entries:
                    alt_entries = self._list_remote_dir_find("/sdcard", device)
            except AdbError:
                alt_entries = []
            if alt_entries:
                mapped: list[FileEntry] = []
                for entry in alt_entries:
                    mapped_path = entry.path
                    if mapped_path == "/sdcard":
                        mapped_path = remote_path
                    elif mapped_path.startswith("/sdcard/"):
                        mapped_path = remote_path + mapped_path[len("/sdcard") :]
                    mapped.append(
                        FileEntry(
                            name=entry.name,
                            path=mapped_path,
                            is_dir=entry.is_dir,
                            size_bytes=entry.size_bytes,
                            modified=entry.modified,
                        )
                    )
                entries = mapped
        entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return entries

    def _list_remote_dir_find(self, remote_path: str, device: str) -> list[FileEntry]:
        escaped = shlex.quote(remote_path)
        all_items = self.run(
            ["shell", f"find {escaped} -mindepth 1 -maxdepth 1 -print"],
            device=device,
            timeout=25,
            check=False,
        )
        if all_items.returncode != 0 and not all_items.stdout.strip():
            stderr = (all_items.stderr or all_items.stdout).strip()
            raise AdbError(stderr or f"Not a directory or not accessible: {remote_path}")

        dir_items = self.run(
            ["shell", f"find {escaped} -mindepth 1 -maxdepth 1 -type d -print"],
            device=device,
            timeout=25,
            check=False,
        )
        if dir_items.returncode != 0 and not dir_items.stdout.strip():
            stderr = (dir_items.stderr or dir_items.stdout).strip()
            raise AdbError(stderr or f"Unable to list {remote_path}")

        dir_set = {
            self._normalize_remote_path(line.strip())
            for line in dir_items.stdout.splitlines()
            if line.strip()
        }

        entries: list[FileEntry] = []
        for raw in all_items.stdout.splitlines():
            full_path = self._normalize_remote_path(raw.strip())
            if not full_path or full_path == remote_path:
                continue
            name = posixpath.basename(full_path.rstrip("/"))
            if not name:
                continue

            entries.append(
                FileEntry(
                    name=name,
                    path=full_path,
                    is_dir=full_path in dir_set,
                    size_bytes=None,
                    modified=None,
                )
            )

        return entries

    def _list_remote_dir_ls(self, remote_path: str, device: str) -> list[FileEntry]:
        escaped = shlex.quote(remote_path)
        listed = self.run(
            ["shell", f"ls -1ApA {escaped}"],
            device=device,
            timeout=25,
            check=False,
        )
        if listed.returncode != 0:
            stderr = (listed.stderr or listed.stdout).strip()
            raise AdbError(stderr or f"Unable to list {remote_path}")

        entries: list[FileEntry] = []
        for raw in listed.stdout.splitlines():
            name = raw.strip()
            if not name or name in {".", ".."}:
                continue
            is_dir = name.endswith("/")
            if is_dir:
                name = name[:-1]
            if not name:
                continue
            full_path = self._normalize_remote_path(posixpath.join(remote_path, name))
            entries.append(
                FileEntry(
                    name=name,
                    path=full_path,
                    is_dir=is_dir,
                    size_bytes=None,
                    modified=None,
                )
            )
        return entries

    def _normalize_remote_path(self, path: str) -> str:
        clean = posixpath.normpath(path.strip() or "/")
        if not clean.startswith("/"):
            clean = "/" + clean
        return clean

    def _parse_ls_line(self, line: str, parent_path: str) -> FileEntry | None:
        parts = line.split(maxsplit=7)
        if len(parts) < 6:
            return None

        mode = parts[0]
        if not mode or mode[0] not in "-dlbcps":
            return None

        name = parts[-1].strip()
        if not name or name in {".", ".."}:
            return None
        if " -> " in name:
            name = name.split(" -> ", 1)[0]

        is_dir = mode.startswith("d")
        size_bytes = None
        try:
            size_bytes = int(parts[4])
        except (TypeError, ValueError):
            size_bytes = None

        modified = self._parse_modified(parts)
        full_path = posixpath.join(parent_path, name)
        if not full_path.startswith("/"):
            full_path = "/" + full_path

        return FileEntry(
            name=name,
            path=full_path,
            is_dir=is_dir,
            size_bytes=size_bytes,
            modified=modified,
        )

    def _parse_modified(self, parts: list[str]) -> datetime | None:
        if len(parts) >= 7 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[5]):
            stamp = f"{parts[5]} {parts[6]}"
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(stamp, fmt)
                except ValueError:
                    continue

        if len(parts) >= 8 and re.fullmatch(r"[A-Za-z]{3}", parts[5]):
            month = parts[5]
            day = parts[6]
            tail = parts[7]
            now = datetime.now()
            if ":" in tail:
                stamp = f"{month} {day} {now.year} {tail}"
                try:
                    return datetime.strptime(stamp, "%b %d %Y %H:%M")
                except ValueError:
                    return None
            stamp = f"{month} {day} {tail}"
            try:
                return datetime.strptime(stamp, "%b %d %Y")
            except ValueError:
                return None

        return None

    def push(self, local_path: str, remote_dir: str, device: str) -> None:
        self.transfer("upload", local_path, remote_dir, device=device)

    def pull(self, remote_path: str, local_dir: str, device: str) -> None:
        self.transfer("download", remote_path, local_dir, device=device)

    def make_dir(self, remote_path: str, device: str) -> None:
        escaped = shlex.quote(remote_path)
        self.run(["shell", f"mkdir -p {escaped}"], device=device, check=True)

    def delete_remote(self, remote_path: str, device: str) -> None:
        escaped = shlex.quote(remote_path)
        self.run(["shell", f"rm -rf {escaped}"], device=device, check=True)

    def move_remote(self, source_path: str, destination_path: str, device: str) -> None:
        src = shlex.quote(source_path)
        dst = shlex.quote(destination_path)
        self.run(["shell", f"mv {src} {dst}"], device=device, check=True)

    def transfer(
        self,
        mode: str,
        source: str,
        target: str,
        device: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if mode == "upload":
            args = ["push", "-p", source, target]
        elif mode == "download":
            args = ["pull", "-p", source, target]
        else:
            raise ValueError(f"Unsupported transfer mode: {mode}")

        total_bytes = self._estimate_transfer_size(mode, source, device)
        self._run_transfer(
            args,
            device=device,
            total_bytes=total_bytes,
            progress_callback=progress_callback,
            is_cancelled=is_cancelled,
            mode=mode,
            source=source,
            target=target,
        )

    def _run_transfer(
        self,
        args: list[str],
        device: str,
        total_bytes: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        mode: str | None = None,
        source: str | None = None,
        target: str | None = None,
    ) -> None:
        current_device = device
        last_details = ""
        max_attempts = 4
        for attempt in range(max_attempts):
            completed = self._run_transfer_attempt(
                args=args,
                device=current_device,
                total_bytes=total_bytes,
                progress_callback=progress_callback,
                is_cancelled=is_cancelled,
                mode=mode,
                source=source,
                target=target,
            )
            if completed.returncode == 0:
                return

            if self._is_benign_transfer_eof(completed):
                return

            combined = self._combined_output(completed)
            last_details = combined or f"exit code {completed.returncode}"

            if attempt < max_attempts - 1 and self._is_transient_transfer_failure(combined):
                self.run(["start-server"], check=False, timeout=20)
                if self._is_device_not_found(combined):
                    recovered = self._wait_for_device(original_serial=device, timeout_seconds=12)
                    if recovered:
                        current_device = recovered
                time.sleep(0.6 * (attempt + 1))
                continue
            break

        adb_path = self.resolve_adb_path()
        cmd = [str(adb_path), "-s", current_device, *args]
        raise AdbError(f"adb transfer failed: {' '.join(cmd)}\n{last_details}")

    def _run_transfer_attempt(
        self,
        args: list[str],
        device: str,
        total_bytes: int | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        is_cancelled: Callable[[], bool] | None,
        mode: str | None,
        source: str | None,
        target: str | None,
    ) -> subprocess.CompletedProcess[str]:
        adb_path = self.resolve_adb_path()
        cmd: list[str] = [str(adb_path), "-s", device, *args]
        process = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )

        output_parts: list[str] = []
        line_buffer = ""
        started_at = time.time()
        last_percent = -1
        last_inferred_bytes = 0
        last_output_at = started_at
        last_heartbeat_at = started_at

        self._emit_transfer_progress(
            progress_callback,
            stage="start",
            percent=0,
            total_bytes=total_bytes,
            transferred_bytes=0,
            started_at=started_at,
            idle_seconds=0.0,
        )

        while True:
            if is_cancelled and is_cancelled():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise AdbError("Transfer cancelled by user")

            now = time.time()
            if progress_callback and (now - last_heartbeat_at) >= 1.0:
                inferred_bytes = self._infer_transferred_bytes(
                    mode=mode,
                    source=source,
                    target=target,
                    device=device,
                )
                if isinstance(inferred_bytes, int) and inferred_bytes >= 0:
                    if total_bytes is not None:
                        inferred_bytes = min(inferred_bytes, total_bytes)
                    last_inferred_bytes = max(last_inferred_bytes, inferred_bytes)

                percent_from_output = last_percent if last_percent >= 0 else -1
                inferred_percent = percent_from_output if percent_from_output >= 0 else None
                progress_bytes: int | None = None
                if total_bytes is not None:
                    bytes_from_percent = (
                        int(total_bytes * (percent_from_output / 100.0)) if percent_from_output >= 0 else 0
                    )
                    progress_bytes = max(last_inferred_bytes, bytes_from_percent)
                    if progress_bytes > 0:
                        inferred_percent = max(
                            percent_from_output,
                            int((progress_bytes / max(total_bytes, 1)) * 100),
                        )
                elif last_inferred_bytes > 0:
                    progress_bytes = last_inferred_bytes

                idle_seconds = max(0.0, now - last_output_at)
                stage = "waiting" if idle_seconds >= 2.0 else "running"
                self._emit_transfer_progress(
                    progress_callback,
                    stage=stage,
                    percent=inferred_percent,
                    total_bytes=total_bytes,
                    transferred_bytes=progress_bytes,
                    started_at=started_at,
                    idle_seconds=idle_seconds,
                )
                last_heartbeat_at = now

            chunk = ""
            if process.stdout:
                ready, _, _ = select.select([process.stdout], [], [], 0.20)
                if ready:
                    chunk = process.stdout.read(1)
            if chunk == "":
                if process.poll() is not None:
                    break
                continue

            last_output_at = time.time()
            output_parts.append(chunk)
            if chunk in {"\r", "\n"}:
                last_percent = self._parse_progress_line(
                    line_buffer,
                    last_percent=last_percent,
                    total_bytes=total_bytes,
                    started_at=started_at,
                    progress_callback=progress_callback,
                    idle_seconds=0.0,
                )
                if total_bytes is not None and last_percent >= 0:
                    last_inferred_bytes = max(last_inferred_bytes, int(total_bytes * (last_percent / 100.0)))
                line_buffer = ""
            else:
                line_buffer += chunk

        if line_buffer:
            self._parse_progress_line(
                line_buffer,
                last_percent=last_percent,
                total_bytes=total_bytes,
                started_at=started_at,
                progress_callback=progress_callback,
                idle_seconds=0.0,
            )

        returncode = process.wait()
        output = "".join(output_parts)

        if returncode == 0:
            self._emit_transfer_progress(
                progress_callback,
                stage="finish",
                percent=100,
                total_bytes=total_bytes,
                transferred_bytes=total_bytes,
                started_at=started_at,
                idle_seconds=0.0,
            )

        return subprocess.CompletedProcess(cmd, returncode, output, "")

    def _infer_transferred_bytes(
        self,
        mode: str | None,
        source: str | None,
        target: str | None,
        device: str,
    ) -> int | None:
        if not mode or not source or not target:
            return None
        try:
            if mode == "download":
                source_name = posixpath.basename(source.rstrip("/"))
                if not source_name:
                    return None
                destination = Path(target).expanduser() / source_name
                if destination.exists() and destination.is_file():
                    return int(destination.stat().st_size)
                return None

            if mode == "upload":
                source_path = Path(source).expanduser()
                if not source_path.exists() or not source_path.is_file():
                    return None
                destination = posixpath.join(target.rstrip("/"), source_path.name)
                escaped_destination = shlex.quote(destination)
                completed = self.run(
                    ["shell", f"ls -ln {escaped_destination}"],
                    device=device,
                    timeout=8,
                    check=False,
                )
                if completed.returncode != 0:
                    return None
                for raw_line in completed.stdout.splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("total"):
                        continue
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    size_token = parts[4]
                    if size_token.isdigit():
                        return int(size_token)
                return None
        except Exception:
            return None
        return None

    def _parse_progress_line(
        self,
        line: str,
        last_percent: int,
        total_bytes: int | None,
        started_at: float,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        idle_seconds: float,
    ) -> int:
        percent_matches = re.findall(r"(\d{1,3})%", line)
        for matched in percent_matches:
            percent = int(matched)
            if percent <= last_percent:
                continue
            percent = max(0, min(100, percent))
            transferred_bytes = None
            if total_bytes is not None:
                transferred_bytes = int(total_bytes * (percent / 100.0))
            self._emit_transfer_progress(
                progress_callback,
                stage="running",
                percent=percent,
                total_bytes=total_bytes,
                transferred_bytes=transferred_bytes,
                started_at=started_at,
                idle_seconds=idle_seconds,
            )
            last_percent = percent
        return last_percent

    def _emit_transfer_progress(
        self,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        stage: str,
        percent: int | None,
        total_bytes: int | None,
        transferred_bytes: int | None,
        started_at: float,
        idle_seconds: float,
    ) -> None:
        if not progress_callback:
            return

        elapsed = max(0.001, time.time() - started_at)
        speed_bps: float | None = None
        eta_seconds: float | None = None
        if transferred_bytes is not None and transferred_bytes >= 0:
            speed_bps = transferred_bytes / elapsed
            if total_bytes is not None and speed_bps > 0:
                remaining = max(0, total_bytes - transferred_bytes)
                eta_seconds = remaining / speed_bps

        progress_callback(
            {
                "stage": stage,
                "percent": percent,
                "total_bytes": total_bytes,
                "transferred_bytes": transferred_bytes,
                "speed_bps": speed_bps,
                "eta_seconds": eta_seconds,
                "idle_seconds": idle_seconds,
            }
        )

    def _estimate_transfer_size(self, mode: str, source: str, device: str) -> int | None:
        if mode == "upload":
            path = Path(source)
            if not path.exists():
                return None
            if path.is_file():
                return int(path.stat().st_size)

            total = 0
            try:
                for root, _dirs, files in os.walk(path):
                    for filename in files:
                        file_path = Path(root) / filename
                        try:
                            total += int(file_path.stat().st_size)
                        except OSError:
                            continue
                return total
            except OSError:
                return None

        if mode == "download":
            escaped = shlex.quote(source)
            completed = self.run(
                ["shell", f"du -sk {escaped} 2>/dev/null | cut -f1"],
                device=device,
                check=False,
            )
            raw = (completed.stdout or "").strip().splitlines()
            if not raw:
                return None
            try:
                return int(raw[-1]) * 1024
            except ValueError:
                return None

        return None

    def _combined_output(self, completed: subprocess.CompletedProcess[str]) -> str:
        parts = [completed.stderr.strip(), completed.stdout.strip()]
        return "\n".join(part for part in parts if part).strip()

    def _is_benign_transfer_eof(self, completed: subprocess.CompletedProcess[str]) -> bool:
        combined = self._combined_output(completed).lower()
        if "failed to read copy response: eof" not in combined:
            return False
        success_markers = (
            "file pushed",
            "files pushed",
            "file pulled",
            "files pulled",
            "bytes in",
        )
        return any(marker in combined for marker in success_markers)

    def _is_transient_transfer_failure(self, combined_output: str) -> bool:
        text = combined_output.lower()
        transient_markers = (
            "device offline",
            "device not found",
            "cannot connect to daemon",
            "failed to check server version",
            "connection reset",
            "transport error",
            "protocol fault",
            "failed to read copy response: eof",
        )
        return any(marker in text for marker in transient_markers)

    def _is_device_not_found(self, combined_output: str) -> bool:
        return "device" in combined_output.lower() and "not found" in combined_output.lower()

    def _wait_for_device(self, original_serial: str, timeout_seconds: int = 12) -> str | None:
        deadline = time.time() + timeout_seconds
        fallback_device: str | None = None

        while time.time() < deadline:
            completed = self.run(["devices"], check=False, timeout=20)
            if completed.returncode == 0:
                connected: list[str] = []
                for raw in completed.stdout.splitlines()[1:]:
                    line = raw.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "device":
                        connected.append(parts[0])

                if original_serial in connected:
                    return original_serial
                if len(connected) == 1:
                    fallback_device = connected[0]
                    break

            time.sleep(1.0)

        return fallback_device
