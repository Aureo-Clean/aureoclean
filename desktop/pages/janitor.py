"""Janitor screens: live dashboard (first), alerts, and profile with SMS settings.

Laid out for the 7-inch 800x480 display: the profile tab is two columns
(details | SMS) so nothing needs scrolling."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QTabWidget, QVBoxLayout, QWidget

from ..widgets import button, card, fmt_time, label, run_async, scroll_page
from .common import SmsCard, TopBar, change_password_button

TAB_DASHBOARD, TAB_ALERTS, TAB_PROFILE = 0, 1, 2


class JanitorDashboard(QWidget):
    def __init__(self, nav):
        super().__init__()
        self.nav = nav
        self.api = nav.api
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = TopBar("AureoClean", nav.logout)
        root.addWidget(self.topbar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- Dashboard tab: the shared Arduino dashboard is placed here on sign-in.
        self.dash_tab = QWidget()
        dl = QVBoxLayout(self.dash_tab)
        dl.setContentsMargins(0, 0, 0, 0)
        self.tabs.addTab(self.dash_tab, "Dashboard")

        # ---- Alerts tab
        alerts_page = QWidget()
        al = QVBoxLayout(alerts_page)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        self.sms_nudge, nb = card()
        row = QHBoxLayout()
        row.addWidget(label("Get restroom alerts by text — add your mobile number.", wrap=True), 1)
        row.addWidget(button("Add number", "Gold", lambda: self.tabs.setCurrentIndex(TAB_PROFILE)))
        nb.addLayout(row)
        al.addWidget(self.sms_nudge)
        alerts, ab = card()
        ab.addWidget(label("Restroom alerts", "Section"))
        self.alerts_body = label("", "Muted")
        ab.addWidget(self.alerts_body)
        al.addWidget(alerts)
        al.addStretch(1)
        self.tabs.addTab(scroll_page(alerts_page), "Alerts")

        # ---- Profile tab: two columns
        profile = QWidget()
        grid = QGridLayout(profile)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)

        info, ib = card()
        self.greeting = label("", "Section")
        ib.addWidget(self.greeting)
        details = QGridLayout()
        details.setHorizontalSpacing(12)
        details.setVerticalSpacing(5)
        self.fields = {}
        for r, (key, title) in enumerate([("fullName", "Name"), ("username", "Username"),
                                          ("email", "Email"), ("createdAt", "Member since")]):
            details.addWidget(label(title, "FieldLabel", wrap=False), r, 0)
            value = label("")
            details.addWidget(value, r, 1)
            self.fields[key] = value
        details.setColumnStretch(1, 1)
        ib.addLayout(details)
        ib.addSpacing(4)
        ib.addWidget(change_password_button(self.api, self))
        ib.addStretch(1)

        self.sms = SmsCard(self.api, "Restroom alerts by text, even away from this screen.")
        grid.addWidget(info, 0, 0)
        grid.addWidget(self.sms, 0, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 6)
        grid.setRowStretch(1, 1)
        self.tabs.addTab(scroll_page(profile), "My profile")
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, index):
        if index == TAB_PROFILE:
            self.sms.message.clear()
        if index in (TAB_ALERTS, TAB_PROFILE):
            self.load()

    def enter(self, user: dict):
        self.topbar.set_user(user)
        first = (user.get("fullName") or user.get("username") or "").split(" ")[0]
        self.greeting.setText(f"Hello, {first}")
        self.sms.message.clear()
        self.nav.dashboard.attach_to(self.dash_tab)
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(TAB_DASHBOARD)
        self.tabs.blockSignals(False)
        self.load()

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
