from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, QThreadPool, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressDialog,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .adb_service import AdbService
from .file_explorer import BrowserPane as ExplorerPane
from .models import AdbDevice, FileEntry
from .workers import ProgressWorker, Worker

ANDROID_USER_ROOT = "/storage/emulated/0"


def format_size(value: int | None) -> str:
    if value is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("mac-drop")
        self.resize(1320, 820)

        self.project_root = Path(__file__).resolve().parents[2]
        self.adb = AdbService(self.project_root)
        self.settings = QSettings("mac-drop", "mac-drop")

        self.thread_pool = QThreadPool.globalInstance()
        self.active_jobs = 0
        self._active_workers: set[object] = set()

        default_local_root = Path.home()
        if not default_local_root.exists() or not default_local_root.is_dir():
            default_local_root = Path("/")
        self.local_path = default_local_root
        self.remote_path = ANDROID_USER_ROOT
        self.device_id: str | None = None
        self.devices: list[AdbDevice] = []
        stored_view = str(self.settings.value("ui/view_mode", "list"))
        self.current_view_mode = stored_view if stored_view in {"icon", "list", "column", "gallery"} else "list"
        stored_icon_size = str(self.settings.value("ui/icon_size", "medium"))
        self.current_icon_size = stored_icon_size if stored_icon_size in {"small", "medium", "large"} else "medium"
        self.show_hidden_files = str(self.settings.value("ui/show_hidden", "true")).lower() in {"1", "true", "yes", "on"}
        self._child_windows: list[MainWindow] = []
        self._copy_buffer_paths: list[str] = []
        self._copy_buffer_source: str | None = None
        self._transfer_context: dict | None = None
        self._pending_transfers: list[dict] = []
        self._transfer_worker: ProgressWorker | None = None
        self._transfer_cancel_requested = False
        self._transfer_dialog: QProgressDialog | None = None
        self._transfer_progress_bar: QProgressBar | None = None
        self._remote_refresh_inflight = False
        self._remote_refresh_pending = False
        self._remote_disconnect_recovering = False
        self._last_remote_error_signature: str = ""
        self._last_remote_error_time = 0.0
        self._last_logged_device_id: str | None = None
        self._menu_pane_override: ExplorerPane | None = None

        self._build_ui()
        self._apply_styles()
        self._reset_log()
        self.local_pane.set_icon_size(self.current_icon_size)
        self.remote_pane.set_icon_size(self.current_icon_size)
        self._set_view_mode(self.current_view_mode)

        self._refresh_local()
        self._bootstrap_adb()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        controls = QFormLayout()

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(300)
        self.device_refresh_button = QPushButton("Reload Devices")
        self.device_info = QLabel("ADB: resolving...")
        self.refresh_all_button = QPushButton("Refresh All")
        self.refresh_all_button.setFixedWidth(110)

        self.view_group = QButtonGroup(self)
        self.view_icon_button = QToolButton()
        self.view_list_button = QToolButton()
        self.view_column_button = QToolButton()
        self.view_gallery_button = QToolButton()
        self.more_button = QToolButton()

        self.view_icon_button.setText("▦")
        self.view_list_button.setText("☰")
        self.view_column_button.setText("▥")
        self.view_gallery_button.setText("▤")
        self.more_button.setText("⋯")

        for idx, button in enumerate(
            [self.view_icon_button, self.view_list_button, self.view_column_button, self.view_gallery_button]
        ):
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setFixedSize(36, 30)
            self.view_group.addButton(button, idx)
        self.view_list_button.setChecked(True)
        self.more_button.setFixedSize(36, 30)

        device_row.addWidget(self.device_combo)
        device_row.addWidget(self.device_refresh_button)
        device_row.addWidget(self.refresh_all_button)
        device_row.addWidget(self.device_info, 1)
        device_row.addWidget(self.view_icon_button)
        device_row.addWidget(self.view_list_button)
        device_row.addWidget(self.view_column_button)
        device_row.addWidget(self.view_gallery_button)
        device_row.addWidget(self.more_button)

        controls.addRow("Android Device", device_row)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.local_pane = ExplorerPane("Mac", "local")
        self.remote_pane = ExplorerPane("Android", "remote")
        splitter.addWidget(self.local_pane)
        splitter.addWidget(self.remote_pane)
        splitter.setSizes([640, 640])
        layout.addWidget(splitter, 1)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Activity log")
        self.log_output.setFixedHeight(140)
        layout.addWidget(self.log_output)

        log_actions = QHBoxLayout()
        log_actions.addStretch(1)
        self.copy_log_button = QPushButton("Copy Log")
        self.copy_log_button.setFixedWidth(86)
        self.copy_log_button.setFixedHeight(28)
        log_actions.addWidget(self.copy_log_button)
        layout.addLayout(log_actions)

        status = QStatusBar()
        self.setStatusBar(status)

        self.setCentralWidget(root)

        self.device_refresh_button.clicked.connect(self._refresh_devices)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)

        self.local_pane.navigate_requested.connect(self._on_local_navigate)
        self.local_pane.refresh_requested.connect(self._refresh_local)

        self.remote_pane.navigate_requested.connect(self._on_remote_navigate)
        self.remote_pane.refresh_requested.connect(self._refresh_remote)
        self.local_pane.transfer_dropped.connect(self._on_transfer_dropped)
        self.remote_pane.transfer_dropped.connect(self._on_transfer_dropped)
        self.local_pane.context_menu_requested.connect(
            lambda global_pos, pane=self.local_pane: self._show_more_menu_for_pane(
                pane, global_pos, include_view_options=False
            )
        )
        self.remote_pane.context_menu_requested.connect(
            lambda global_pos, pane=self.remote_pane: self._show_more_menu_for_pane(
                pane, global_pos, include_view_options=False
            )
        )

        self.refresh_all_button.clicked.connect(self._refresh_all)
        self.copy_log_button.clicked.connect(self._copy_log)
        self.more_button.clicked.connect(self._show_more_menu)
        self.view_icon_button.clicked.connect(lambda: self._set_view_mode("icon"))
        self.view_list_button.clicked.connect(lambda: self._set_view_mode("list"))
        self.view_column_button.clicked.connect(lambda: self._set_view_mode("column"))
        self.view_gallery_button.clicked.connect(lambda: self._set_view_mode("gallery"))

        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self.paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.copy_shortcut.activated.connect(self._copy_selection)
        self.paste_shortcut.activated.connect(self._paste_buffered_items)

        self.view_shortcuts = [
            QShortcut(QKeySequence("Meta+1"), self),
            QShortcut(QKeySequence("Meta+2"), self),
            QShortcut(QKeySequence("Meta+3"), self),
            QShortcut(QKeySequence("Meta+4"), self),
        ]
        self.view_shortcuts[0].activated.connect(lambda: self._set_view_mode("icon"))
        self.view_shortcuts[1].activated.connect(lambda: self._set_view_mode("list"))
        self.view_shortcuts[2].activated.connect(lambda: self._set_view_mode("column"))
        self.view_shortcuts[3].activated.connect(lambda: self._set_view_mode("gallery"))

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #141722;
            }
            QWidget {
                color: #e9ecf4;
                font-size: 13px;
            }
            QLabel#paneTitle {
                font-size: 16px;
                font-weight: 600;
                color: #f4f7ff;
            }
            QLineEdit, QComboBox, QTableWidget, QListWidget, QScrollArea, QPlainTextEdit {
                background-color: #1b2030;
                border: 1px solid #2a334d;
                border-radius: 8px;
                padding: 6px;
                color: #f4f7ff;
                selection-background-color: #3a6ef5;
            }
            QPushButton {
                background-color: #253252;
                border: 1px solid #2f4570;
                border-radius: 8px;
                padding: 8px 12px;
                color: #eff4ff;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2d3d66;
            }
            QPushButton:disabled {
                background-color: #2b2f3d;
                color: #9aa3ba;
                border-color: #3a3f51;
            }
            QHeaderView::section {
                background-color: #20283c;
                color: #cfd8f1;
                border: none;
                border-right: 1px solid #2f3a56;
                padding: 6px;
            }
            QTableWidget {
                gridline-color: #2b3450;
            }
            QListWidget::item:selected {
                background-color: #2f5ed8;
                border-radius: 6px;
            }
            QStatusBar {
                background: #131826;
                color: #cfd8f1;
            }
            """
        )

    def _bootstrap_adb(self) -> None:
        self._run_task(
            self.adb.ensure_ready,
            on_result=self._on_adb_ready,
            on_error=self._on_adb_error,
        )

    def _on_adb_ready(self, adb_path: str) -> None:
        self.device_info.setText(f"ADB: {self.adb.adb_source} ({adb_path})")
        self._log(f"ADB ready from {self.adb.adb_source}: {adb_path}")
        self._refresh_devices()

    def _on_adb_error(self, traceback_text: str) -> None:
        self.device_info.setText("ADB: unavailable")
        self._log("Failed to initialize ADB")
        self._log(traceback_text)
        QMessageBox.critical(
            self,
            "ADB Error",
            "ADB could not be initialized.\n"
            "Set MAC_DROP_ADB_PATH or run scripts/fetch_adb.py and restart.",
        )

    def _refresh_all(self) -> None:
        self._refresh_local()
        self._refresh_devices()
        self._refresh_remote()

    def _set_view_mode(self, mode: str) -> None:
        if mode not in {"icon", "list", "column", "gallery"}:
            mode = "list"
        self.current_view_mode = mode
        self.settings.setValue("ui/view_mode", mode)
        self.local_pane.apply_view_mode(mode)
        self.remote_pane.apply_view_mode(mode)
        button_for_mode = {
            "icon": self.view_icon_button,
            "list": self.view_list_button,
            "column": self.view_column_button,
            "gallery": self.view_gallery_button,
        }
        target_button = button_for_mode.get(mode)
        if target_button and not target_button.isChecked():
            target_button.setChecked(True)

    def _set_icon_size(self, icon_size: str) -> None:
        if icon_size not in {"small", "medium", "large"}:
            return
        self.current_icon_size = icon_size
        self.settings.setValue("ui/icon_size", icon_size)
        self.local_pane.set_icon_size(icon_size)
        self.remote_pane.set_icon_size(icon_size)

    def _set_show_hidden_files(self, enabled: bool) -> None:
        self.show_hidden_files = bool(enabled)
        self.settings.setValue("ui/show_hidden", "true" if self.show_hidden_files else "false")
        self._refresh_local()
        self._refresh_remote()

    def _refresh_local(self) -> None:
        if not self.local_path.exists():
            fallback = Path.home()
            if not fallback.exists() or not fallback.is_dir():
                fallback = Path("/")
            self.local_path = fallback

        entries: list[FileEntry] = []
        try:
            with os.scandir(self.local_path) as iterator:
                for item in iterator:
                    try:
                        stat = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append(
                        FileEntry(
                            name=item.name,
                            path=str(Path(item.path)),
                            is_dir=item.is_dir(follow_symlinks=False),
                            size_bytes=None if item.is_dir(follow_symlinks=False) else int(stat.st_size),
                            modified=datetime.fromtimestamp(stat.st_mtime),
                        )
                    )
        except OSError as exc:
            QMessageBox.warning(self, "Local Path Error", str(exc))
            return

        if not self.show_hidden_files:
            entries = [entry for entry in entries if not entry.name.startswith(".")]
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        parent = None if self.local_path.parent == self.local_path else str(self.local_path.parent)
        self.local_pane.set_path(str(self.local_path), parent)
        self.local_pane.set_entries(entries, parent)

    def _refresh_devices(self) -> None:
        self._run_task(
            self.adb.list_devices,
            on_result=self._on_devices_loaded,
            on_error=lambda trace: self._log(f"Device refresh failed\n{trace}"),
        )

    def _on_devices_loaded(self, devices: list[AdbDevice]) -> None:
        self._remote_disconnect_recovering = False
        previous = self.device_id
        self.devices = devices

        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        if not devices:
            self.device_combo.addItem("No devices detected", None)
            self.device_id = None
            self._last_logged_device_id = None
            self.remote_pane.set_path(self.remote_path, self._parent_remote_path(self.remote_path))
            self.remote_pane.set_entries([], self._parent_remote_path(self.remote_path))
            self.device_combo.blockSignals(False)
            self._log("No Android devices detected")
            return

        active_index = 0
        for idx, device in enumerate(devices):
            label = self._device_label(device)
            self.device_combo.addItem(label, device.serial)
            if previous and device.serial == previous:
                active_index = idx

        self.device_combo.setCurrentIndex(active_index)
        self.device_combo.blockSignals(False)

        self.device_id = self.device_combo.currentData()
        if self.device_id and self.device_id != self._last_logged_device_id:
            active_device = next((item for item in self.devices if item.serial == self.device_id), None)
            if active_device and active_device.display_name:
                self._log(f"Using device: {active_device.display_name} ({active_device.serial})")
            else:
                self._log(f"Using device: {self.device_id}")
            self._last_logged_device_id = self.device_id
        self._refresh_remote()

    def _on_device_changed(self) -> None:
        self.device_id = self.device_combo.currentData()
        self._refresh_remote()

    def _device_label(self, device: AdbDevice) -> str:
        name_part = f"{device.display_name} ({device.serial})" if device.display_name else device.serial
        base = f"{name_part} [{device.status}]"
        extra = self._device_extra_details(device.details)
        if extra:
            return f"{base} {extra}"
        return base

    def _device_extra_details(self, details: str) -> str:
        if not details:
            return ""
        skip_prefixes = ("product:", "model:", "device:", "manufacturer:", "marketname:")
        kept = [token for token in details.split() if not token.lower().startswith(skip_prefixes)]
        return " ".join(kept)

    def _on_local_navigate(self, new_path: str) -> None:
        if not new_path:
            return
        target = Path(new_path).expanduser()
        if target.is_file():
            target = target.parent
        if not target.exists() or not target.is_dir():
            QMessageBox.warning(self, "Invalid Path", f"Not a directory: {new_path}")
            return

        self.local_path = target.resolve()
        self._refresh_local()

    def _on_remote_navigate(self, new_path: str) -> None:
        if not new_path:
            return
        self.remote_path = self._normalize_remote_path(new_path)
        self._refresh_remote()

    def _refresh_remote(self) -> None:
        self.remote_path = self._normalize_remote_path(self.remote_path)
        self.remote_pane.set_path(self.remote_path, self._parent_remote_path(self.remote_path))

        if not self.device_id:
            self.remote_pane.set_entries([], self._parent_remote_path(self.remote_path))
            return

        if self._remote_refresh_inflight:
            self._remote_refresh_pending = True
            return

        current_path = self.remote_path
        current_device = self.device_id
        self._remote_refresh_inflight = True

        self._run_task(
            lambda: self.adb.list_remote_dir(current_path, current_device),
            on_result=lambda entries: self._on_remote_loaded(current_path, current_device, entries),
            on_error=lambda trace: self._on_remote_error(current_device, trace),
        )

    def _on_remote_loaded(self, path: str, device: str, entries: list[FileEntry]) -> None:
        self._finish_remote_refresh_cycle()
        if device != self.device_id:
            return
        if path != self.remote_path:
            return
        if not self.show_hidden_files:
            entries = [entry for entry in entries if not entry.name.startswith(".")]
        parent = self._parent_remote_path(path)
        self.remote_pane.set_path(path, parent)
        self.remote_pane.set_entries(entries, parent)

    def _on_remote_error(self, device: str, traceback_text: str) -> None:
        self._finish_remote_refresh_cycle()

        lower_trace = traceback_text.lower()
        device_not_found = (
            "device '" in lower_trace and "not found" in lower_trace
        ) or "no devices/emulators found" in lower_trace

        if device_not_found:
            if device == self.device_id:
                self.device_id = None
                self.remote_pane.set_entries([], self._parent_remote_path(self.remote_path))
            if not self._remote_disconnect_recovering:
                self._remote_disconnect_recovering = True
                self._log(f"Device disconnected: {device}")
                self._refresh_devices()
            return

        missing_path = "no such file or directory" in lower_trace and "find:" in lower_trace
        if missing_path:
            parent = self._parent_remote_path(self.remote_path)
            if parent and parent != self.remote_path:
                self.remote_path = parent
                self._log(f"Remote path unavailable. Moved up to: {parent}")
                self._refresh_remote()
                return

        signature = traceback_text.strip()
        now = time.time()
        if signature == self._last_remote_error_signature and (now - self._last_remote_error_time) < 1.5:
            return
        self._last_remote_error_signature = signature
        self._last_remote_error_time = now
        self._log("Remote listing failed")
        self._log(traceback_text)

    def _finish_remote_refresh_cycle(self) -> None:
        self._remote_refresh_inflight = False
        if self._remote_refresh_pending:
            self._remote_refresh_pending = False
            self._refresh_remote()

    def _show_more_menu(self) -> None:
        pane = self._active_pane()
        anchor = self.more_button.mapToGlobal(self.more_button.rect().bottomLeft())
        self._show_more_menu_for_pane(pane, anchor, include_view_options=True)

    def _show_more_menu_for_pane(self, pane: ExplorerPane, global_pos: QPoint, include_view_options: bool) -> None:
        entries = pane.selected_entries()
        menu = QMenu(self)

        if include_view_options:
            view_menu = menu.addMenu("View Options")
            view_actions: list[tuple[str, str]] = [
                ("icon", "Icon View"),
                ("list", "List View"),
                ("column", "Column View"),
                ("gallery", "Gallery View"),
            ]
            for mode, label in view_actions:
                action = QAction(label, self)
                action.setCheckable(True)
                action.setChecked(self.current_view_mode == mode)
                action.triggered.connect(lambda checked=False, m=mode: self._set_view_mode(m))
                view_menu.addAction(action)

            icon_size_menu = view_menu.addMenu("Icon Size")
            for icon_size, label in [("small", "Small"), ("medium", "Medium"), ("large", "Large")]:
                action = QAction(label, self)
                action.setCheckable(True)
                action.setChecked(self.current_icon_size == icon_size)
                action.triggered.connect(lambda checked=False, s=icon_size: self._set_icon_size(s))
                icon_size_menu.addAction(action)

            show_hidden_action = QAction("Show Hidden Files", self)
            show_hidden_action.setCheckable(True)
            show_hidden_action.setChecked(self.show_hidden_files)
            show_hidden_action.toggled.connect(self._set_show_hidden_files)
            view_menu.addAction(show_hidden_action)

            menu.addSeparator()

        if entries:
            count = len(entries)
            new_folder_with_selection = QAction(f"New Folder with Selection ({count} Items)", self)
            new_folder_with_selection.triggered.connect(self._new_folder_with_selection)
            menu.addAction(new_folder_with_selection)
        else:
            new_folder_action = QAction("New Folder", self)
            new_folder_action.triggered.connect(self._new_folder_empty)
            menu.addAction(new_folder_action)

        open_new_tab = QAction("Open in New Tab", self)
        open_new_tab.triggered.connect(self._open_in_new_tab)
        menu.addAction(open_new_tab)

        if entries:
            move_to_trash = QAction("Move to Trash", self)
            move_to_trash.triggered.connect(self._move_selection_to_trash)
            menu.addAction(move_to_trash)

        get_info = QAction("Get Info", self)
        get_info.triggered.connect(self._show_info)
        menu.addAction(get_info)

        if entries:
            rename_action = QAction("Rename", self)
            rename_action.triggered.connect(self._rename_selection)
            menu.addAction(rename_action)

            compress_menu = menu.addMenu("Compress")
            compress_zip = QAction(".zip", self)
            compress_zip.triggered.connect(lambda: self._compress_selection(".zip"))
            compress_cbz = QAction(".cbz", self)
            compress_cbz.triggered.connect(lambda: self._compress_selection(".cbz"))
            compress_menu.addAction(compress_zip)
            compress_menu.addAction(compress_cbz)

        quick_look = QAction("Quick Look", self)
        quick_look.triggered.connect(self._quick_look_selection)
        menu.addAction(quick_look)

        if entries:
            copy_action = QAction("Copy", self)
            copy_action.triggered.connect(self._copy_selection)
            menu.addAction(copy_action)

        self._menu_pane_override = pane
        try:
            menu.exec(global_pos)
        finally:
            self._menu_pane_override = None

    def _new_folder_empty(self) -> None:
        pane = self._active_pane()
        self._create_folder_in_pane(pane, move_entries=[])

    def _new_folder_with_selection(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if not entries:
            return
        self._create_folder_in_pane(pane, move_entries=entries)

    def _create_folder_in_pane(self, pane: ExplorerPane, move_entries: list[FileEntry]) -> None:
        default_name = self._next_folder_name(pane)

        if pane.kind == "local":
            base = self.local_path
            new_dir = base / default_name
            try:
                new_dir.mkdir(parents=False, exist_ok=False)
                folder_name, ok = QInputDialog.getText(self, "Rename Folder", "Folder name", text=default_name)
                if ok:
                    folder_name = folder_name.strip() or default_name
                    renamed = base / folder_name
                    if renamed != new_dir:
                        new_dir.rename(renamed)
                        new_dir = renamed
                for entry in move_entries:
                    shutil.move(entry.path, str(new_dir / entry.name))
            except Exception as exc:
                QMessageBox.warning(self, "Operation Failed", str(exc))
                return
            self._refresh_local()
            return

        if not self.device_id:
            QMessageBox.information(self, "No Device", "Connect an Android device first.")
            return
        folder_name, ok = QInputDialog.getText(self, "Rename Folder", "Folder name", text=default_name)
        if ok:
            folder_name = folder_name.strip() or default_name
        else:
            folder_name = default_name
        target_dir = self._normalize_remote_path(posixpath.join(self.remote_path, folder_name))
        device = self.device_id

        def task() -> None:
            self.adb.make_dir(target_dir, device)
            for entry in move_entries:
                dest = self._normalize_remote_path(posixpath.join(target_dir, entry.name))
                self.adb.move_remote(entry.path, dest, device)

        self._run_task(task, on_result=lambda _r: self._refresh_remote(), on_error=self._on_transfer_error)

    def _next_folder_name(self, pane: ExplorerPane) -> str:
        base_name = "New Folder"
        existing = {entry.name for entry in pane.entries}
        if base_name not in existing:
            return base_name
        index = 2
        while True:
            candidate = f"{base_name} {index}"
            if candidate not in existing:
                return candidate
            index += 1

    def _open_in_new_tab(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        target_path = pane.path_edit.text().strip()
        if entries and entries[0].is_dir:
            target_path = entries[0].path

        child = MainWindow()
        child.show()
        self._child_windows.append(child)

        if pane.kind == "local":
            child.local_path = Path(target_path).expanduser()
            child._refresh_local()
        else:
            child.remote_path = self._normalize_remote_path(target_path)
            child._refresh_remote()

    def _move_selection_to_trash(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if not entries:
            return

        if pane.kind == "local":
            failures: list[str] = []
            for entry in entries:
                path = entry.path.replace('"', '\\"')
                script = f'tell application "Finder" to delete POSIX file "{path}"'
                result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
                if result.returncode != 0:
                    failures.append(entry.name)
            if failures:
                QMessageBox.warning(self, "Move to Trash", "Some items could not be moved to Trash.")
            self._refresh_local()
            return

        if not self.device_id:
            return
        device = self.device_id

        def task() -> None:
            for entry in entries:
                self.adb.delete_remote(entry.path, device)

        self._run_task(task, on_result=lambda _r: self._refresh_remote(), on_error=self._on_transfer_error)

    def _show_info(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if not entries:
            QMessageBox.information(
                self,
                "Info",
                f"Location: {pane.path_edit.text().strip()}\nSelected: 0",
            )
            return

        total_size = sum(entry.size_bytes or 0 for entry in entries)
        names = "\n".join(f"- {entry.name}" for entry in entries[:8])
        if len(entries) > 8:
            names += f"\n... and {len(entries) - 8} more"
        QMessageBox.information(
            self,
            "Info",
            f"Selected: {len(entries)} item(s)\n"
            f"Approx Size: {format_size(total_size)}\n\n{names}",
        )

    def _rename_selection(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if len(entries) != 1:
            QMessageBox.information(self, "Rename", "Select exactly one item.")
            return
        entry = entries[0]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name", text=entry.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == entry.name:
            return

        if pane.kind == "local":
            src = Path(entry.path)
            dest = src.parent / new_name
            try:
                src.rename(dest)
            except Exception as exc:
                QMessageBox.warning(self, "Rename Failed", str(exc))
                return
            self._refresh_local()
            return

        if not self.device_id:
            return
        device = self.device_id
        parent = posixpath.dirname(entry.path.rstrip("/"))
        dest = self._normalize_remote_path(posixpath.join(parent, new_name))
        self._run_task(
            lambda: self.adb.move_remote(entry.path, dest, device),
            on_result=lambda _r: self._refresh_remote(),
            on_error=self._on_transfer_error,
        )

    def _compress_selection(self, extension: str) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if not entries:
            return
        if pane.kind != "local":
            QMessageBox.information(self, "Not Supported", "Compression is currently available for Mac files only.")
            return

        archive_name, ok = QInputDialog.getText(self, "Compress", "Archive name", text=f"Archive{extension}")
        if not ok:
            return
        archive_name = archive_name.strip()
        if not archive_name:
            return
        if not archive_name.endswith(extension):
            archive_name += extension

        target = self.local_path / archive_name
        try:
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for entry in entries:
                    path = Path(entry.path)
                    if path.is_file():
                        zf.write(path, arcname=path.name)
                    elif path.is_dir():
                        for file_path in path.rglob("*"):
                            if file_path.is_file():
                                zf.write(file_path, arcname=str(Path(path.name) / file_path.relative_to(path)))
        except Exception as exc:
            QMessageBox.warning(self, "Compress Failed", str(exc))
            return
        self._refresh_local()

    def _quick_look_selection(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if pane.kind != "local":
            QMessageBox.information(self, "Not Supported", "Quick Look is available for Mac files only.")
            return

        if entries:
            paths = [entry.path for entry in entries[:10]]
        else:
            paths = [str(self.local_path)]
        subprocess.Popen(["qlmanage", "-p", *paths], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _upload_selected(self) -> None:
        entries = self.local_pane.selected_entries()
        if not entries:
            QMessageBox.information(self, "Nothing Selected", "Select local files or folders to upload.")
            return

        self._upload_paths([entry.path for entry in entries], self.remote_path, "Uploaded")

    def _download_selected(self) -> None:
        entries = self.remote_pane.selected_entries()
        if not entries:
            QMessageBox.information(self, "Nothing Selected", "Select Android files or folders to download.")
            return

        self._download_paths([entry.path for entry in entries], str(self.local_path), "Downloaded")

    def _on_transfer_dropped(self, source_kind: str, paths: list[str], target_path: str) -> None:
        if not paths:
            return

        if source_kind in {"local", "external"}:
            destination = self._normalize_remote_path(target_path or self.remote_path)
            self._upload_paths(paths, destination, "Dragged")
            return

        if source_kind == "remote":
            destination = Path(target_path or str(self.local_path)).expanduser()
            if not destination.exists() or not destination.is_dir():
                destination = self.local_path
            self._download_paths(paths, str(destination), "Dragged")

    def _active_pane(self) -> ExplorerPane:
        if self._menu_pane_override is not None:
            return self._menu_pane_override
        focused = QApplication.focusWidget()
        if focused and self.remote_pane.isAncestorOf(focused):
            return self.remote_pane
        if focused and self.local_pane.isAncestorOf(focused):
            return self.local_pane
        if self.local_pane.selected_entries():
            return self.local_pane
        if self.remote_pane.selected_entries():
            return self.remote_pane
        return self.local_pane

    def _copy_selection(self) -> None:
        pane = self._active_pane()
        entries = pane.selected_entries()
        if not entries:
            return
        self._copy_buffer_paths = [entry.path for entry in entries]
        self._copy_buffer_source = pane.kind
        self.statusBar().showMessage(f"Copied {len(entries)} item(s)", 2000)

    def _paste_buffered_items(self) -> None:
        if not self._copy_buffer_paths or not self._copy_buffer_source:
            QMessageBox.information(self, "Clipboard Empty", "Copy files or folders first.")
            return
        if self._copy_buffer_source == "local":
            self._upload_paths(list(self._copy_buffer_paths), self.remote_path, "Pasted")
            return
        if self._copy_buffer_source == "remote":
            self._download_paths(list(self._copy_buffer_paths), str(self.local_path), "Pasted")

    def _upload_paths(self, local_paths: list[str], remote_target: str, verb: str) -> None:
        self._start_transfer("upload", local_paths, remote_target, verb)

    def _download_paths(self, remote_paths: list[str], local_target: str, verb: str) -> None:
        self._start_transfer("download", remote_paths, local_target, verb)

    def _start_transfer(self, mode: str, paths: list[str], target: str, verb: str) -> None:
        if not self.device_id:
            QMessageBox.information(self, "No Device", "Connect an Android device first.")
            return
        if not paths:
            return

        request = {
            "mode": mode,
            "paths": list(paths),
            "target": target,
            "verb": verb,
            "device": self.device_id,
            "total": len(paths),
            "completed": 0,
            "current_name": "",
            "item_percent": None,
            "item_total_bytes": None,
            "item_done_bytes": None,
            "item_speed_bps": None,
            "item_eta_seconds": None,
            "item_stage": "start",
            "item_idle_seconds": 0.0,
            "item_started_at": None,
            "last_auto_refresh_time": 0.0,
        }
        if self._transfer_context is not None:
            self._pending_transfers.append(request)
            self.statusBar().showMessage(
                f"Queued {len(paths)} item(s). Queue length: {len(self._pending_transfers)}",
                2500,
            )
            self._update_transfer_popup()
            return

        self._transfer_context = request
        self._transfer_cancel_requested = False
        self._show_transfer_popup()
        self._run_next_transfer_item()

    def _show_transfer_popup(self) -> None:
        if not self._transfer_context:
            return

        if not self._transfer_dialog:
            dialog = QProgressDialog("Preparing transfer...", "Cancel", 0, 100, self)
            dialog.setWindowTitle("Copying Files")
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            dialog.setMinimumDuration(0)
            dialog.canceled.connect(self._request_transfer_cancel)
            dialog.show()
            self._transfer_dialog = dialog
            self._transfer_progress_bar = dialog.findChild(QProgressBar)
            if self._transfer_progress_bar:
                self._transfer_progress_bar.setTextVisible(True)
                self._transfer_progress_bar.setFormat("%p%")
        else:
            self._transfer_dialog.show()

        self._update_transfer_popup()

    def _update_transfer_popup(self) -> None:
        if not self._transfer_context or not self._transfer_dialog:
            return
        completed = int(self._transfer_context["completed"])
        total = int(self._transfer_context["total"])
        paths: list[str] = self._transfer_context["paths"]  # type: ignore[assignment]

        if completed < total:
            source = paths[completed]
            mode = str(self._transfer_context["mode"])
            name = Path(source).name if mode == "upload" else posixpath.basename(source.rstrip("/"))
            if not name:
                name = source
            self._transfer_context["current_name"] = name
        else:
            name = "Finalizing"

        item_total = self._transfer_context.get("item_total_bytes")
        item_done = self._transfer_context.get("item_done_bytes")
        speed_bps = self._transfer_context.get("item_speed_bps")
        eta_seconds = self._transfer_context.get("item_eta_seconds")
        item_percent = self._transfer_context.get("item_percent")
        item_stage = str(self._transfer_context.get("item_stage") or "running")
        idle_seconds = float(self._transfer_context.get("item_idle_seconds") or 0.0)
        started_at = self._transfer_context.get("item_started_at")

        lines = [
            f"Copying {name}",
            f"{completed + 1 if completed < total else total} of {total}",
        ]
        if isinstance(item_total, int) and item_total > 0:
            if isinstance(item_done, int) and item_done > 0:
                done = max(0, min(int(item_done), item_total))
                lines.append(f"{format_size(done)} / {format_size(item_total)}")
            else:
                lines.append(f"-- / {format_size(item_total)}")
        if isinstance(item_percent, int):
            lines.append(f"Progress: {max(0, min(item_percent, 100))}%")
        if isinstance(speed_bps, (int, float)) and speed_bps > 0:
            speed_text = f"{format_size(int(speed_bps))}/s"
            if isinstance(eta_seconds, (int, float)) and eta_seconds >= 0:
                lines.append(f"{speed_text}, ETA {self._format_eta(float(eta_seconds))}")
            else:
                lines.append(speed_text)
        if isinstance(started_at, (int, float)) and started_at > 0:
            elapsed = max(0.0, time.time() - float(started_at))
            lines.append(f"Elapsed: {self._format_eta(elapsed)}")
        if item_stage == "waiting" and idle_seconds >= 2.0:
            lines.append(f"Still transferring... waiting for device progress output ({int(idle_seconds)}s)")
        if self._pending_transfers:
            lines.append(f"Queued: {len(self._pending_transfers)}")
        if self._transfer_cancel_requested:
            lines.append("Cancelling after current item...")

        self._transfer_dialog.setLabelText("\n".join(lines))
        if isinstance(item_percent, int):
            self._transfer_dialog.setRange(0, 100)
            self._transfer_dialog.setValue(max(0, min(item_percent, 100)))
            if self._transfer_progress_bar:
                self._transfer_progress_bar.setFormat(f"{max(0, min(item_percent, 100))}%")
        else:
            self._transfer_dialog.setRange(0, 0)
            if self._transfer_progress_bar:
                self._transfer_progress_bar.setFormat("...")

    def _run_next_transfer_item(self) -> None:
        if not self._transfer_context:
            return
        if self._transfer_cancel_requested:
            self._finish_transfer(cancelled=True)
            return

        completed = int(self._transfer_context["completed"])
        total = int(self._transfer_context["total"])
        if completed >= total:
            self._finish_transfer()
            return

        mode = str(self._transfer_context["mode"])
        paths: list[str] = self._transfer_context["paths"]  # type: ignore[assignment]
        target = str(self._transfer_context["target"])
        device = str(self._transfer_context["device"])
        source = paths[completed]

        self._transfer_context["item_percent"] = None
        self._transfer_context["item_total_bytes"] = None
        self._transfer_context["item_done_bytes"] = None
        self._transfer_context["item_speed_bps"] = None
        self._transfer_context["item_eta_seconds"] = None
        self._transfer_context["item_stage"] = "start"
        self._transfer_context["item_idle_seconds"] = 0.0
        self._transfer_context["item_started_at"] = time.time()

        self._update_transfer_popup()

        worker = ProgressWorker(self.adb.transfer, mode, source, target, device)
        worker.signals.progress.connect(self._on_transfer_item_progress)
        worker.signals.result.connect(lambda _result: self._on_transfer_item_done())
        worker.signals.error.connect(self._on_transfer_item_error)
        worker.signals.finished.connect(lambda current=worker: self._on_worker_finished(current))

        self._transfer_worker = worker
        self._active_workers.add(worker)
        self.active_jobs += 1
        self._set_busy(True)
        self.thread_pool.start(worker)

    def _on_transfer_item_progress(self, payload: object) -> None:
        if not self._transfer_context or not isinstance(payload, dict):
            return
        self._transfer_context["item_percent"] = payload.get("percent")
        self._transfer_context["item_total_bytes"] = payload.get("total_bytes")
        self._transfer_context["item_done_bytes"] = payload.get("transferred_bytes")
        self._transfer_context["item_speed_bps"] = payload.get("speed_bps")
        self._transfer_context["item_eta_seconds"] = payload.get("eta_seconds")
        self._transfer_context["item_stage"] = payload.get("stage") or "running"
        self._transfer_context["item_idle_seconds"] = payload.get("idle_seconds") or 0.0
        self._update_transfer_popup()

    def _format_eta(self, seconds: float) -> str:
        value = int(max(0, round(seconds)))
        minutes, rem_seconds = divmod(value, 60)
        hours, rem_minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h {rem_minutes:02d}m"
        if minutes > 0:
            return f"{minutes}m {rem_seconds:02d}s"
        return f"{rem_seconds}s"

    def _on_transfer_item_done(self) -> None:
        self._transfer_worker = None
        if not self._transfer_context:
            return
        self._transfer_context["completed"] = int(self._transfer_context["completed"]) + 1
        self._transfer_context["item_percent"] = 100
        self._update_transfer_popup()
        mode = str(self._transfer_context.get("mode") or "")
        now = time.time()
        last_refresh = float(self._transfer_context.get("last_auto_refresh_time") or 0.0)
        if now - last_refresh >= 0.25:
            if mode == "upload":
                self._refresh_remote()
            elif mode == "download":
                self._refresh_local()
            else:
                self._refresh_local()
                self._refresh_remote()
            self._transfer_context["last_auto_refresh_time"] = now
        self._run_next_transfer_item()

    def _on_transfer_item_error(self, traceback_text: str) -> None:
        self._transfer_worker = None
        if "Transfer cancelled by user" in traceback_text:
            self._finish_transfer(cancelled=True)
            return
        self._finish_transfer(error=traceback_text)

    def _request_transfer_cancel(self) -> None:
        if not self._transfer_context:
            return
        self._transfer_cancel_requested = True
        if self._transfer_worker:
            self._transfer_worker.cancel()
        self._update_transfer_popup()

    def _finish_transfer(self, error: str | None = None, cancelled: bool = False) -> None:
        context = self._transfer_context
        if not context:
            return

        completed = int(context["completed"])
        total = int(context["total"])
        verb = str(context["verb"])
        target = str(context["target"])

        if self._transfer_dialog:
            self._transfer_dialog.hide()

        self._transfer_context = None
        self._transfer_cancel_requested = False
        self._transfer_worker = None

        if error:
            self._on_transfer_error(error)
            self._refresh_local()
            self._refresh_remote()
        elif cancelled:
            self._log(f"Transfer cancelled after {completed}/{total} item(s)")
            self._refresh_local()
            self._refresh_remote()
        else:
            self._after_transfer(f"{verb} {completed} item(s) to {target}")

        if self._pending_transfers:
            self._transfer_context = self._pending_transfers.pop(0)
            self._show_transfer_popup()
            self._run_next_transfer_item()

    def _create_remote_folder(self) -> None:
        if not self.device_id:
            QMessageBox.information(self, "No Device", "Connect an Android device first.")
            return

        folder_name, ok = QInputDialog.getText(self, "New Folder", "Folder name")
        if not ok:
            return
        folder_name = folder_name.strip()
        if not folder_name:
            return
        if "/" in folder_name:
            QMessageBox.warning(self, "Invalid Name", "Folder name cannot contain '/'.")
            return

        full_path = self._normalize_remote_path(posixpath.join(self.remote_path, folder_name))
        device = self.device_id
        self._run_task(
            lambda: self.adb.make_dir(full_path, device),
            on_result=lambda _: self._after_transfer(f"Created folder: {full_path}"),
            on_error=self._on_transfer_error,
        )

    def _delete_remote_selected(self) -> None:
        if not self.device_id:
            QMessageBox.information(self, "No Device", "Connect an Android device first.")
            return

        entries = self.remote_pane.selected_entries()
        if not entries:
            QMessageBox.information(self, "Nothing Selected", "Select Android files or folders to delete.")
            return

        names = "\n".join(f"- {entry.name}" for entry in entries[:10])
        if len(entries) > 10:
            names += f"\n... and {len(entries) - 10} more"

        answer = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete the selected remote item(s)?\n\n{names}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        device = self.device_id

        def task() -> int:
            for entry in entries:
                self.adb.delete_remote(entry.path, device)
            return len(entries)

        self._run_task(
            task,
            on_result=lambda count: self._after_transfer(f"Deleted {count} remote item(s)"),
            on_error=self._on_transfer_error,
        )

    def _after_transfer(self, message: str) -> None:
        self._log(message)
        self._refresh_local()
        self._refresh_remote()

    def _on_transfer_error(self, traceback_text: str) -> None:
        self._log("Transfer/action failed")
        self._log(traceback_text)
        QMessageBox.warning(self, "Operation Failed", "See log for error details.")

    def _run_task(self, fn, on_result=None, on_error=None) -> None:
        worker = Worker(fn)

        if on_result:
            worker.signals.result.connect(on_result)

        if on_error:
            worker.signals.error.connect(on_error)
        else:
            worker.signals.error.connect(self._log)

        worker.signals.finished.connect(lambda current=worker: self._on_worker_finished(current))

        self._active_workers.add(worker)
        self.active_jobs += 1
        self._set_busy(True)
        self.thread_pool.start(worker)

    def _on_worker_finished(self, worker: Worker) -> None:
        self._active_workers.discard(worker)
        self._on_task_finished()

    def _on_task_finished(self) -> None:
        self.active_jobs = max(0, self.active_jobs - 1)
        if self.active_jobs == 0:
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.statusBar().showMessage("Working...")
        else:
            self.statusBar().clearMessage()

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{timestamp}] {message}")

    def _reset_log(self) -> None:
        self.log_output.clear()

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_output.toPlainText())
        self.statusBar().showMessage("Log copied", 2000)

    def _normalize_remote_path(self, path: str) -> str:
        clean = posixpath.normpath(path.strip() or ANDROID_USER_ROOT)
        if not clean.startswith("/"):
            clean = "/" + clean
        if clean == "/" or not clean.startswith(ANDROID_USER_ROOT):
            return ANDROID_USER_ROOT
        return clean

    def _parent_remote_path(self, path: str) -> str | None:
        normalized = self._normalize_remote_path(path)
        if normalized == ANDROID_USER_ROOT:
            return None
        parent = posixpath.dirname(normalized)
        if not parent.startswith(ANDROID_USER_ROOT):
            return ANDROID_USER_ROOT
        return parent if parent else ANDROID_USER_ROOT
