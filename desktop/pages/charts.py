"""Reports dashboard: key numbers, simple bar charts, exports.

Charts are drawn with QPainter (no extra Qt module needed), sized for the
7-inch 800x480 screen."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..exporting import CATEGORY_LABELS, kpis, summary_html, write_pdf, write_xlsx
from ..theme import BLUE, GREEN, INK, LINE, MUTED, NAVY
from ..widgets import Message, button, card, label, run_async


class BarChart(QWidget):
    """Horizontal bars: label on the left, value on the right."""

    def __init__(self, title: str, color: str = BLUE, rows: int = 6):
        super().__init__()
        self.title, self.color, self.rows = title, color, rows
        self.items: list[tuple[str, float]] = []
        self.setMinimumHeight(34 + rows * 22)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_items(self, items):
        self.items = list(items)[: self.rows]
        self.update()

    def paintEvent(self, _):  # noqa: N802 (Qt name)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        bold = QFont(self.font())
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor(NAVY))
        p.drawText(QRectF(0, 0, w, 22), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        p.setFont(self.font())
        if not self.items:
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(0, 26, w, 22), Qt.AlignLeft | Qt.AlignVCenter, "No reports in this period.")
            return
        biggest = max(v for _, v in self.items) or 1
        label_w, value_w = min(150, w * 0.38), 34
        bar_x, bar_w = label_w + 6, max(10, w - label_w - value_w - 12)
        for i, (name, value) in enumerate(self.items):
            y = 28 + i * 22
            p.setPen(QColor(INK))
            p.drawText(QRectF(0, y, label_w, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(str(name), Qt.ElideRight, int(label_w)))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(LINE))
            p.drawRoundedRect(QRectF(bar_x, y + 3, bar_w, 12), 4, 4)
            p.setBrush(QColor(self.color))
            p.drawRoundedRect(QRectF(bar_x, y + 3, max(4, bar_w * value / biggest), 12), 4, 4)
            p.setPen(QColor(INK))
            p.drawText(QRectF(w - value_w, y, value_w, 18), Qt.AlignRight | Qt.AlignVCenter, f"{value:g}")


class HourChart(QWidget):
    """24 small columns: reports per hour of the day."""

    def __init__(self):
        super().__init__()
        self.counts = [0] * 24
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_counts(self, by_hour: dict):
        self.counts = [int(by_hour.get(str(h), by_hour.get(h, 0)) or 0) for h in range(24)]
        self.update()

    def paintEvent(self, _):  # noqa: N802 (Qt name)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bold = QFont(self.font())
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor(NAVY))
        p.drawText(QRectF(0, 0, w, 22), Qt.AlignLeft | Qt.AlignVCenter, "Busiest hours")
        small = QFont(self.font())
        small.setPointSizeF(max(6.5, small.pointSizeF() * 0.75))
        p.setFont(small)
        top, bottom = 28, h - 18
        biggest = max(self.counts) or 1
        col = w / 24
        for hour, n in enumerate(self.counts):
            x = hour * col
            bar_h = (bottom - top) * n / biggest
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(GREEN if n == biggest and n else BLUE))
            if n:
                p.drawRoundedRect(QRectF(x + col * 0.18, bottom - bar_h, col * 0.64, bar_h), 2, 2)
            if hour % 3 == 0:
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(x - 4, bottom + 2, col * 3, 14), Qt.AlignLeft, f"{hour:02d}")


class ReportsDashboard(QWidget):
    PERIODS = [("Last 7 days", 7), ("Last 30 days", 30), ("Last 90 days", 90)]

    def __init__(self, api, on_back):
        super().__init__()
        self.api = api
        self.data = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(button("← List", "Small", on_back))
        self.period = QComboBox()
        for text, days in self.PERIODS:
            self.period.addItem(text, days)
        self.period.setCurrentIndex(1)
        self.period.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.period)
        bar.addStretch(1)
        self.xlsx_btn = button("Export Excel", "Small", self.export_xlsx)
        self.pdf_btn = button("Export PDF", "Small", self.export_pdf)
        bar.addWidget(self.xlsx_btn)
        bar.addWidget(self.pdf_btn)
        root.addLayout(bar)

        tiles = QGridLayout()
        tiles.setSpacing(6)
        self.tile_values = []
        for i in range(6):
            frame, body = card()
            body.setContentsMargins(8, 6, 8, 6)
            body.setSpacing(0)
            value = label("–", "StatValue", wrap=False)
            name = label("", "Muted")
            body.addWidget(value)
            body.addWidget(name)
            self.tile_values.append((value, name))
            tiles.addWidget(frame, i // 3, i % 3)
        root.addLayout(tiles)

        charts = QGridLayout()
        charts.setSpacing(8)
        self.by_place = BarChart("Reports by restroom")
        self.by_issue = BarChart("Most common problems", GREEN)
        self.by_type = BarChart("By type", NAVY, rows=6)
        self.hours = HourChart()
        for i, chart in enumerate((self.by_place, self.by_issue, self.by_type, self.hours)):
            frame, body = card()
            body.setContentsMargins(10, 8, 10, 8)
            body.addWidget(chart)
            charts.addWidget(frame, i // 2, i % 2)
        root.addLayout(charts)

        extra, eb = card()
        eb.setContentsMargins(10, 8, 10, 8)
        eb.addWidget(label("Recurring problems & janitors", "Section"))
        self.recurring = label("", "Muted")
        eb.addWidget(self.recurring)
        root.addWidget(extra)
        self.message = Message()
        root.addWidget(self.message)
        root.addStretch(1)

    def refresh(self):
        days = self.period.currentData()

        def ok(data):
            self.data = data
            s = data["summary"]
            for (value, name), (k, v) in zip(self.tile_values, kpis(s)):
                value.setText(v)
                name.setText(k)
            self.by_place.set_items(s.get("byLocation", {}).items())
            self.by_issue.set_items(s.get("byIssue", {}).items())
            self.by_type.set_items([(CATEGORY_LABELS.get(k, k), v) for k, v in s.get("byCategory", {}).items()])
            self.hours.set_counts(s.get("byHour", {}))
            lines = [f"• {p['issue']} at {p['location']}: {p['count']}×" for p in s.get("recurringProblems", [])[:4]]
            lines += [f"• {n}: {v['handled']} jobs" + (f", median fix {v['medianMinutesToResolve']} min"
                                                         if v.get("medianMinutesToResolve") is not None else "")
                      for n, v in list(s.get("byJanitor", {}).items())[:4]]
            self.recurring.setText("\n".join(lines) or "Nothing recurring yet.")
            self.message.clear()

        run_async(lambda: self.api.reports_summary(days), ok, lambda e: self.message.error(e.message))

    def _ask_path(self, ext: str, what: str) -> str:
        default = Path.home() / "Documents" / f"AureoClean-reports-{datetime.now():%Y%m%d}.{ext}"
        default.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, f"Save {what}", str(default), f"{what} (*.{ext})")
        return path

    def export_xlsx(self):
        if not self.data:
            return
        path = self._ask_path("xlsx", "Excel file")
        if not path:
            return
        try:
            write_xlsx(path, self.data["summary"], self.data["records"], self.data["days"])
            self.message.success(f"Saved {path}")
        except ImportError:
            self.message.error("Excel export needs the openpyxl package: run Update AureoClean.")
        except OSError as exc:
            self.message.error(f"Couldn't save: {exc}")

    def export_pdf(self):
        if not self.data:
            return
        path = self._ask_path("pdf", "PDF")
        if not path:
            return
        try:
            write_pdf(path, summary_html(self.data["summary"], self.data["days"]))
            self.message.success(f"Saved {path}")
        except OSError as exc:
            self.message.error(f"Couldn't save: {exc}")
