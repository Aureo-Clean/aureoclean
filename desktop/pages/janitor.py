"""Janitor screens: live dashboard (shown first, automatically), alerts, profile and SMS settings."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QTabWidget, QVBoxLayout, QWidget

from ..widgets import button, card, fmt_time, label, run_async, scroll_page
from .common import ChangePasswordCard, SmsCard, TopBar
from .dashboard import DashboardPanel

TAB_DASHBOARD, TAB_ALERTS, TAB_PROFILE = 0, 1, 2


class JanitorDashboard(QWidget):
    def __init__(self, nav):
        super().__init__()
        self.nav = nav
        self.api = nav.api
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = TopBar("AureoClean · Janitor", nav.logout)
        root.addWidget(self.topbar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- Dashboard tab (first, so janitors see it straight after signing in)
        self.dashboard = DashboardPanel("janitor")
        self.tabs.addTab(self.dashboard, "Dashboard")

        # ---- Alerts tab
        home = QWidget()
        hl = QVBoxLayout(home)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(12)
        self.greeting = label("", "PageTitle")
        hl.addWidget(self.greeting)

        # Nudge to add a number for SMS alerts (hidden once verified).
        self.sms_nudge, nb = card()
        row = QHBoxLayout()
        row.addWidget(label("Get restroom alerts by text message — add your mobile number.", wrap=True), 1)
        row.addWidget(button("Add number", "Gold", lambda: self.tabs.setCurrentIndex(TAB_PROFILE)))
        nb.addLayout(row)
        hl.addWidget(self.sms_nudge)

        alerts, ab = card()
        ab.addWidget(label("Restroom alerts", "Section"))
        self.alerts_body = label("", "Muted")
        ab.addWidget(self.alerts_body)
        hl.addWidget(alerts)

        hl.addStretch(1)
        self.tabs.addTab(scroll_page(home), "Alerts")

        # ---- Profile tab
        profile = QWidget()
        pl = QVBoxLayout(profile)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(12)
        info, ib = card()
        ib.addWidget(label("My profile", "Section"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        self.fields = {}
        for row_i, (key, title) in enumerate([("fullName", "Full name"), ("username", "Username"),
                                               ("email", "Email"), ("createdAt", "Member since")]):
            grid.addWidget(label(title, "FieldLabel", wrap=False), row_i, 0)
            value = label("")
            grid.addWidget(value, row_i, 1)
            self.fields[key] = value
        grid.setColumnStretch(1, 1)
        ib.addLayout(grid)
        info.setMaximumWidth(460)
        pl.addWidget(info)
        self.sms = SmsCard(self.api, "Get restroom alerts by text message, even when you're away from this screen. "
                                     "Add your number and confirm it with the code we text you.")
        pl.addWidget(self.sms)
        pl.addWidget(ChangePasswordCard(self.api))
        pl.addStretch(1)
        self.tabs.addTab(scroll_page(profile), "My profile")
        self.tabs.currentChanged.connect(lambda i: i == TAB_ALERTS and self.load())

    def enter(self, user: dict):
        self.topbar.set_user(user)
        first = (user.get("fullName") or user.get("username") or "").split(" ")[0]
        self.greeting.setText(f"Hello, {first}")
        self.sms.message.clear()
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(TAB_DASHBOARD)
        self.tabs.blockSignals(False)
        self.load()

        def show(links):
            url = next((link["url"] for link in links if link.get("key") == "dashboard"), "")
            self.dashboard.show_url(url)

        run_async(self.api.links, show, lambda e: self.dashboard.show_url(""))

    def leave(self):
        self.dashboard.leave()

    def load(self):
        self.alerts_body.setText("Loading…")

        def ok(data):
            p = data["profile"]
            for key, lbl in self.fields.items():
                value = p.get(key)
                lbl.setText(fmt_time(value) if key == "createdAt" else (value or "—"))
            self.sms.load(p)
            self.sms_nudge.setVisible(not p.get("smsPhone"))
            notes = data.get("notifications") or []
            if notes:
                self.alerts_body.setText("\n".join(f"• {n.get('title', n)}" for n in notes))
            else:
                self.alerts_body.setText(data.get("message", "No alerts right now."))

        def err(e):
            if not self.nav.handle_session_error(e):
                self.alerts_body.setText(e.message)

        run_async(self.api.janitor_overview, ok, err)
