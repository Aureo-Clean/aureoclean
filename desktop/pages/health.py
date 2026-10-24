"""System health card (top of the Tools tab): one coloured line per check."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel

from ..theme import AMBER, DANGER, GREEN, MUTED
from ..widgets import button, card, label, run_async

COLORS = {"ok": GREEN, "warn": AMBER, "fail": DANGER, "off": MUTED}
SUMMARY = {"ok": "✓ Everything is working", "warn": "⚠ Needs attention", "fail": "✗ Something is broken"}


class HealthCard:
    def __init__(self, api, on_status=None):
        self.api = api
        self.on_status = on_status  # called with the overall status ("ok"/"warn"/"fail")
        self.frame, body = card()
        head = QHBoxLayout()
        head.addWidget(label("System health", "Section"))
        self.summary = label("", wrap=False)
        head.addWidget(self.summary)
        head.addStretch(1)
        self.refresh_btn = button("Refresh", "Small", lambda: self.refresh())
        self.balance_btn = button("Check load", "Small", lambda: self.refresh(check_balance=True))
        head.addWidget(self.refresh_btn)
        head.addWidget(self.balance_btn)
        body.addLayout(head)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnStretch(2, 1)
        body.addLayout(self.grid)
        self.note = label("", "Muted")
        body.addWidget(self.note)

    def _busy(self, on: bool):
        self.refresh_btn.setEnabled(not on)
        self.balance_btn.setEnabled(not on)

    def show(self, report: dict):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for row, c in enumerate(report.get("checks", [])):
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{COLORS.get(c['status'], MUTED)}; font-size:14px;")
            self.grid.addWidget(dot, row, 0)
            self.grid.addWidget(label(c["name"], "FieldLabel", wrap=False), row, 1)
            self.grid.addWidget(label(c["detail"], "Muted"), row, 2)
        status = report.get("status", "ok")
        self.summary.setText(SUMMARY.get(status, ""))
        self.summary.setStyleSheet(f"color:{COLORS.get(status, MUTED)}; font-weight:600;")
        self.note.setText("")
        if self.on_status:
            self.on_status(status)

    def refresh(self, check_balance: bool = False):
        self._busy(True)
        self.note.setText("Checking the SIM load with your network… (up to 20 s)" if check_balance else "Checking…")

        def err(e):
            self.note.setText(f"Couldn't check: {e.message}")

        run_async(lambda: self.api.system_health(check_balance), self.show, err, lambda: self._busy(False))
