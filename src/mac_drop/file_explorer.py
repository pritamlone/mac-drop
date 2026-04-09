from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileInfo, QMimeData, QPoint, QPropertyAnimation, QRect, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QDrag, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QFileDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QFileIconProvider,
)

from .models import FileEntry


ROLE_META = int(Qt.ItemDataRole.UserRole)
ROLE_SORT = int(Qt.ItemDataRole.UserRole + 1)

ICON_SIZE_MAP: dict[str, tuple[int, int]] = {
    "small": (18, 70),
    "medium": (26, 92),
    "large": (36, 112),
}


@dataclass(slots=True)
class PaneState:
    current_view_mode: str = "list"
    current_directory: str = ""
    selected_paths: list[str] | None = None
    column_history: list[str] | None = None


@dataclass(slots=True)
class MarqueeSelectionState:
    origin: QPoint
    mode: str
    started_on_item: bool
    active: bool = False
    base_selected_rows: set[int] | None = None
    last_hit_rows: set[int] | None = None


def selection_rect(origin: QPoint, current: QPoint) -> QRect:
    return QRect(origin, current).normalized()


def intersects(a: QRect, b: QRect) -> bool:
    return a.intersects(b)


class SelectionBoxOverlay(QWidget):
    """
    Lightweight marquee overlay that renders a Finder-style rubber-band box.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._rect = QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def set_selection_rect(self, rect: QRect) -> None:
        self._rect = rect.normalized()
        if self._rect.isNull():
            self.hide()
            return
        self.setGeometry(self._rect)
        self.show()
        self.update()

    def clear(self) -> None:
        self._rect = QRect()
        self.hide()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._rect.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        fill = QColor(90, 145, 255, 56)
        border = QColor(122, 170, 255, 215)
        painter.fillRect(self.rect(), fill)
        painter.setPen(QPen(border, 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


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


def format_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M")


def _sort_key_for_entry(entry: FileEntry, column: int) -> tuple[object, ...]:
    # Keep folders grouped first for Finder-like behavior across all views.
    folder_rank = 0 if entry.is_dir else 1
    if column == 0:
        return (folder_rank, entry.name.lower())
    if column == 1:
        return (folder_rank, "folder" if entry.is_dir else "file", entry.name.lower())
    if column == 2:
        size = -1 if entry.size_bytes is None else entry.size_bytes
        return (folder_rank, size, entry.name.lower())
    if column == 3:
        modified = int(entry.modified.timestamp()) if entry.modified else -1
        return (folder_rank, modified, entry.name.lower())
    return (folder_rank, entry.name.lower())


class SortTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(ROLE_SORT)
        right = other.data(ROLE_SORT)
        if left is None or right is None:
            return super().__lt__(other)
        return left < right


class TransferTableWidget(QTableWidget):
    payload_dropped = Signal(str, list, str)

    def __init__(self, pane: "BrowserPane") -> None:
        super().__init__(0, 4, pane)
        self._pane = pane
        self._drag_start_pos = QPoint()
        self._drag_threshold_px = 5
        self._drag_row = -1
        self._marquee = SelectionBoxOverlay(self.viewport())
        self._marquee_state: MarqueeSelectionState | None = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        press_pos = event.position().toPoint()
        self._drag_start_pos = press_pos
        self._drag_row = self.rowAt(press_pos.y())
        clicked_index = self.indexAt(press_pos)

        mode = "replace"
        modifiers = event.modifiers()
        if modifiers & (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier):
            mode = "toggle"
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            mode = "extend"

        self._marquee.clear()
        self._marquee_state = MarqueeSelectionState(
            origin=press_pos,
            mode=mode,
            started_on_item=clicked_index.isValid(),
            active=False,
            base_selected_rows=self._selected_rows_snapshot(),
            last_hit_rows=set(),
        )

        if not clicked_index.isValid():
            self.setFocus()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        current = event.position().toPoint()
        threshold = max(self._drag_threshold_px, QApplication.startDragDistance())
        if (current - self._drag_start_pos).manhattanLength() < threshold:
            super().mouseMoveEvent(event)
            return

        state = self._marquee_state
        if state and not state.started_on_item:
            if not state.active:
                state.active = True
            self._update_marquee_selection(current)
            event.accept()
            return

        if self._drag_row < 0:
            super().mouseMoveEvent(event)
            return
        item = self.item(self._drag_row, 0)
        if item is None or not item.isSelected():
            super().mouseMoveEvent(event)
            return
        self._start_drag()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        state = self._marquee_state
        if event.button() == Qt.MouseButton.LeftButton and state and not state.started_on_item:
            threshold = max(self._drag_threshold_px, QApplication.startDragDistance())
            moved = (event.position().toPoint() - state.origin).manhattanLength()
            if state.active:
                self._marquee.clear()
                self._marquee_state = None
                event.accept()
                return

            if moved < threshold and state.mode == "replace":
                self.clearSelection()
            self._marquee.clear()
            self._marquee_state = None
            event.accept()
            return

        self._marquee.clear()
        self._marquee_state = None
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._marquee_state and not self._marquee_state.active:
            self._marquee.clear()
        super().leaveEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._can_accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._can_accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        target_path = self._drop_target_path(event.position().toPoint())
        mime_data = event.mimeData()

        if mime_data.hasUrls() and self._pane.kind == "remote":
            local_paths = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]
            local_paths = [path for path in local_paths if path]
            if local_paths:
                self.payload_dropped.emit("external", local_paths, target_path)
                event.acceptProposedAction()
                return

        payload = self._extract_payload(mime_data)
        if payload:
            source_kind, paths = payload
            self.payload_dropped.emit(source_kind, paths, target_path)
            event.acceptProposedAction()
            return

        event.ignore()

    def _drop_target_path(self, point: QPoint) -> str:
        fallback = self._pane.current_path
        row = self.rowAt(point.y())
        if row < 0:
            return fallback
        item = self.item(row, 0)
        if item is None:
            return fallback
        meta = item.data(ROLE_META)
        if not isinstance(meta, dict):
            return fallback
        if meta.get("kind") == "parent":
            return str(meta.get("path") or fallback)
        if meta.get("kind") == "entry" and meta.get("is_dir"):
            return str(meta.get("path") or fallback)
        return fallback

    def _start_drag(self) -> None:
        entries = self._pane.selected_entries()
        if not entries:
            return
        payload = {"source": self._pane.kind, "paths": [entry.path for entry in entries]}
        mime_data = QMimeData()
        mime_data.setData(self._pane.MIME_TYPE, json.dumps(payload).encode("utf-8"))
        mime_data.setText("\n".join(payload["paths"]))

        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

    def _can_accept_mime(self, mime_data) -> bool:
        if mime_data.hasUrls() and self._pane.kind == "remote":
            return any(url.isLocalFile() for url in mime_data.urls())

        payload = self._extract_payload(mime_data)
        if not payload:
            return False
        source_kind, _paths = payload
        return (source_kind == "local" and self._pane.kind == "remote") or (
            source_kind == "remote" and self._pane.kind == "local"
        )

    def _extract_payload(self, mime_data) -> tuple[str, list[str]] | None:
        if not mime_data.hasFormat(self._pane.MIME_TYPE):
            return None
        try:
            raw = bytes(mime_data.data(self._pane.MIME_TYPE)).decode("utf-8")
            parsed = json.loads(raw)
        except Exception:
            return None

        source = parsed.get("source")
        paths = parsed.get("paths")
        if not isinstance(source, str) or not isinstance(paths, list):
            return None

        normalized = [str(path) for path in paths if str(path).strip()]
        if not normalized:
            return None
        return source, normalized

    def _selected_rows_snapshot(self) -> set[int]:
        selected: set[int] = set()
        model = self.selectionModel()
        if model is None:
            return selected
        for index in model.selectedRows():
            row = index.row()
            if self._is_entry_row(row):
                selected.add(row)
        return selected

    def _is_entry_row(self, row: int) -> bool:
        item = self.item(row, 0)
        if item is None:
            return False
        meta = item.data(ROLE_META)
        return isinstance(meta, dict) and meta.get("kind") == "entry"

    def _hit_rows_for_rect(self, rect: QRect) -> set[int]:
        hits: set[int] = set()
        for row in range(self.rowCount()):
            if not self._is_entry_row(row):
                continue
            index = self.model().index(row, 0)
            if not index.isValid():
                continue
            row_rect = self.visualRect(index)
            if intersects(rect, row_rect):
                hits.add(row)
        return hits

    def _apply_row_selection(self, desired_rows: set[int]) -> None:
        model = self.selectionModel()
        if model is None:
            return
        self.blockSignals(True)
        self.clearSelection()
        for row in sorted(desired_rows):
            self.selectRow(row)
        self.blockSignals(False)
        self.itemSelectionChanged.emit()

    def _update_marquee_selection(self, current: QPoint) -> None:
        state = self._marquee_state
        if not state:
            return
        viewport_rect = self.viewport().rect()
        clamped_current = QPoint(
            max(viewport_rect.left(), min(current.x(), viewport_rect.right())),
            max(viewport_rect.top(), min(current.y(), viewport_rect.bottom())),
        )
        marquee_rect = selection_rect(state.origin, clamped_current)
        self._marquee.set_selection_rect(marquee_rect)
        hit_rows = self._hit_rows_for_rect(marquee_rect)

        previous_hits = state.last_hit_rows or set()
        if hit_rows == previous_hits:
            return
        state.last_hit_rows = hit_rows

        base_rows = state.base_selected_rows or set()
        if state.mode == "toggle":
            desired = base_rows.symmetric_difference(hit_rows)
        elif state.mode == "extend":
            desired = base_rows.union(hit_rows)
        else:
            desired = set(hit_rows)
        self._apply_row_selection(desired)


class TransferListWidget(QListWidget):
    payload_dropped = Signal(str, list, str)

    def __init__(self, pane: "BrowserPane") -> None:
        super().__init__(pane)
        self._pane = pane
        self._drag_start_pos = QPoint()
        self._drag_threshold_px = 5
        self._marquee = SelectionBoxOverlay(self.viewport())
        self._marquee_state: MarqueeSelectionState | None = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        press_pos = event.position().toPoint()
        self._drag_start_pos = press_pos
        clicked_item = self.itemAt(press_pos)

        mode = "replace"
        modifiers = event.modifiers()
        if modifiers & (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier):
            mode = "toggle"
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            mode = "extend"

        self._marquee.clear()
        self._marquee_state = MarqueeSelectionState(
            origin=press_pos,
            mode=mode,
            started_on_item=clicked_item is not None,
            active=False,
            base_selected_rows=self._selected_rows_snapshot(),
            last_hit_rows=set(),
        )

        if clicked_item is None:
            # Keep current selection until drag threshold is crossed.
            # A simple click on empty space clears selection on mouse release.
            self.setFocus()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return

        current = event.position().toPoint()
        threshold = max(self._drag_threshold_px, QApplication.startDragDistance())
        if (current - self._drag_start_pos).manhattanLength() < threshold:
            super().mouseMoveEvent(event)
            return

        state = self._marquee_state
        if state and not state.started_on_item:
            if not state.active:
                state.active = True
            self._update_marquee_selection(current)
            event.accept()
            return

        item = self.itemAt(self._drag_start_pos)
        if item is None or not item.isSelected():
            super().mouseMoveEvent(event)
            return
        self._start_drag()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        state = self._marquee_state
        if event.button() == Qt.MouseButton.LeftButton and state and not state.started_on_item:
            threshold = max(self._drag_threshold_px, QApplication.startDragDistance())
            moved = (event.position().toPoint() - state.origin).manhattanLength()
            if state.active:
                self._marquee.clear()
                self._marquee_state = None
                event.accept()
                return

            if moved < threshold and state.mode == "replace":
                self.clearSelection()
            self._marquee.clear()
            self._marquee_state = None
            event.accept()
            return

        self._marquee.clear()
        self._marquee_state = None
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._marquee_state and not self._marquee_state.active:
            self._marquee.clear()
        super().leaveEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._can_accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._can_accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        target_path = self._drop_target_path(event.position().toPoint())
        mime_data = event.mimeData()

        if mime_data.hasUrls() and self._pane.kind == "remote":
            local_paths = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]
            local_paths = [path for path in local_paths if path]
            if local_paths:
                self.payload_dropped.emit("external", local_paths, target_path)
                event.acceptProposedAction()
                return

        payload = self._extract_payload(mime_data)
        if payload:
            source_kind, paths = payload
            self.payload_dropped.emit(source_kind, paths, target_path)
            event.acceptProposedAction()
            return

        event.ignore()

    def _drop_target_path(self, point: QPoint) -> str:
        fallback = self._pane.current_path
        item = self.itemAt(point)
        if item is None:
            return fallback
        meta = item.data(ROLE_META)
        if not isinstance(meta, dict):
            return fallback
        if meta.get("kind") == "entry" and meta.get("is_dir"):
            return str(meta.get("path") or fallback)
        return fallback

    def _start_drag(self) -> None:
        entries = self._pane.selected_entries()
        if not entries:
            return
        payload = {"source": self._pane.kind, "paths": [entry.path for entry in entries]}
        mime_data = QMimeData()
        mime_data.setData(self._pane.MIME_TYPE, json.dumps(payload).encode("utf-8"))
        mime_data.setText("\n".join(payload["paths"]))

        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

    def _can_accept_mime(self, mime_data) -> bool:
        if mime_data.hasUrls() and self._pane.kind == "remote":
            return any(url.isLocalFile() for url in mime_data.urls())

        payload = self._extract_payload(mime_data)
        if not payload:
            return False
        source_kind, _paths = payload
        return (source_kind == "local" and self._pane.kind == "remote") or (
            source_kind == "remote" and self._pane.kind == "local"
        )

    def _extract_payload(self, mime_data) -> tuple[str, list[str]] | None:
        if not mime_data.hasFormat(self._pane.MIME_TYPE):
            return None
        try:
            raw = bytes(mime_data.data(self._pane.MIME_TYPE)).decode("utf-8")
            parsed = json.loads(raw)
        except Exception:
            return None

        source = parsed.get("source")
        paths = parsed.get("paths")
        if not isinstance(source, str) or not isinstance(paths, list):
            return None

        normalized = [str(path) for path in paths if str(path).strip()]
        if not normalized:
            return None
        return source, normalized

    def _selected_rows_snapshot(self) -> set[int]:
        selected: set[int] = set()
        for item in self.selectedItems():
            row = self.row(item)
            if row >= 0:
                selected.add(row)
        return selected

    def _hit_rows_for_rect(self, rect: QRect) -> set[int]:
        hits: set[int] = set()
        for row in range(self.count()):
            item = self.item(row)
            item_rect = self.visualItemRect(item)
            if intersects(rect, item_rect):
                hits.add(row)
        return hits

    def _apply_row_selection(self, desired_rows: set[int]) -> None:
        self.blockSignals(True)
        for row in range(self.count()):
            item = self.item(row)
            should_select = row in desired_rows
            if item.isSelected() != should_select:
                item.setSelected(should_select)
        self.blockSignals(False)
        self.itemSelectionChanged.emit()

    def _update_marquee_selection(self, current: QPoint) -> None:
        state = self._marquee_state
        if not state:
            return
        viewport_rect = self.viewport().rect()
        clamped_current = QPoint(
            max(viewport_rect.left(), min(current.x(), viewport_rect.right())),
            max(viewport_rect.top(), min(current.y(), viewport_rect.bottom())),
        )
        marquee_rect = selection_rect(state.origin, clamped_current)
        self._marquee.set_selection_rect(marquee_rect)
        hit_rows = self._hit_rows_for_rect(marquee_rect)

        previous_hits = state.last_hit_rows or set()
        if hit_rows == previous_hits:
            return
        state.last_hit_rows = hit_rows

        base_rows = state.base_selected_rows or set()
        if state.mode == "toggle":
            desired = base_rows.symmetric_difference(hit_rows)
        elif state.mode == "extend":
            desired = base_rows.union(hit_rows)
        else:
            desired = set(hit_rows)
        self._apply_row_selection(desired)


class ListView(QWidget):
    selection_paths_changed = Signal(list)
    entry_activated = Signal(FileEntry)
    navigate_parent = Signal(str)
    sort_changed = Signal(int, object)
    transfer_dropped = Signal(str, list, str)

    def __init__(self, pane: "BrowserPane") -> None:
        super().__init__(pane)
        self._pane = pane
        self.table = TransferTableWidget(pane)
        self.table.setHorizontalHeaderLabels(["Name", "Kind", "Size", "Modified"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.table, 1)

        self.table.itemSelectionChanged.connect(self._emit_selection)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._on_sort_changed)
        self.table.payload_dropped.connect(self.transfer_dropped.emit)

    def set_data(
        self,
        entries: list[FileEntry],
        parent_path: str | None,
        selected_paths: list[str],
        sort_column: int,
        sort_order: Qt.SortOrder,
    ) -> None:
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.clearContents()

        row_count = len(entries) + (1 if parent_path else 0)
        self.table.setRowCount(row_count)

        row = 0
        if parent_path:
            self._set_parent_row(row, parent_path)
            row += 1
        for entry in entries:
            self._set_entry_row(row, entry)
            row += 1

        self.table.setSortingEnabled(True)
        self.table.sortItems(sort_column, sort_order)
        self._apply_selection(selected_paths)
        header.blockSignals(False)

    def selected_paths(self) -> list[str]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        selected: list[str] = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is None:
                continue
            meta = item.data(ROLE_META)
            if not isinstance(meta, dict):
                continue
            if meta.get("kind") == "entry":
                path = str(meta.get("path") or "")
                if path:
                    selected.append(path)
        return selected

    def scroll_state(self) -> tuple[int, int]:
        return (self.table.horizontalScrollBar().value(), self.table.verticalScrollBar().value())

    def restore_scroll_state(self, state: tuple[int, int]) -> None:
        self.table.horizontalScrollBar().setValue(int(state[0]))
        self.table.verticalScrollBar().setValue(int(state[1]))

    def _set_parent_row(self, row: int, parent_path: str) -> None:
        name_item = SortTableWidgetItem("..")
        kind_item = SortTableWidgetItem("Parent")
        size_item = SortTableWidgetItem("-")
        modified_item = SortTableWidgetItem("-")
        meta = {"kind": "parent", "path": parent_path}
        name_item.setData(ROLE_META, meta)
        name_item.setData(ROLE_SORT, (0, ""))
        kind_item.setData(ROLE_SORT, (0, ""))
        size_item.setData(ROLE_SORT, (0, -1))
        modified_item.setData(ROLE_SORT, (0, -1))
        self.table.setItem(row, 0, name_item)
        self.table.setItem(row, 1, kind_item)
        self.table.setItem(row, 2, size_item)
        self.table.setItem(row, 3, modified_item)

    def _set_entry_row(self, row: int, entry: FileEntry) -> None:
        icon = self._pane.icon_for_entry(entry)

        name_item = SortTableWidgetItem(entry.name)
        name_item.setIcon(icon)
        kind_item = SortTableWidgetItem("Folder" if entry.is_dir else "File")
        size_item = SortTableWidgetItem(format_size(entry.size_bytes))
        modified_item = SortTableWidgetItem(format_time(entry.modified))

        meta = {"kind": "entry", "path": entry.path, "is_dir": entry.is_dir}
        for item in (name_item, kind_item, size_item, modified_item):
            item.setData(ROLE_META, meta)

        name_item.setData(ROLE_SORT, _sort_key_for_entry(entry, 0))
        kind_item.setData(ROLE_SORT, _sort_key_for_entry(entry, 1))
        size_item.setData(ROLE_SORT, _sort_key_for_entry(entry, 2))
        modified_item.setData(ROLE_SORT, _sort_key_for_entry(entry, 3))

        self.table.setItem(row, 0, name_item)
        self.table.setItem(row, 1, kind_item)
        self.table.setItem(row, 2, size_item)
        self.table.setItem(row, 3, modified_item)

    def _emit_selection(self) -> None:
        self.selection_paths_changed.emit(self.selected_paths())

    def _on_double_click(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is None:
            return
        meta = item.data(ROLE_META)
        if not isinstance(meta, dict):
            return
        if meta.get("kind") == "parent":
            self.navigate_parent.emit(str(meta.get("path") or ""))
            return

        path = str(meta.get("path") or "")
        if not path:
            return
        entry = self._pane.entry_by_path(path)
        if entry and entry.is_dir:
            self.entry_activated.emit(entry)

    def _apply_selection(self, selected_paths: list[str]) -> None:
        selected = set(selected_paths)
        self.table.blockSignals(True)
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            meta = item.data(ROLE_META)
            if not isinstance(meta, dict) or meta.get("kind") != "entry":
                continue
            path = str(meta.get("path") or "")
            if path in selected:
                self.table.selectRow(row)
        self.table.blockSignals(False)

    def _on_sort_changed(self, column: int, order: Qt.SortOrder) -> None:
        self.sort_changed.emit(column, order)


class IconView(QWidget):
    selection_paths_changed = Signal(list)
    entry_activated = Signal(FileEntry)
    transfer_dropped = Signal(str, list, str)

    def __init__(self, pane: "BrowserPane") -> None:
        super().__init__(pane)
        self._pane = pane
        self.list = TransferListWidget(pane)
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setSpacing(14)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionRectVisible(False)
        self.list.setWordWrap(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.list, 1)

        self.list.itemSelectionChanged.connect(self._emit_selection)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.payload_dropped.connect(self.transfer_dropped.emit)

        self._set_icon_scale("medium")

    def set_data(self, entries: list[FileEntry], selected_paths: list[str], icon_size: str) -> None:
        self._set_icon_scale(icon_size)

        self.list.blockSignals(True)
        self.list.clear()

        for entry in entries:
            item = QListWidgetItem(entry.name)
            item.setIcon(self._pane.icon_for_entry(entry))
            item.setToolTip(entry.path)
            item.setData(ROLE_META, {"kind": "entry", "path": entry.path, "is_dir": entry.is_dir})
            self.list.addItem(item)

        selected = set(selected_paths)
        for index in range(self.list.count()):
            item = self.list.item(index)
            meta = item.data(ROLE_META)
            if isinstance(meta, dict) and str(meta.get("path") or "") in selected:
                item.setSelected(True)

        self.list.blockSignals(False)

    def selected_paths(self) -> list[str]:
        paths: list[str] = []
        for item in self.list.selectedItems():
            meta = item.data(ROLE_META)
            if isinstance(meta, dict):
                path = str(meta.get("path") or "")
                if path:
                    paths.append(path)
        return paths

    def scroll_state(self) -> tuple[int, int]:
        return (self.list.horizontalScrollBar().value(), self.list.verticalScrollBar().value())

    def restore_scroll_state(self, state: tuple[int, int]) -> None:
        self.list.horizontalScrollBar().setValue(int(state[0]))
        self.list.verticalScrollBar().setValue(int(state[1]))

    def _set_icon_scale(self, scale: str) -> None:
        icon_px, grid_h = ICON_SIZE_MAP.get(scale, ICON_SIZE_MAP["medium"])
        self.list.setIconSize(QPixmap(icon_px, icon_px).size())
        self.list.setGridSize(QPixmap(120, grid_h).size())

    def _emit_selection(self) -> None:
        self.selection_paths_changed.emit(self.selected_paths())

    def _on_double_click(self, item: QListWidgetItem) -> None:
        meta = item.data(ROLE_META)
        if not isinstance(meta, dict):
            return
        path = str(meta.get("path") or "")
        if not path:
            return
        entry = self._pane.entry_by_path(path)
        if entry and entry.is_dir:
            self.entry_activated.emit(entry)


class ColumnEntryList(QListWidget):
    item_activated = Signal(int, str)
    context_menu_requested = Signal(int, QPoint, str)

    def __init__(self, column_index: int, pane: "BrowserPane") -> None:
        super().__init__(pane)
        self._column_index = column_index
        self._pane = pane
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.itemClicked.connect(self._emit_click)
        self.itemDoubleClicked.connect(self._emit_click)
        self.customContextMenuRequested.connect(self._emit_context_menu)

    def _emit_click(self, item: QListWidgetItem) -> None:
        meta = item.data(ROLE_META)
        if not isinstance(meta, dict):
            return
        path = str(meta.get("path") or "")
        if not path:
            return
        self.item_activated.emit(self._column_index, path)

    def _emit_context_menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        path = ""
        if item is not None:
            meta = item.data(ROLE_META)
            if isinstance(meta, dict):
                path = str(meta.get("path") or "")
        self.context_menu_requested.emit(self._column_index, pos, path)


class ColumnView(QWidget):
    path_requested = Signal(int, str)
    context_menu_requested = Signal(QPoint, str)

    def __init__(self, pane: "BrowserPane") -> None:
        super().__init__(pane)
        self._pane = pane
        self._columns_host = QWidget()
        self._columns_layout = QHBoxLayout(self._columns_host)
        self._columns_layout.setContentsMargins(0, 0, 0, 0)
        self._columns_layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._columns_host)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll, 1)

    def set_data(
        self,
        path_history: list[str],
        directory_cache: dict[str, list[FileEntry]],
        selected_paths: list[str],
    ) -> None:
        while self._columns_layout.count():
            item = self._columns_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        selected = set(selected_paths)
        next_path_by_level: dict[int, str] = {}
        for idx, path in enumerate(path_history[1:]):
            next_path_by_level[idx] = path

        for level, column_path in enumerate(path_history):
            list_widget = ColumnEntryList(level, self._pane)
            list_widget.setMinimumWidth(220)
            list_widget.item_activated.connect(self.path_requested.emit)
            list_widget.context_menu_requested.connect(
                lambda lvl, pos, path, widget=list_widget: self.context_menu_requested.emit(
                    widget.viewport().mapToGlobal(pos), path
                )
            )
            entries = directory_cache.get(column_path, [])
            for entry in entries:
                label = entry.name + ("  ›" if entry.is_dir else "")
                item = QListWidgetItem(self._pane.icon_for_entry(entry), label)
                item.setData(ROLE_META, {"path": entry.path, "is_dir": entry.is_dir})
                list_widget.addItem(item)

                should_select = False
                if entry.path in selected:
                    should_select = True
                if next_path_by_level.get(level) == entry.path:
                    should_select = True
                if should_select:
                    item.setSelected(True)

            self._columns_layout.addWidget(list_widget)

        self._columns_layout.addStretch(1)
        QApplication.processEvents()
        self._scroll.horizontalScrollBar().setValue(self._scroll.horizontalScrollBar().maximum())

    def scroll_state(self) -> tuple[int, int]:
        return (self._scroll.horizontalScrollBar().value(), self._scroll.verticalScrollBar().value())

    def restore_scroll_state(self, state: tuple[int, int]) -> None:
        self._scroll.horizontalScrollBar().setValue(int(state[0]))
        self._scroll.verticalScrollBar().setValue(int(state[1]))


class GalleryView(QWidget):
    selection_paths_changed = Signal(list)
    entry_activated = Signal(FileEntry)

    def __init__(self, pane: "BrowserPane") -> None:
        super().__init__(pane)
        self._pane = pane
        self._preview = QLabel("Select an item to preview")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumHeight(320)
        self._preview.setWordWrap(True)

        self._thumbs = QListWidget()
        self._thumbs.setViewMode(QListWidget.ViewMode.IconMode)
        self._thumbs.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._thumbs.setMovement(QListWidget.Movement.Static)
        self._thumbs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._thumbs.setFlow(QListWidget.Flow.LeftToRight)
        self._thumbs.setWrapping(False)
        self._thumbs.setIconSize(QPixmap(64, 64).size())
        self._thumbs.setGridSize(QPixmap(90, 96).size())
        self._thumbs.setFixedHeight(120)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._preview, 1)
        root.addWidget(self._thumbs)

        self._thumbs.itemSelectionChanged.connect(self._on_selection_changed)
        self._thumbs.itemDoubleClicked.connect(self._on_double_click)

    def set_data(self, entries: list[FileEntry], selected_paths: list[str]) -> None:
        self._thumbs.blockSignals(True)
        self._thumbs.clear()

        selected = set(selected_paths)
        selected_index = 0

        for idx, entry in enumerate(entries):
            item = QListWidgetItem(self._pane.icon_for_entry(entry), entry.name)
            item.setData(ROLE_META, {"path": entry.path, "is_dir": entry.is_dir})
            self._thumbs.addItem(item)
            if entry.path in selected:
                item.setSelected(True)
                selected_index = idx

        if self._thumbs.count() > 0:
            self._thumbs.setCurrentRow(selected_index)

        self._thumbs.blockSignals(False)
        self._update_preview()

    def selected_paths(self) -> list[str]:
        paths: list[str] = []
        for item in self._thumbs.selectedItems():
            meta = item.data(ROLE_META)
            if not isinstance(meta, dict):
                continue
            path = str(meta.get("path") or "")
            if path:
                paths.append(path)
        return paths

    def scroll_state(self) -> tuple[int, int]:
        return (self._thumbs.horizontalScrollBar().value(), self._thumbs.verticalScrollBar().value())

    def restore_scroll_state(self, state: tuple[int, int]) -> None:
        self._thumbs.horizontalScrollBar().setValue(int(state[0]))
        self._thumbs.verticalScrollBar().setValue(int(state[1]))

    def _on_selection_changed(self) -> None:
        self.selection_paths_changed.emit(self.selected_paths())
        self._update_preview()

    def _on_double_click(self, item: QListWidgetItem) -> None:
        meta = item.data(ROLE_META)
        if not isinstance(meta, dict):
            return
        path = str(meta.get("path") or "")
        if not path:
            return
        entry = self._pane.entry_by_path(path)
        if entry and entry.is_dir:
            self.entry_activated.emit(entry)

    def _update_preview(self) -> None:
        current = self._thumbs.currentItem()
        if current is None:
            self._preview.setPixmap(QPixmap())
            self._preview.setText("Select an item to preview")
            return

        meta = current.data(ROLE_META)
        if not isinstance(meta, dict):
            return

        path = str(meta.get("path") or "")
        entry = self._pane.entry_by_path(path)
        if not entry:
            return

        if self._pane.kind == "local" and not entry.is_dir and Path(path).exists():
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self._preview.setText("")
                self._preview.setPixmap(
                    pixmap.scaled(
                        self._preview.width() - 24,
                        self._preview.height() - 24,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                return

        icon = self._pane.icon_for_entry(entry)
        pix = icon.pixmap(220, 220)
        self._preview.setPixmap(pix)
        self._preview.setText(f"\n{entry.name}")


class BrowserPane(QWidget):
    """
    File explorer pane with a shared data source and switchable rendering strategies.

    Architecture notes:
    - `self.entries` is the single source of truth for file items.
    - All view widgets render from that same list.
    - Selection is tracked in `self.selected_paths` and synced across views.
    """

    navigate_requested = Signal(str)
    refresh_requested = Signal()
    selection_changed = Signal()
    transfer_dropped = Signal(str, list, str)
    context_menu_requested = Signal(QPoint)
    MIME_TYPE = "application/x-mac-drop-items"

    def __init__(self, title: str, kind: str) -> None:
        super().__init__()
        self.kind = kind

        self.entries: list[FileEntry] = []
        self.current_path = ""
        self.parent_path: str | None = None
        self.view_mode = "list"

        self.sort_column = 0
        self.sort_order = Qt.SortOrder.AscendingOrder
        self.icon_size = "medium"

        self.selected_paths: list[str] = []
        self._path_to_entry: dict[str, FileEntry] = {}

        self._directory_cache: dict[str, list[FileEntry]] = {}
        self._column_history: list[str] = []
        self._scroll_positions: dict[str, tuple[int, int]] = {
            "icon": (0, 0),
            "list": (0, 0),
            "column": (0, 0),
            "gallery": (0, 0),
        }
        self._fade_animation: QPropertyAnimation | None = None
        self._nav_history: list[str] = []
        self._nav_index = -1
        self._history_jump_pending = False

        self.icon_provider = QFileIconProvider()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        title_label = QLabel(title)
        title_label.setObjectName("paneTitle")
        root.addWidget(title_label)

        nav = QHBoxLayout()
        self.back_button = QPushButton("‹")
        self.forward_button = QPushButton("›")
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Path")
        self.go_button = QPushButton("Go")
        self.refresh_button = QPushButton("Refresh")
        self.back_button.setFixedWidth(36)
        self.forward_button.setFixedWidth(36)
        self.path_edit.setToolTip("Enter a path and press Enter. Right-click for Go/Open actions.")
        nav.addWidget(self.back_button)
        nav.addWidget(self.forward_button)
        nav.addWidget(self.path_edit, 1)
        nav.addWidget(self.go_button)
        nav.addWidget(self.refresh_button)
        root.addLayout(nav)

        self.stack = QStackedWidget()
        self.list_view = ListView(self)
        self.icon_view = IconView(self)
        self.column_view = ColumnView(self)
        self.gallery_view = GalleryView(self)

        self._view_widgets: dict[str, QWidget] = {
            "icon": self.icon_view,
            "list": self.list_view,
            "column": self.column_view,
            "gallery": self.gallery_view,
        }

        for mode in ("icon", "list", "column", "gallery"):
            self.stack.addWidget(self._view_widgets[mode])

        root.addWidget(self.stack, 1)

        self.path_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.path_edit.returnPressed.connect(self._emit_path)
        self.path_edit.customContextMenuRequested.connect(self._on_path_context_menu)
        self.go_button.clicked.connect(self._emit_path)
        self.back_button.clicked.connect(self._emit_back)
        self.forward_button.clicked.connect(self._emit_forward)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.list_view.selection_paths_changed.connect(self._sync_selection_from_view)
        self.icon_view.selection_paths_changed.connect(self._sync_selection_from_view)
        self.gallery_view.selection_paths_changed.connect(self._sync_selection_from_view)

        self.list_view.entry_activated.connect(self._on_entry_activated)
        self.icon_view.entry_activated.connect(self._on_entry_activated)
        self.gallery_view.entry_activated.connect(self._on_entry_activated)
        self.list_view.navigate_parent.connect(self.navigate_requested.emit)

        self.list_view.transfer_dropped.connect(self.transfer_dropped.emit)
        self.icon_view.transfer_dropped.connect(self.transfer_dropped.emit)
        self.column_view.path_requested.connect(self._on_column_path_requested)
        self.column_view.context_menu_requested.connect(self._on_column_context_menu)

        self.list_view.sort_changed.connect(self._on_sort_changed)

        self.list_view.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.icon_view.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gallery_view._thumbs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.table.customContextMenuRequested.connect(self._on_list_context_menu)
        self.icon_view.list.customContextMenuRequested.connect(self._on_icon_context_menu)
        self.gallery_view._thumbs.customContextMenuRequested.connect(self._on_gallery_context_menu)

        self.apply_view_mode("list")

    def state(self) -> PaneState:
        return PaneState(
            current_view_mode=self.view_mode,
            current_directory=self.current_path,
            selected_paths=list(self.selected_paths),
            column_history=list(self._column_history),
        )

    def set_path(self, path: str, parent_path: str | None) -> None:
        self.current_path = path
        self.path_edit.setText(path)
        self.parent_path = parent_path
        self._record_navigation(path)

        if not self._column_history:
            self._column_history = [path]
        else:
            if path in self._column_history:
                self._column_history = self._column_history[: self._column_history.index(path) + 1]
            else:
                prev = self._column_history[-1]
                if self._parent_of(path) == prev:
                    self._column_history.append(path)
                else:
                    self._column_history = [path]

    def set_entries(self, entries: list[FileEntry], parent_path: str | None) -> None:
        self.parent_path = parent_path
        self.entries = list(entries)
        self._path_to_entry = {entry.path: entry for entry in entries}
        self._directory_cache[self.current_path] = list(entries)

        # Keep selection stable across refreshes and view switches.
        selected = [path for path in self.selected_paths if path in self._path_to_entry]
        self.selected_paths = selected

        self._render_views()

    def apply_view_mode(self, mode: str) -> None:
        if mode not in self._view_widgets:
            mode = "list"

        previous = self.view_mode
        if previous in self._view_widgets:
            self._scroll_positions[previous] = self._scroll_state(previous)

        self.view_mode = mode
        self._render_views()

        next_widget = self._view_widgets[mode]
        self.stack.setCurrentWidget(next_widget)
        self._restore_scroll_state(mode)
        self._animate_switch(next_widget)

    def set_icon_size(self, icon_size: str) -> None:
        if icon_size not in ICON_SIZE_MAP:
            return
        self.icon_size = icon_size
        self._render_views()

    def selected_entries(self) -> list[FileEntry]:
        result: list[FileEntry] = []
        for path in self.selected_paths:
            entry = self._path_to_entry.get(path)
            if entry:
                result.append(entry)
        return result

    def entry_by_path(self, path: str) -> FileEntry | None:
        return self._path_to_entry.get(path)

    def icon_for_entry(self, entry: FileEntry):
        if self.kind == "local":
            local_path = Path(entry.path)
            if local_path.exists():
                icon = self.icon_provider.icon(QFileInfo(str(local_path)))
                if not icon.isNull():
                    return icon
        style = self.style()
        pixmap = QStyle.StandardPixmap.SP_DirIcon if entry.is_dir else QStyle.StandardPixmap.SP_FileIcon
        return style.standardIcon(pixmap)

    def _render_views(self) -> None:
        ordered_entries = self._ordered_entries()
        self.list_view.set_data(ordered_entries, self.parent_path, self.selected_paths, self.sort_column, self.sort_order)
        self.icon_view.set_data(ordered_entries, self.selected_paths, self.icon_size)
        self.gallery_view.set_data(ordered_entries, self.selected_paths)

        history = self._safe_column_history()
        self.column_view.set_data(history, self._directory_cache, self.selected_paths)

    def _ordered_entries(self) -> list[FileEntry]:
        ordered = list(self.entries)
        reverse = self.sort_order == Qt.SortOrder.DescendingOrder
        ordered.sort(key=lambda entry: _sort_key_for_entry(entry, self.sort_column), reverse=reverse)
        return ordered

    def _safe_column_history(self) -> list[str]:
        if not self._column_history:
            return [self.current_path] if self.current_path else []
        return list(self._column_history)

    def _sync_selection_from_view(self, selected_paths: list[str]) -> None:
        unique_paths: list[str] = []
        seen: set[str] = set()
        for path in selected_paths:
            if path not in seen and path in self._path_to_entry:
                unique_paths.append(path)
                seen.add(path)
        self.selected_paths = unique_paths
        self._sync_selection_to_other_views()
        self.selection_changed.emit()

    def _sync_selection_to_other_views(self) -> None:
        active = self.view_mode
        for mode in ("icon", "list", "gallery"):
            if mode == active:
                continue
            if mode == "icon":
                self.icon_view.set_data(self._ordered_entries(), self.selected_paths, self.icon_size)
            elif mode == "list":
                self.list_view.set_data(
                    self._ordered_entries(),
                    self.parent_path,
                    self.selected_paths,
                    self.sort_column,
                    self.sort_order,
                )
            elif mode == "gallery":
                self.gallery_view.set_data(self._ordered_entries(), self.selected_paths)

    def _on_entry_activated(self, entry: FileEntry) -> None:
        if entry.is_dir:
            self.navigate_requested.emit(entry.path)

    def _on_column_path_requested(self, level: int, path: str) -> None:
        entry = self.entry_by_path(path) or self._cached_entry_by_path(path)
        if entry and entry.is_dir:
            self._column_history = self._column_history[: level + 1]
            if not self._column_history or self._column_history[-1] != path:
                self._column_history.append(path)
            self.navigate_requested.emit(path)
            return

        # file selection in column mode
        self.selected_paths = [path] if path in self._path_to_entry else []
        self.selection_changed.emit()
        self._render_views()

    def _on_list_context_menu(self, pos: QPoint) -> None:
        row = self.list_view.table.rowAt(pos.y())
        if row >= 0:
            item = self.list_view.table.item(row, 0)
            if item is not None:
                meta = item.data(ROLE_META)
                if isinstance(meta, dict) and meta.get("kind") == "entry":
                    path = str(meta.get("path") or "")
                    if path and path not in self.selected_paths:
                        self.selected_paths = [path]
                        self._sync_selection_to_other_views()
                        self.selection_changed.emit()
        self.context_menu_requested.emit(self.list_view.table.viewport().mapToGlobal(pos))

    def _on_icon_context_menu(self, pos: QPoint) -> None:
        item = self.icon_view.list.itemAt(pos)
        if item is not None:
            meta = item.data(ROLE_META)
            if isinstance(meta, dict):
                path = str(meta.get("path") or "")
                if path and path not in self.selected_paths:
                    self.selected_paths = [path]
                    self._sync_selection_to_other_views()
                    self.selection_changed.emit()
        self.context_menu_requested.emit(self.icon_view.list.viewport().mapToGlobal(pos))

    def _on_gallery_context_menu(self, pos: QPoint) -> None:
        item = self.gallery_view._thumbs.itemAt(pos)
        if item is not None:
            meta = item.data(ROLE_META)
            if isinstance(meta, dict):
                path = str(meta.get("path") or "")
                if path and path not in self.selected_paths:
                    self.selected_paths = [path]
                    self._sync_selection_to_other_views()
                    self.selection_changed.emit()
        self.context_menu_requested.emit(self.gallery_view._thumbs.viewport().mapToGlobal(pos))

    def _on_column_context_menu(self, global_pos: QPoint, path: str) -> None:
        if path and path in self._path_to_entry and path not in self.selected_paths:
            self.selected_paths = [path]
            self._sync_selection_to_other_views()
            self.selection_changed.emit()
        self.context_menu_requested.emit(global_pos)

    def _cached_entry_by_path(self, path: str) -> FileEntry | None:
        direct = self._path_to_entry.get(path)
        if direct:
            return direct
        for entries in self._directory_cache.values():
            for entry in entries:
                if entry.path == path:
                    return entry
        return None

    def _on_sort_changed(self, column: int, order: Qt.SortOrder) -> None:
        self.sort_column = column
        self.sort_order = order
        self._render_views()

    def _emit_path(self) -> None:
        self.navigate_requested.emit(self.path_edit.text().strip())

    def _emit_back(self) -> None:
        if self._nav_index <= 0:
            return
        self._nav_index -= 1
        self._history_jump_pending = True
        self._update_nav_buttons()
        self.navigate_requested.emit(self._nav_history[self._nav_index])

    def _emit_forward(self) -> None:
        if self._nav_index < 0 or self._nav_index >= len(self._nav_history) - 1:
            return
        self._nav_index += 1
        self._history_jump_pending = True
        self._update_nav_buttons()
        self.navigate_requested.emit(self._nav_history[self._nav_index])

    def _record_navigation(self, path: str) -> None:
        if not path:
            self._update_nav_buttons()
            return
        if self._nav_index < 0 or not self._nav_history:
            self._nav_history = [path]
            self._nav_index = 0
            self._history_jump_pending = False
            self._update_nav_buttons()
            return

        current = self._nav_history[self._nav_index]
        if path == current:
            self._history_jump_pending = False
            self._update_nav_buttons()
            return

        if self._history_jump_pending and 0 <= self._nav_index < len(self._nav_history):
            self._nav_history[self._nav_index] = path
        else:
            self._nav_history = self._nav_history[: self._nav_index + 1]
            self._nav_history.append(path)
            self._nav_index = len(self._nav_history) - 1

        self._history_jump_pending = False
        self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        can_back = self._nav_index > 0
        can_forward = self._nav_index >= 0 and self._nav_index < len(self._nav_history) - 1
        self.back_button.setEnabled(can_back)
        self.forward_button.setEnabled(can_forward)

    def _on_path_context_menu(self, pos: QPoint) -> None:
        menu = self.path_edit.createStandardContextMenu()
        menu.addSeparator()
        go_action = menu.addAction("Go (cd)")
        go_action.triggered.connect(self._emit_path)

        if self.kind == "local":
            open_action = menu.addAction("Open in Finder")
            open_action.triggered.connect(self._open_current_local_path)

            pick_action = menu.addAction("Open Folder...")
            pick_action.triggered.connect(self._choose_local_folder)

        menu.exec(self.path_edit.mapToGlobal(pos))

    def _open_current_local_path(self) -> None:
        if self.kind != "local":
            return
        current = self.path_edit.text().strip() or self.current_path
        if not current:
            return
        target = Path(current).expanduser()
        if not target.exists():
            return
        folder = target if target.is_dir() else target.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _choose_local_folder(self) -> None:
        if self.kind != "local":
            return
        base = self.path_edit.text().strip() or self.current_path or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Open Folder", str(Path(base).expanduser()))
        if not chosen:
            return
        self.path_edit.setText(chosen)
        self.navigate_requested.emit(chosen)

    def _parent_of(self, path: str) -> str:
        if self.kind == "remote":
            parent = posixpath.dirname(path.rstrip("/"))
            return parent if parent else "/"
        parent = str(Path(path).parent)
        return parent if parent else "/"

    def _scroll_state(self, mode: str) -> tuple[int, int]:
        if mode == "icon":
            return self.icon_view.scroll_state()
        if mode == "list":
            return self.list_view.scroll_state()
        if mode == "column":
            return self.column_view.scroll_state()
        return self.gallery_view.scroll_state()

    def _restore_scroll_state(self, mode: str) -> None:
        state = self._scroll_positions.get(mode, (0, 0))
        if mode == "icon":
            self.icon_view.restore_scroll_state(state)
        elif mode == "list":
            self.list_view.restore_scroll_state(state)
        elif mode == "column":
            self.column_view.restore_scroll_state(state)
        else:
            self.gallery_view.restore_scroll_state(state)

    def _animate_switch(self, widget: QWidget) -> None:
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", widget)
        animation.setDuration(130)
        animation.setStartValue(0.25)
        animation.setEndValue(1.0)

        def clear_effect() -> None:
            widget.setGraphicsEffect(None)

        animation.finished.connect(clear_effect)
        animation.start()
        self._fade_animation = animation
