"""Administrator console: requests, users, invites, audit log, account."""
from __future__ import annotations


from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..widgets import (
    CodeDialog,
    Field,
    Message,
    button,
    card,
    fill_table,
    fmt_time,
    label,
    make_table,
    run_async,
    scroll_page,
    selected_object,
    status_item,
)
from .common import ChangePasswordCard, LinkButton, SmsCard, TopBar

ACTION_LABELS = {
    "login": "Signed in",
    "janitor_request_submitted": "Janitor request submitted",
    "janitor_request_approved": "Request approved",
    "janitor_request_rejected": "Request rejected",
    "activation_code_reissued": "New activation code issued",
    "janitor_activated": "Janitor account activated",
    "admin_invite_created": "Admin invite created",
    "admin_invite_revoked": "Admin invite revoked",
    "admin_registered": "Admin registered with invite",
    "admin_bootstrapped": "First admin created (Pi tool)",
    "admin_linked_existing": "Existing account linked as admin",
    "user_enabled": "User enabled",
    "user_disabled": "User disabled",
    "password_changed": "Password changed",
    "sms_code_sent": "SMS verification code sent",
    "sms_phone_verified": "Mobile number verified",
    "sms_phone_removed": "Mobile number removed",
    "sms_alerts_on": "SMS alerts turned on",
    "sms_alerts_off": "SMS alerts turned off",
    "sms_test_sent": "Test SMS sent",
}


def _confirm(parent, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


class _Tab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.message = Message()
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 14, 16, 16)
        self.layout_.setSpacing(10)

    def fail(self, e):
        window = self.window()
        if hasattr(window, "handle_session_error") and window.handle_session_error(e):
            return
        self.message.error(e.message)


