"""Reusable widgets and helpers for the desktop app."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .api import ApiError
from .theme import STATUS_COLORS


# ---------------------------------------------------------------- background calls
class _Signals(QObject):
    """Lives in the GUI thread; its slots run the callbacks there."""

    done = Signal(object)
    failed = Signal(object)

    def __init__(self, on_ok, on_err, on_finally):
        super().__init__()
        self._on_ok, self._on_err, self._on_finally = on_ok, on_err, on_finally
        self.done.connect(self._handle_done)
        self.failed.connect(self._handle_failed)

    @Slot(object)
    def _handle_done(self, result):
        try:
            if self._on_ok:
                self._on_ok(result)
        finally:
            self._finish()

    @Slot(object)
    def _handle_failed(self, error):
        try:
            if self._on_err:
                self._on_err(error)
        finally:
            self._finish()

    def _finish(self):
        if self._on_finally:
            self._on_finally()
        _ACTIVE.discard(self)


class _Job(QRunnable):
    def __init__(self, fn, signals: _Signals):
        super().__init__()
        self.fn, self.signals = fn, signals

    def run(self):
        try:
            result = self.fn()
        except ApiError as exc:
            self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            self.signals.failed.emit(ApiError(f"Unexpected error: {exc}"))
        else:
            self.signals.done.emit(result)


_ACTIVE: set[_Signals] = set()


def run_async(fn: Callable, on_ok=None, on_err=None, on_finally=None) -> None:
    """Run a blocking API call off the GUI thread so the window never freezes."""
    signals = _Signals(on_ok, on_err, on_finally)
    _ACTIVE.add(signals)
    QThreadPool.globalInstance().start(_Job(fn, signals))


# ---------------------------------------------------------------- layout helpers
def card(parent: Optional[QWidget] = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(10)
    return frame, layout


def label(text: str = "", name: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def button(text: str, name: str = "", on_click: Optional[Callable] = None) -> QPushButton:
    btn = QPushButton(text)
    if name:
        btn.setObjectName(name)
    btn.setCursor(Qt.PointingHandCursor)
    if on_click:
        btn.clicked.connect(on_click)
    return btn


def scroll_page(content: QWidget, max_width: Optional[int] = None) -> QScrollArea:
    """Wrap content in a scroll area so nothing is clipped on small screens
    or when the on-screen keyboard covers part of the window."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    holder = QWidget()
    outer = QHBoxLayout(holder)
    outer.setContentsMargins(16, 16, 16, 16)
    if max_width:
        content.setMaximumWidth(max_width)
        content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        outer.addStretch(1)
        col = QVBoxLayout()
        col.addWidget(content)
        col.addStretch(1)
        outer.addLayout(col, 100)
        outer.addStretch(1)
    else:
        outer.addWidget(content)
    area.setWidget(holder)
    return area


class Field(QWidget):
    """A labelled input."""

    def __init__(self, title: str, placeholder: str = "", password: bool = False, hint: str = ""):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(label(title, "FieldLabel"))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        if password:
            self.edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.edit)
        if hint:
            layout.addWidget(label(hint, "Muted"))

    def text(self) -> str:
        return self.edit.text()

    def value(self) -> str:
        return self.edit.text().strip()

    def clear(self) -> None:
        self.edit.clear()


def show_password_toggle(*fields: Field) -> QCheckBox:
    box = QCheckBox("Show password")

    def toggle(on):
        for f in fields:
            f.edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)

    box.toggled.connect(toggle)
    return box


class Message(QLabel):
    """Inline status message (error / success / info)."""

    def __init__(self):
        super().__init__()
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hide()

    def _show(self, kind: str, text: str):
        self.setObjectName(kind)
        self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()

    def error(self, text: str):
        self._show("Error", text)

    def success(self, text: str):
        self._show("Success", text)

    def info(self, text: str):
        self._show("Info", text)

    def clear(self):
        self.hide()
        self.setText("")


class BusyButton:
    """Disables a button and changes its text while a call is in flight."""

    def __init__(self, btn: QPushButton, busy_text: str):
        self.btn, self.busy_text, self.idle_text = btn, busy_text, btn.text()

    def start(self):
        self.btn.setEnabled(False)
        self.btn.setText(self.busy_text)

    def stop(self):
        self.btn.setEnabled(True)
        self.btn.setText(self.idle_text)


# ---------------------------------------------------------------- formatting
def fmt_time(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return value
    return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def status_item(status: str) -> QTableWidgetItem:
    item = QTableWidgetItem(status.capitalize())
    fg, bg = STATUS_COLORS.get(status, ("#5E6B70", "#F3F5F4"))
    from PySide6.QtGui import QBrush, QColor

    item.setForeground(QBrush(QColor(fg)))
    item.setBackground(QBrush(QColor(bg)))
    return item


def make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setSelectionMode(QTableWidget.SingleSelection)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.setWordWrap(False)
    return table


def fill_table(table: QTableWidget, rows: list[list], data: list) -> None:
    """Fill a table; each row keeps its source object in column 0's UserRole."""
    table.setRowCount(0)
    for r, (cells, obj) in enumerate(zip(rows, data)):
        table.insertRow(r)
        for c, cell in enumerate(cells):
            item = cell if isinstance(cell, QTableWidgetItem) else QTableWidgetItem("" if cell is None else str(cell))
            if c == 0:
                item.setData(Qt.UserRole, obj)
            table.setItem(r, c, item)
    table.resizeColumnsToContents()


def selected_object(table: QTableWidget):
    rows = table.selectionModel().selectedRows()
    if not rows:
        return None
    item = table.item(rows[0].row(), 0)
    return item.data(Qt.UserRole) if item else None


# ---------------------------------------------------------------- dialogs
class CodeDialog(QDialog):
    """Shows a one-time code once, with a copy button."""

    def __init__(self, parent: QWidget, title: str, intro: str, code: str, footer: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(min(460, parent.window().width() - 40) if parent else 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(label(title, "Section"))
        layout.addWidget(label(intro))
        code_lbl = QLabel(code)
        code_lbl.setObjectName("Code")
        code_lbl.setAlignment(Qt.AlignCenter)
        code_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(code_lbl)
        layout.addWidget(label(footer, "Muted"))
        row = QHBoxLayout()
        copy = button("Copy code")
        copied = label("", "Muted", wrap=False)

        def do_copy():
            QGuiApplication.clipboard().setText(code)
            copied.setText("Copied")

        copy.clicked.connect(do_copy)
        row.addWidget(copy)
        row.addWidget(copied)
        row.addStretch(1)
        row.addWidget(button("Done", "Primary", self.accept))
        layout.addLayout(row)