# ====================================================================== requests
class RequestsTab(_Tab):
    FILTERS = [("Pending", "pending"), ("Approved (awaiting activation)", "approved"),
               ("Activated", "activated"), ("Rejected", "rejected"), ("All", "")]

    def __init__(self, api, on_change):
        super().__init__(api)
        self.on_change = on_change
        bar = QHBoxLayout()
        bar.addWidget(label("Show", "FieldLabel", wrap=False))
        self.filter = QComboBox()
        for text, value in self.FILTERS:
            self.filter.addItem(text, value)
        self.filter.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.filter)
        bar.addStretch(1)
        bar.addWidget(button("Refresh", on_click=self.refresh))
        self.layout_.addLayout(bar)

        self.table = make_table(["Submitted", "Full name", "Username", "Email", "Status", "Note"])
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.layout_.addWidget(self.table, 1)
        self.layout_.addWidget(self.message)

        actions = QHBoxLayout()
        self.approve_btn = button("Approve", "Primary", self.approve)
        self.reissue_btn = button("Issue new code", on_click=self.reissue)
        self.reject_btn = button("Reject", "Danger", self.reject)
        for b in (self.approve_btn, self.reissue_btn, self.reject_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        self.layout_.addLayout(actions)
        self.update_buttons()

    def refresh(self):
        status = self.filter.currentData()

        def ok(rows):
            data = []
            for r in rows:
                note = ""
                if r["status"] == "approved":
                    note = "Code locked (too many tries)" if r.get("codeLocked") else (
                        "Code expired" if r.get("codeExpired") else f"Code expires {fmt_time(r.get('activationExpiresAt'))}")
                elif r["status"] == "rejected":
                    note = r.get("rejectReason") or ""
                elif r.get("reviewedBy"):
                    note = f"by {r['reviewedBy'].get('username')}"
                data.append([fmt_time(r.get("createdAt")), r.get("fullName"), r.get("username"), r.get("email"),
                             status_item(r["status"]), note])
            fill_table(self.table, data, rows)
            if not rows:
                self.message.info("No requests in this list.")
            else:
                self.message.clear()
            self.update_buttons()

        run_async(lambda: self.api.list_requests(status), ok, self.fail)

    def update_buttons(self):
        req = selected_object(self.table)
        status = req["status"] if req else None
        self.approve_btn.setEnabled(status == "pending")
        self.reissue_btn.setEnabled(status == "approved")
        self.reject_btn.setEnabled(status in ("pending", "approved"))

    def _show_code(self, result, req):
        CodeDialog(
            self,
            "Activation code",
            f"Give this code to {req.get('fullName')} (username “{req.get('username')}”) in person or by phone.",
            result["code"],
            f"It works once and expires {fmt_time(result.get('expiresAt'))}. "
            "It is shown only now — if it is lost, use “Issue new code”.",
        ).exec()

    def _after(self, result, req, show_code=True):
        if show_code:
            self._show_code(result, req)
        self.refresh()
        self.on_change()

    def approve(self):
        req = selected_object(self.table)
        if not req or not _confirm(self, "Approve request", f"Approve {req['fullName']} ({req['username']}) as a janitor?"):
            return
        run_async(lambda: self.api.approve(req["id"]), lambda r: self._after(r, req), self.fail)

    def reissue(self):
        req = selected_object(self.table)
        if not req or not _confirm(self, "Issue new code", "The old code will stop working. Continue?"):
            return
        run_async(lambda: self.api.reissue_code(req["id"]), lambda r: self._after(r, req), self.fail)

    def reject(self):
        req = selected_object(self.table)
        if not req:
            return
        reason, ok = QInputDialog.getText(self, "Reject request", f"Reason for rejecting {req['username']} (optional):")
        if not ok:
            return
        run_async(lambda: self.api.reject(req["id"], reason), lambda r: self._after(r, req, False), self.fail)


# ====================================================================== users
class UsersTab(_Tab):
    def __init__(self, api, on_change):
        super().__init__(api)
        self.on_change = on_change
        self.my_uid = None
        bar = QHBoxLayout()
        bar.addWidget(label("Everyone who can sign in to AureoClean.", "Muted"))
        bar.addStretch(1)
        bar.addWidget(button("Refresh", on_click=self.refresh))
        self.layout_.addLayout(bar)
        self.table = make_table(["Username", "Full name", "Email", "Role", "Status", "SMS alerts", "Last sign-in", "Created"])
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.layout_.addWidget(self.table, 1)
        self.layout_.addWidget(self.message)
        row = QHBoxLayout()
        self.toggle_btn = button("Disable account", "Danger", self.toggle)
        self.sms_btn = button("Send test SMS", on_click=self.test_sms)
        row.addWidget(self.toggle_btn)
        row.addWidget(self.sms_btn)
        row.addStretch(1)
        self.layout_.addLayout(row)
        self.update_buttons()

    def refresh(self):
        def ok(rows):
            def sms(u):
                if not u.get("smsPhone"):
                    return "No number"
                return f"{u['smsPhone']} · {'on' if u.get('smsAlerts') else 'off'}"

            data = [[u["username"], u.get("fullName"), u["email"], u["role"].capitalize(), status_item(u["status"]),
                     sms(u), fmt_time(u.get("lastLoginAt")), fmt_time(u.get("createdAt"))] for u in rows]
            fill_table(self.table, data, rows)
            self.message.clear()
            self.update_buttons()

        run_async(self.api.list_users, ok, self.fail)

    def update_buttons(self):
        user = selected_object(self.table)
        self.sms_btn.setEnabled(bool(user and user.get("smsPhone")))
        if not user or user["uid"] == self.my_uid:
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("Disable account")
            return
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText("Enable account" if user["status"] != "active" else "Disable account")

    def toggle(self):
        user = selected_object(self.table)
        if not user:
            return
        enable = user["status"] != "active"
        verb = "Enable" if enable else "Disable"
        extra = "" if enable else " They will be signed out and unable to sign in."
        if not _confirm(self, f"{verb} account", f"{verb} {user['username']}?{extra}"):
            return

        def ok(_):
            self.refresh()
            self.on_change()

        run_async(lambda: self.api.set_user_active(user["uid"], enable), ok, self.fail)


    def test_sms(self):
        user = selected_object(self.table)
        if not user or not _confirm(self, "Send test SMS", f"Send a test text message to {user['username']} ({user['smsPhone']})?"):
            return
        self.message.info("Sending… this can take up to a minute with a GSM modem.")
        run_async(lambda: self.api.test_sms(user["uid"]), lambda r: self.message.success(r["message"]), self.fail)


# ====================================================================== tools
class ToolsTab(_Tab):
    """Shortcuts to the Arduino IoT Cloud dashboard, Arduino Cloud and Firebase, plus SMS modem status."""

    def __init__(self, api):
        super().__init__(api)
        self.layout_.addWidget(label("Monitoring & cloud tools", "Section"))
        self.layout_.addWidget(label(
            "These open in the web browser. Access to each website is controlled by its own account "
            "(Arduino / Google), not by AureoClean — sign in with the project accounts there.", "Muted"))
        self.links_box = QVBoxLayout()
        self.links_box.setSpacing(10)
        self.layout_.addLayout(self.links_box)

        frame, body = card()
        body.addWidget(label("SMS modem", "Section"))
        self.sms_state = label("Not checked yet.", "Muted")
        body.addWidget(self.sms_state)
        row = QHBoxLayout()
        self.check_btn = button("Check SMS modem", on_click=self.check_sms)
        row.addWidget(self.check_btn)
        row.addStretch(1)
        body.addLayout(row)
        self.layout_.addWidget(frame)
        self.layout_.addWidget(self.message)
        self.layout_.addStretch(1)

    def refresh(self):
        def ok(links):
            while self.links_box.count():
                item = self.links_box.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for link in links:
                self.links_box.addWidget(LinkButton(link))
            if not any(l["key"] == "dashboard" for l in links):
                note, nb = card()
                nb.addWidget(label("Restroom monitoring dashboard", "Section"))
                nb.addWidget(label(
                    "Not set up yet. In Arduino Cloud open your dashboard, copy its link, and put it in "
                    "~/.config/aureoclean/aureoclean.env as AUREOCLEAN_DASHBOARD_URL=… then restart the app.", "Muted"))
                self.links_box.insertWidget(0, note)

        run_async(self.api.links, ok, self.fail)

    def check_sms(self):
        self.sms_state.setText("Checking… (up to a few seconds)")
        self.check_btn.setEnabled(False)

        def ok(st):
            mark = "✓" if st.get("ok") else "✗"
            mode = "Test mode (not sending)" if st.get("mode") == "log" else "GSM modem"
            self.sms_state.setText(f"{mark} {mode}: {st.get('detail', '')}")

        run_async(self.api.sms_status, ok, self.fail, lambda: self.check_btn.setEnabled(True))


# ====================================================================== invites
class InvitesTab(_Tab):
    def __init__(self, api):
        super().__init__(api)
        frame, body = card()
        body.addWidget(label("Invite another administrator", "Section"))
        body.addWidget(label(
            "Creates a one-time code. Locking it to an email address is recommended — "
            "the code then only works for that address.", "Muted"))
        grid = QGridLayout()
        self.email = Field("Lock to email (recommended)", "admin@example.com")
        grid.addWidget(self.email, 0, 0)
        hours_box = QWidget()
        hl = QVBoxLayout(hours_box)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        hl.addWidget(label("Valid for (hours)", "FieldLabel"))
        self.hours = QSpinBox()
        self.hours.setRange(1, 168)
        self.hours.setValue(24)
        hl.addWidget(self.hours)
        grid.addWidget(hours_box, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        body.addLayout(grid)
        body.addWidget(button("Create invite code", "Gold", self.create))
        self.layout_.addWidget(frame)

        bar = QHBoxLayout()
        bar.addWidget(label("Invite codes", "Section"))
        bar.addStretch(1)
        bar.addWidget(button("Refresh", on_click=self.refresh))
        self.layout_.addLayout(bar)
        self.table = make_table(["Created", "For email", "Status", "Expires", "Created by", "Used by"])
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.layout_.addWidget(self.table, 1)
        self.layout_.addWidget(self.message)
        row = QHBoxLayout()
        self.revoke_btn = button("Revoke invite", "Danger", self.revoke)
        row.addWidget(self.revoke_btn)
        row.addStretch(1)
        self.layout_.addLayout(row)
        self.update_buttons()

    def refresh(self):
        def ok(rows):
            data = [[fmt_time(i.get("createdAt")), i.get("email") or "Any email", status_item(i["status"]),
                     fmt_time(i.get("expiresAt")), (i.get("createdBy") or {}).get("username", "—"),
                     (i.get("usedBy") or {}).get("username", "—")] for i in rows]
            fill_table(self.table, data, rows)
            self.message.clear()
            self.update_buttons()

        run_async(self.api.list_invites, ok, self.fail)

    def update_buttons(self):
        inv = selected_object(self.table)
        self.revoke_btn.setEnabled(bool(inv and inv["status"] == "active"))

    def create(self):
        email, hours = self.email.value(), self.hours.value()

        def ok(result):
            self.email.clear()
            who = f"for {result['email']}" if result.get("email") else "(any email)"
            CodeDialog(
                self, "Admin invite code",
                f"Share this code only with the new administrator {who}.",
                result["code"],
                f"Single use. Expires {fmt_time(result.get('expiresAt'))}. It is shown only now.",
            ).exec()
            self.refresh()

        run_async(lambda: self.api.create_invite(email, hours), ok, self.fail)

    def revoke(self):
        inv = selected_object(self.table)
        if not inv or not _confirm(self, "Revoke invite", "This invite code will stop working. Continue?"):
            return
        run_async(lambda: self.api.revoke_invite(inv["id"]), lambda _: self.refresh(), self.fail)


# ====================================================================== audit
class AuditTab(_Tab):
    def __init__(self, api):
        super().__init__(api)
        bar = QHBoxLayout()
        bar.addWidget(label("Most recent 200 security events.", "Muted"))
        bar.addStretch(1)
        bar.addWidget(button("Refresh", on_click=self.refresh))
        self.layout_.addLayout(bar)
        self.table = make_table(["Time", "Event", "By", "Details"])
        self.layout_.addWidget(self.table, 1)
        self.layout_.addWidget(self.message)

    def refresh(self):
        def ok(rows):
            data = []
            for a in rows:
                actor = a.get("actor") or {}
                by = actor.get("username") or actor.get("type", "")
                if actor.get("type") and actor.get("type") != actor.get("username"):
                    by = f"{by} ({actor['type']})"
                details = ", ".join(f"{k}: {v}" for k, v in (a.get("details") or {}).items() if v not in (None, ""))
                data.append([fmt_time(a.get("at")), ACTION_LABELS.get(a["action"], a["action"]), by, details])
            fill_table(self.table, data, rows)
            self.message.clear()

        run_async(lambda: self.api.audit(200), ok, self.fail)


# ====================================================================== dashboard
class AdminDashboard(QWidget):
    def __init__(self, nav):
        super().__init__()
        self.nav = nav
        self.api = nav.api
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = TopBar("AureoClean · Admin console", nav.logout)
        root.addWidget(self.topbar)

        # Overview stat tiles.
        stats = QWidget()
        grid = QGridLayout(stats)
        grid.setContentsMargins(16, 14, 16, 0)
        grid.setSpacing(10)
        self.stat_labels = {}
        tiles = [("pendingRequests", "Pending requests"), ("awaitingActivation", "Awaiting activation"),
                 ("activeJanitors", "Active janitors"), ("activeAdmins", "Active admins")]
        for i, (key, title) in enumerate(tiles):
            frame, body = card()
            body.setContentsMargins(16, 10, 16, 10)
            body.setSpacing(0)
            value = label("–", "StatValue", wrap=False)
            body.addWidget(value)
            body.addWidget(label(title, "Muted"))
            self.stat_labels[key] = value
            grid.addWidget(frame, 0, i)
        root.addWidget(stats)

        self.tabs = QTabWidget()
        self.requests = RequestsTab(self.api, self.refresh_overview)
        self.users = UsersTab(self.api, self.refresh_overview)
        self.invites = InvitesTab(self.api)
        self.audit = AuditTab(self.api)
        account = QWidget()
        al = QVBoxLayout(account)
        al.setContentsMargins(16, 14, 16, 16)
        self.sms = SmsCard(self.api, "Admins receive critical alerts (e.g. fire / emergency stop) and system "
                                     "notices by SMS once the alert service is connected. Add your number here.")
        al.addWidget(self.sms)
        al.addWidget(ChangePasswordCard(self.api))
        al.addStretch(1)
        self.tabs.addTab(self.requests, "Janitor requests")
        self.tabs.addTab(self.users, "Users")
        self.tabs.addTab(scroll_page(self.invites), "Admin invites")
        self.tools = ToolsTab(self.api)
        self.tabs.addTab(self.audit, "Audit log")
        self.tabs.addTab(scroll_page(self.tools), "Tools")
        self.tabs.addTab(scroll_page(account), "My account")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)

    def _tab_changed(self, index):
        refreshers = {0: self.requests.refresh, 1: self.users.refresh, 2: self.invites.refresh, 3: self.audit.refresh,
                      4: self.tools.refresh, 5: self.load_account}
        if index in refreshers:
            refreshers[index]()

    def enter(self, user: dict):
        self.topbar.set_user(user)
        self.users.my_uid = user["uid"]
        self.tabs.setCurrentIndex(0)
        self.requests.filter.setCurrentIndex(0)
        self.refresh_overview()
        self.requests.refresh()

    def load_account(self):
        run_async(self.api.me, self.sms.load, lambda e: self.nav.handle_session_error(e))

    def refresh_overview(self):
        def ok(ov):
            for key, lbl in self.stat_labels.items():
                lbl.setText(str(ov.get(key, "–")))

        run_async(self.api.admin_overview, ok, lambda e: self.nav.handle_session_error(e))
