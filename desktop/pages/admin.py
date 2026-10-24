"""Administrator console: dashboard, requests, users, invites, audit log, tools, account.
Laid out for the 7-inch 800x480 display (no stat tiles; counts live in the tab names)."""
from __future__ import annotations

import sys

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QStackedWidget,
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
from .charts import ReportsDashboard
from .health import HealthCard
from .common import LinkButton, SmsCard, TopBar, change_password_button, lock_pin_button

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
    "user_deleted": "Account deleted",
    "sms_reply_received": "SMS reply received",
    "sms_reply_simulated": "SMS reply simulated",
    "sms_from_unknown": "SMS from unknown number",
    "report_escalated": "Report escalated (no reply)",
    "password_changed": "Password changed",
    "sms_code_sent": "SMS verification code sent",
    "sms_phone_verified": "Mobile number verified",
    "sms_phone_removed": "Mobile number removed",
    "sms_alerts_on": "SMS alerts turned on",
    "sms_alerts_off": "SMS alerts turned off",
    "sms_test_sent": "Test SMS sent",
    "sms_modem_enabled": "SMS switched to GSM modem",
    "sms_test_mode": "SMS switched to test mode",
    "web_signup_received": "Website sign-up received",
    "web_signup_rejected": "Website sign-up rejected",
    "janitor_contact_approved": "Janitor added as SMS contact",
    "report_created": "Report created",
    "report_updated": "Report updated",
    "public_report_dropped": "Public report dropped (too many)",
    "admin_email_verified": "Admin email verified",
    "verification_email_sent": "Verification email sent",
}


def _confirm(parent, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


class _Tab(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.message = Message()
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(8, 6, 8, 8)
        self.layout_.setSpacing(6)

    def fail(self, e):
        window = self.window()
        if hasattr(window, "handle_session_error") and window.handle_session_error(e):
            return
        self.message.error(e.message)


# ====================================================================== requests
class RequestsTab(_Tab):
    FILTERS = [("Pending", "pending"), ("Awaiting activation", "approved"),
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
        self.summary = label("", "Muted", wrap=False)
        bar.addWidget(self.summary, 1)
        bar.addWidget(button("Refresh", "Small", self.refresh))
        self.layout_.addLayout(bar)

        self.table = make_table(["Submitted", "Full name", "Mobile / email", "From", "Status", "Note"])
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
                contact = r.get("phone") or r.get("email") or "—"
                source = "Website" if r.get("source") == "web" else "App"
                data.append([fmt_time(r.get("createdAt")), r.get("fullName"), contact, source,
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
        if show_code and result.get("code"):
            self._show_code(result, req)
        elif result.get("message"):
            self.message.success(result["message"])
        self.refresh()
        self.on_change()

    def approve(self):
        req = selected_object(self.table)
        if not req:
            return
        if req.get("source") == "web":
            question = (f"Approve {req['fullName']} as a janitor?\n\nThey'll become an SMS contact and get a "
                        f"welcome text at {req.get('phone')}.")
        else:
            question = f"Approve {req['fullName']} ({req['username']}) as a janitor?"
        if not _confirm(self, "Approve request", question):
            return
        self.message.info("Approving… (sending the welcome text can take up to a minute)")
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


# ====================================================================== reports
REPORT_TYPES = [("Emergency", "emergency"), ("Broken facility", "facility"), ("Supplies", "supplies"),
                ("Cleanliness", "cleanliness"), ("System problem", "system"), ("Other", "other")]
PRIORITIES = [("Critical", "critical"), ("High", "high"), ("Normal", "normal"), ("Low", "low")]
STATUS_LABELS = {"open": "Open", "in_progress": "In progress", "resolved": "Resolved"}
TYPE_LABELS = dict((v, k) for k, v in REPORT_TYPES)


class NewReportDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("New report")
        self.setModal(True)
        body = QVBoxLayout(self)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(7)
        body.addWidget(label("New report", "Section"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        def combo(items):
            c = QComboBox()
            for text, value in items:
                c.addItem(text, value)
            return c

        self.category = combo(REPORT_TYPES)
        self.priority = combo(PRIORITIES)
        self.notify = combo([("Don't text anyone", "none"), ("Text the janitors", "janitors"),
                             ("Text everyone with SMS on", "everyone")])
        self.title_f = Field("What's wrong? *", "e.g. Broken faucet, Fire alarm, Sensor offline")
        self.location = Field("Where", "e.g. 2F Male CR")
        for col, (text, widget) in enumerate([("Type", self.category), ("Priority", self.priority)]):
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(label(text, "FieldLabel"))
            box.addWidget(widget)
            grid.addLayout(box, 0, col)
        grid.addWidget(self.title_f, 1, 0)
        grid.addWidget(self.location, 1, 1)
        body.addLayout(grid)
        body.addWidget(label("Details", "FieldLabel"))
        self.details = QPlainTextEdit()
        self.details.setPlaceholderText("Anything that helps fix it (optional)")
        self.details.setFixedHeight(70)
        body.addWidget(self.details)
        nrow = QHBoxLayout()
        nrow.addWidget(label("Notify by SMS", "FieldLabel"))
        nrow.addWidget(self.notify, 1)
        body.addLayout(nrow)
        self.message = Message()
        body.addWidget(self.message)
        row = QHBoxLayout()
        row.addWidget(button("Cancel", on_click=self.reject))
        row.addStretch(1)
        row.addWidget(button("Save report", "Primary", self.save))
        body.addLayout(row)
        self.category.currentIndexChanged.connect(self._type_changed)
        self._type_changed()
        self.setMinimumWidth(min(560, parent.window().width() - 20))
        self.result_data = None

    def _type_changed(self):
        emergency = self.category.currentData() == "emergency"
        self.priority.setCurrentIndex(0 if emergency else 2)
        self.notify.setCurrentIndex(1 if emergency else 0)

    def save(self):
        if len(self.title_f.value()) < 3:
            self.message.error("Say briefly what's wrong (at least 3 characters).")
            return
        self.result_data = {
            "category": self.category.currentData(), "title": self.title_f.value(),
            "location": self.location.value(), "details": self.details.toPlainText().strip(),
            "priority": self.priority.currentData(), "notify": self.notify.currentData(),
        }
        self.accept()


class ReportsTab(_Tab):
    """Emergencies, broken facilities and system problems, with their status history."""

    SHOW = [("Open & in progress", "active"), ("Resolved", "resolved"), ("All", "")]

    def __init__(self, api, on_change, on_charts=None):
        super().__init__(api)
        self.on_change = on_change
        bar = QHBoxLayout()
        if on_charts:
            bar.addWidget(button("📊 Charts", "Small", on_charts))
        self.show_filter = QComboBox()
        for text, value in self.SHOW:
            self.show_filter.addItem(text, value)
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", "")
        for text, value in REPORT_TYPES:
            self.type_filter.addItem(text, value)
        for c in (self.show_filter, self.type_filter):
            c.currentIndexChanged.connect(self.refresh)
            bar.addWidget(c)
        bar.addStretch(1)
        bar.addWidget(button("Refresh", "Small", self.refresh))
        bar.addWidget(button("+ New report", "Gold", self.new_report))
        self.layout_.addLayout(bar)

        self.table = make_table(["#", "Priority", "Type", "What", "Where", "From", "Status", "Taken by", "Reported"])
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda *_: self.details())
        self.layout_.addWidget(self.table, 1)
        self.layout_.addWidget(self.message)

        actions = QHBoxLayout()
        self.progress_btn = button("In progress", on_click=lambda: self.set_status("in_progress"))
        self.resolve_btn = button("Resolved ✓", "Primary", lambda: self.set_status("resolved"))
        self.reopen_btn = button("Reopen", on_click=lambda: self.set_status("open"))
        self.note_btn = button("Add note…", on_click=self.add_note)
        self.details_btn = button("Details", on_click=self.details)
        for b in (self.progress_btn, self.resolve_btn, self.reopen_btn, self.note_btn, self.details_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        self.layout_.addLayout(actions)
        self.update_buttons()

    def refresh(self):
        show, category = self.show_filter.currentData(), self.type_filter.currentData()
        status = "resolved" if show == "resolved" else ""

        def ok(rows):
            if show == "active":
                rows = [r for r in rows if r["status"] != "resolved"]
            pri_colors = {"critical": "rejected", "high": "pending", "normal": "approved", "low": "expired"}
            data = []
            for r in rows:
                pri = status_item(pri_colors.get(r["priority"], "expired"))
                pri.setText(r["priority"].capitalize())
                st = status_item({"open": "pending", "in_progress": "approved", "resolved": "active"}[r["status"]])
                st.setText(STATUS_LABELS[r["status"]])
                origin = {"qr": "QR code", "web": "Report page"}.get(r.get("channel"), "Admin") \
                    if r.get("source") == "public" else "Admin"
                taken = (r.get("assignedTo") or {}).get("name") or ("not answered" if r.get("escalatedAt") else "—")
                data.append([f"#{r['smsRef']}" if r.get("smsRef") else "", pri,
                             TYPE_LABELS.get(r["category"], r["category"]), r["title"], r.get("location") or "—",
                             origin, st, taken, fmt_time(r.get("createdAt"))])
            fill_table(self.table, data, rows)
            if rows:
                self.message.clear()
            else:
                self.message.info("Nothing to show. Use “+ New report” to log an emergency, a broken "
                                  "facility or a system problem.")
            self.update_buttons()

        run_async(lambda: self.api.list_reports(status, category), ok, self.fail)

    def update_buttons(self):
        r = selected_object(self.table)
        st = r["status"] if r else None
        self.progress_btn.setEnabled(st == "open")
        self.resolve_btn.setEnabled(st in ("open", "in_progress"))
        self.reopen_btn.setEnabled(st == "resolved")
        self.note_btn.setEnabled(bool(r))
        self.details_btn.setEnabled(bool(r))

    def _done(self, result):
        if result.get("message"):
            self.message.success(result["message"])
        self.refresh()
        self.on_change()

    def new_report(self):
        dialog = NewReportDialog(self)
        if dialog.exec() and dialog.result_data:
            data = dialog.result_data
            run_async(lambda: self.api.create_report(**data), self._done, self.fail)

    def set_status(self, status):
        r = selected_object(self.table)
        if r:
            run_async(lambda: self.api.update_report(r["id"], status=status), self._done, self.fail)

    def add_note(self):
        r = selected_object(self.table)
        if not r:
            return
        text, ok = QInputDialog.getText(self, "Add note", f"Note for “{r['title']}”:")
        if ok and text.strip():
            run_async(lambda: self.api.update_report(r["id"], note=text.strip()), self._done, self.fail)

    def details(self):
        r = selected_object(self.table)
        if not r:
            return
        lines = [f"{TYPE_LABELS.get(r['category'], r['category'])} · {r['priority'].capitalize()} · "
                 f"{STATUS_LABELS[r['status']]}",
                 f"Where: {r.get('location') or '—'}",
                 f"Reported {fmt_time(r.get('createdAt'))} by {(r.get('createdBy') or {}).get('username', '?')}"]
        if r.get("source") == "public":
            urgency = {"low": "It can wait", "medium": "Needs attention soon", "high": "Urgent or unsafe"}
            lines.append(f"Public report {r.get('publicRef', '')} · urgency: {urgency.get(r.get('urgency'), '?')}"
                         + (f" · cleanliness {r['rating']}/5" if r.get("rating") else ""))
            if r.get("contact"):
                c = r["contact"]
                lines.append(f"Contact: {c.get('name') or '—'} {c.get('phone') or ''}".rstrip())
        if r.get("assignedTo"):
            lines.append(f"Taken by {r['assignedTo'].get('name')} {fmt_time(r.get('acceptedAt'))}")
        if r.get("details"):
            lines += ["", r["details"]]
        if r.get("notes"):
            lines += ["", "History:"] + [f"• {fmt_time(n['at'])} — {n.get('by', '')}: {n['text']}"
                                         for n in r["notes"]]
        box = QMessageBox(self)
        box.setWindowTitle("Report")
        box.setText((f"#{r['smsRef']}  " if r.get("smsRef") else "") + r["title"])
        box.setInformativeText("\n".join(lines))
        box.exec()


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
        self.delete_btn = button("Delete account…", "Danger", self.delete)
        row.addWidget(self.toggle_btn)
        row.addWidget(self.sms_btn)
        row.addStretch(1)
        row.addWidget(self.delete_btn)
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
        self.delete_btn.setEnabled(bool(user and user["uid"] != self.my_uid))
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

    def delete(self):
        user = selected_object(self.table)
        if not user or user["uid"] == self.my_uid:
            return
        who = user.get("fullName") or user["username"]
        text, ok = QInputDialog.getText(
            self, "Delete account",
            f"Permanently delete {who} ({user['role']})?\n\n"
            "They lose access straight away and stop getting SMS alerts. This can't be undone; "
            "if you only want to block them for now, use “Disable account” instead.\n\n"
            f"To confirm, type the username:  {user['username']}")
        if not ok:
            return
        if text.strip().lower() != user["username"].lower():
            self.message.error("Not deleted: the username you typed didn't match.")
            return

        def done(result):
            self.message.success(result["message"])
            self.refresh()
            self.on_change()

        run_async(lambda: self.api.delete_user(user["uid"], text.strip()), done, self.fail)

    def test_sms(self):
        user = selected_object(self.table)
        if not user or not _confirm(self, "Send test SMS", f"Send a test text message to {user['username']} ({user['smsPhone']})?"):
            return
        self.message.info("Sending… this can take up to a minute with a GSM modem.")
        run_async(lambda: self.api.test_sms(user["uid"]), lambda r: self.message.success(r["message"]), self.fail)


# ====================================================================== tools
class PromoDialog(QDialog):
    """Enter the SIM's promo: name, how many days, and the day it was registered."""

    def __init__(self, parent, current: dict):
        super().__init__(parent)
        self.cleared = False
        self.setWindowTitle("SIM promo")
        self.setModal(True)
        body = QVBoxLayout(self)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(7)
        body.addWidget(label("SIM promo", "Section"))
        body.addWidget(label("Enter the promo on the modem's SIM (e.g. Globe unli calls & texts for 15 days). "
                             "AureoClean warns in System health and texts the admins the day before it ends.",
                             "Muted"))
        self.name = Field("Promo name", "Globe unli calls & texts")
        self.name.edit.setMaxLength(60)
        self.name.edit.setText(current.get("name", "") if current.get("set") else "")
        body.addWidget(self.name)
        row = QHBoxLayout()
        days_box = QVBoxLayout()
        days_box.addWidget(label("Lasts (days)", "FieldLabel"))
        self.days = QSpinBox()
        self.days.setRange(1, 400)
        self.days.setValue(15)
        days_box.addWidget(self.days)
        start_box = QVBoxLayout()
        start_box.addWidget(label("Registered on", "FieldLabel"))
        self.start = QDateEdit(QDate.currentDate())
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("MMM d, yyyy")
        if current.get("set") and current.get("registered"):
            self.start.setDate(QDate.fromString(current["registered"], "yyyy-MM-dd"))
            reg = QDate.fromString(current["registered"], "yyyy-MM-dd")
            self.days.setValue(max(1, reg.daysTo(QDate.fromString(current["expires"], "yyyy-MM-dd"))))
        start_box.addWidget(self.start)
        row.addLayout(days_box, 1)
        row.addLayout(start_box, 2)
        body.addLayout(row)
        self.ends = label("", "Muted")
        body.addWidget(self.ends)
        self.days.valueChanged.connect(self._update_end)
        self.start.dateChanged.connect(self._update_end)
        self._update_end()
        buttons = QHBoxLayout()
        buttons.addWidget(button("Cancel", on_click=self.reject))
        if current.get("set"):
            buttons.addWidget(button("Remove", "Danger", self._clear))
        buttons.addStretch(1)
        buttons.addWidget(button("Save", "Primary", self.accept))
        body.addLayout(buttons)
        self.setMinimumWidth(min(400, parent.window().width() - 20))

    def _update_end(self):
        end = self.start.date().addDays(self.days.value())
        self.ends.setText(f"Ends on {end.toString('ddd MMM d, yyyy')}.")

    def _clear(self):
        self.cleared = True
        self.accept()

    def values(self):
        return (self.name.value() or "Promo", self.days.value(), self.start.date().toString("yyyy-MM-dd"))


class ToolsTab(_Tab):
    """SMS (GSM modem) controls, plus shortcuts to Arduino Cloud and Firebase."""

    def __init__(self, api, on_health=None):
        super().__init__(api)

        # ---- System health (first, so problems are obvious)
        self.health = HealthCard(api, on_health)
        self.layout_.addWidget(self.health.frame)

        # ---- SMS card (full width)
        sms, body = card()
        head = QHBoxLayout()
        head.addWidget(label("SMS (GSM modem)", "Section"))
        head.addStretch(1)
        body.addLayout(head)
        self.mode_label = label("Checking…")
        self.mode_label.setObjectName("Info")
        body.addWidget(self.mode_label)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.detect_btn = button("Find & use modem", "Primary", self.detect)
        self.check_btn = button("Check signal", on_click=self.check_sms)
        self.testmode_btn = button("Use test mode", on_click=self.test_mode)
        for b in (self.detect_btn, self.check_btn, self.testmode_btn):
            row.addWidget(b)
        row.addStretch(1)
        body.addLayout(row)
        self.sms_state = label("", "Muted")
        body.addWidget(self.sms_state)

        # The SIM's promo (e.g. unli texts for 15 days): warned before it ends.
        promo_row = QHBoxLayout()
        promo_row.setSpacing(6)
        self.promo_label = label("SIM promo: not set", "Muted")
        promo_row.addWidget(self.promo_label, 1)
        promo_row.addWidget(button("Set promo…", "Small", self.edit_promo))
        body.addLayout(promo_row)

        send_row = QHBoxLayout()
        send_row.setSpacing(6)
        self.test_number = Field("Send a test SMS to", "0917 123 4567")
        send_row.addWidget(self.test_number, 1)
        self.send_btn = button("Send test SMS", "Gold", self.send_test)
        send_row.addWidget(self.send_btn, 0, Qt.AlignBottom)
        body.addLayout(send_row)
        self.test_number.edit.returnPressed.connect(self.send_btn.click)

        # Pretend a janitor replied (OK 12 / DONE 12): handy for testing and demos.
        sim_row = QHBoxLayout()
        sim_row.setSpacing(6)
        self.sim_from = Field("Try a reply: from number", "0917 123 4567")
        self.sim_text = Field("Text", "OK 12")
        sim_row.addWidget(self.sim_from, 3)
        sim_row.addWidget(self.sim_text, 2)
        self.sim_btn = button("Try reply", on_click=self.simulate)
        sim_row.addWidget(self.sim_btn, 0, Qt.AlignBottom)
        body.addLayout(sim_row)
        body.addWidget(label("Janitors reply OK <number> to take a job and DONE <number> when it's finished. "
                             "Real replies are read from the SIM every 20 seconds.", "Muted"))
        self.layout_.addWidget(sms)
        self.layout_.addWidget(self.message)

        # ---- website shortcuts
        self.layout_.addWidget(label("Websites (open in Google Chrome)", "FieldLabel"))
        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        self.layout_.addLayout(self.grid)

        # Kiosk mode: open full-screen at boot (switch), and the way to close it on purpose.
        kiosk_card, kb = card()
        kb.addWidget(label("Kiosk mode", "Section"))
        self.kiosk_box = QCheckBox("Open AureoClean full-screen when the Pi starts, and reopen it if it closes")
        kb.addWidget(self.kiosk_box)
        kb.addWidget(label("Takes effect the next time the Pi starts. On the sign-in screen, "
                           "“Exit kiosk mode…” (admin password needed) turns it off again.", "Muted"))
        self.quit_btn = button("Close AureoClean now", "Danger", self.quit_kiosk)
        self.quit_btn.setVisible("--kiosk" in sys.argv)
        kb.addWidget(self.quit_btn, 0, Qt.AlignLeft)
        self.layout_.addWidget(kiosk_card)
        self.kiosk_box.toggled.connect(self._set_kiosk)
        self.layout_.addStretch(1)

    def _set_kiosk(self, on: bool):
        from .. import kiosk
        try:
            kiosk.enable() if on else kiosk.disable()
        except OSError as exc:
            self.message.error(f"Couldn't change kiosk mode: {exc}")
            return
        self.message.success("Kiosk mode is on: AureoClean will open full-screen when the Pi starts." if on
                             else "Kiosk mode is off: open AureoClean from the desktop icon.")

    def quit_kiosk(self):
        if _confirm(self, "Close AureoClean", "Close the app? It stays closed until the Pi restarts or you "
                    "open AureoClean from the desktop icon. Alerts and SMS keep running in the background "
                    "if the AureoClean service is installed."):
            self.window().quit_app()

    def _busy(self, on: bool, text: str = "") -> None:
        for b in (self.detect_btn, self.check_btn, self.testmode_btn, self.send_btn):
            b.setEnabled(not on)
        if text:
            self.sms_state.setText(text)

    def _show_settings(self, st: dict) -> None:
        on_modem = st.get("mode") == "modem"
        self.mode_label.setText(("● " if on_modem else "○ ") + st.get("label", ""))
        self.testmode_btn.setVisible(on_modem)
        self.detect_btn.setText("Find modem again" if on_modem else "Find & use modem")

    def _show_promo(self, p: dict) -> None:
        self.promo = p
        if not p.get("set"):
            self.promo_label.setText("SIM promo: not set. Enter it to be warned (and texted) before it ends.")
            return
        d = p["daysLeft"]
        when = (f"ended {-d} day(s) ago" if d < 0 else "ends today" if d == 0 else
                "ends tomorrow" if d == 1 else f"{d} days left")
        mark = "✗" if d < 0 else "⚠" if d <= 2 else "●"
        self.promo_label.setText(f"{mark} SIM promo: {p['name']} · {when} (ends {p['expires']})")

    def edit_promo(self):
        dlg = PromoDialog(self, getattr(self, "promo", {}) or {})
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.cleared:
            run_async(self.api.clear_sim_promo, self._after_promo, self.fail)
        else:
            name, days, start = dlg.values()
            run_async(lambda: self.api.set_sim_promo(name, days, start), self._after_promo, self.fail)

    def _after_promo(self, p):
        self._show_promo(p)
        self.message.success("SIM promo saved. You'll get a text the day before it ends."
                             if p.get("set") else "SIM promo removed.")
        self.health.refresh()

    def refresh(self):
        self.message.clear()
        from .. import kiosk
        self.kiosk_box.blockSignals(True)
        self.kiosk_box.setChecked(kiosk.is_enabled())
        self.kiosk_box.blockSignals(False)
        self.health.refresh()
        run_async(self.api.sim_promo, self._show_promo, self.fail)
        run_async(self.api.sms_settings, self._show_settings, self.fail)

        def ok(links):
            while self.grid.count():
                item = self.grid.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            tiles = [LinkButton(link) for link in links if link.get("key") != "dashboard"]
            for i, tile in enumerate(tiles):
                self.grid.addWidget(tile, i // 2, i % 2)

        run_async(self.api.links, ok, self.fail)

    def detect(self):
        self.message.clear()
        self._busy(True, "Looking for the modem on the serial ports… (up to a minute)")

        def ok(result):
            self._show_settings(result)
            self.sms_state.setText("")
            self.message.success(result["message"])

        def err(e):
            self.sms_state.setText("")
            self.fail(e)

        run_async(self.api.sms_detect, ok, err, lambda: self._busy(False))

    def test_mode(self):
        if not _confirm(self, "Use test mode", "Stop sending real texts? Messages will only be written to a log file."):
            return

        def ok(result):
            self._show_settings(result)
            self.message.info(result["message"])

        run_async(self.api.sms_test_mode, ok, self.fail)

    def check_sms(self):
        self.message.clear()
        self._busy(True, "Checking… (a few seconds)")

        def ok(st):
            mark = "✓" if st.get("ok") else "✗"
            self.sms_state.setText(f"{mark} {st.get('detail', '')}")
            if st.get("settings"):
                self._show_settings(st["settings"])

        run_async(self.api.sms_status, ok, self.fail, lambda: self._busy(False))

    def simulate(self):
        self.message.clear()
        phone, text = self.sim_from.value(), self.sim_text.value()
        if not phone or not text:
            self.message.error("Type the janitor's number and a reply, e.g. OK 12.")
            return
        labels = {"accepted": "accepted the job", "done": "marked it done", "taken": "was told it's already taken",
                  "already_done": "was told it's already done", "not_found": "was told that report isn't open",
                  "help": "got the how-to-reply text", "ignored": "was ignored"}

        def ok(r):
            extra = f" (#{r['ref']})" if r.get("ref") else (f": {r['reason']}" if r.get("reason") else "")
            self.message.success(f"Result: the sender {labels.get(r['action'], r['action'])}{extra}.")

        run_async(lambda: self.api.sms_simulate_reply(phone, text), ok, self.fail)

    def send_test(self):
        self.message.clear()
        number = self.test_number.value()
        if not number:
            self.message.error("Type a mobile number first.")
            return
        self._busy(True, "Sending… (up to a minute with the GSM modem)")
        run_async(lambda: self.api.sms_test_to(number), lambda r: self.message.success(r["message"]), self.fail,
                  lambda: self._busy(False, " "))


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


# ====================================================================== console
TAB_DASHBOARD, TAB_REQUESTS, TAB_REPORTS, TAB_USERS, TAB_INVITES, TAB_AUDIT, TAB_TOOLS, TAB_ACCOUNT = range(8)


class AdminDashboard(QWidget):
    def __init__(self, nav):
        super().__init__()
        self.nav = nav
        self.api = nav.api
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = TopBar("AureoClean · Admin", nav.logout, on_lock=lambda: nav.lock_screen())
        root.addWidget(self.topbar)

        self.tabs = QTabWidget()
        self.dash_tab = QWidget()
        dl = QVBoxLayout(self.dash_tab)
        dl.setContentsMargins(0, 0, 0, 0)
        self.requests = RequestsTab(self.api, self.refresh_overview)
        # Reports: the list, and a charts dashboard behind the "📊 Charts" button.
        self.reports_stack = QStackedWidget()
        self.reports = ReportsTab(self.api, self.refresh_overview, on_charts=self.show_report_charts)
        self.report_charts = ReportsDashboard(self.api, on_back=lambda: self.reports_stack.setCurrentIndex(0))
        self.reports_stack.addWidget(self.reports)
        self.reports_stack.addWidget(scroll_page(self.report_charts))
        self.users = UsersTab(self.api, self.refresh_overview)
        self.invites = InvitesTab(self.api)
        self.audit = AuditTab(self.api)
        self.tools = ToolsTab(self.api, on_health=self._show_health)

        # My account: details/password | SMS, side by side.
        account = QWidget()
        grid = QGridLayout(account)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        info, ib = card()
        ib.addWidget(label("My account", "Section"))
        self.account_info = label("", "Muted")
        ib.addWidget(self.account_info)
        ib.addWidget(change_password_button(self.api, self))
        ib.addWidget(lock_pin_button(self.api, self, self.nav))
        ib.addStretch(1)
        self.sms = SmsCard(self.api, "Critical alerts (fire, emergency stop) and system notices by text.",
                           on_open_tools=lambda: self.tabs.setCurrentIndex(TAB_TOOLS))
        grid.addWidget(info, 0, 0)
        grid.addWidget(self.sms, 0, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 6)
        grid.setRowStretch(1, 1)

        self.tabs.addTab(self.dash_tab, "Dashboard")
        self.tabs.addTab(self.requests, "Requests")
        self.tabs.addTab(self.reports_stack, "Reports")
        self.tabs.addTab(self.users, "Users")
        self.tabs.addTab(scroll_page(self.invites), "Invites")
        self.tabs.addTab(self.audit, "Audit")
        self.tabs.addTab(scroll_page(self.tools), "Tools")
        self.tabs.addTab(scroll_page(account), "Account")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)

        # Website sign-ups and new reports arrive in the background: keep the counts fresh.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_tick)

    def _poll_tick(self):
        if self.isVisible() and self.nav.api.token:
            self.refresh_overview(cached=True)
            if self.tabs.currentIndex() == TAB_REQUESTS:
                self.requests.refresh()
            self._ticks = getattr(self, "_ticks", 0) + 1
            if self._ticks % 4 == 1:  # every 2 minutes: warn on the Tools tab if something's wrong
                run_async(self.api.system_health, lambda r: self._show_health(r.get("status")), lambda e: None)

    def _show_health(self, status):
        self.tabs.setTabText(TAB_TOOLS, {"warn": "Tools ⚠", "fail": "Tools ✗"}.get(status, "Tools"))

    def _tab_changed(self, index):
        refreshers = {TAB_REQUESTS: self.requests.refresh,
                      TAB_REPORTS: lambda: (self.report_charts.refresh() if self.reports_stack.currentIndex()
                                            else self.reports.refresh()),
                      TAB_USERS: self.users.refresh,
                      TAB_INVITES: self.invites.refresh, TAB_AUDIT: self.audit.refresh,
                      TAB_TOOLS: self.tools.refresh, TAB_ACCOUNT: self.load_account}
        if index in refreshers:
            refreshers[index]()
        if index in (TAB_DASHBOARD, TAB_REQUESTS, TAB_REPORTS):
            self.refresh_overview()

    def enter(self, user: dict):
        self.topbar.set_user(user)
        self.users.my_uid = user["uid"]
        self.account_info.setText(f"{user.get('fullName', '')}\n{user.get('username', '')} · {user.get('email', '')}")
        self.nav.dashboard.attach_to(self.dash_tab)
        self.requests.filter.setCurrentIndex(0)
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(TAB_DASHBOARD)
        self.tabs.blockSignals(False)
        self.refresh_overview()
        self._ticks = 1
        run_async(self.api.system_health, lambda r: self._show_health(r.get("status")), lambda e: None)
        self._poll.start(30_000)

    def show_report_charts(self):
        self.reports_stack.setCurrentIndex(1)
        self.report_charts.refresh()

    def load_account(self):
        self.sms.message.clear()  # don't show an old "test mode" note after switching modes
        run_async(self.api.me, self.sms.load, lambda e: self.nav.handle_session_error(e))

    def refresh_overview(self, cached: bool = False):
        """Pending count goes in the tab name, e.g. "Requests (2)"."""

        def ok(ov):
            pending = ov.get("pendingRequests", 0)
            self.tabs.setTabText(TAB_REQUESTS, f"Requests ({pending})" if pending else "Requests")
            open_reports = ov.get("openReports", 0)
            self.tabs.setTabText(TAB_REPORTS, f"Reports ({open_reports})" if open_reports else "Reports")
            self.requests.summary.setText(
                f"{pending} pending · {ov.get('awaitingActivation', 0)} awaiting activation · "
                f"{ov.get('activeJanitors', 0)} active janitors")

        def err(e):
            if not self.nav.handle_session_error(e):
                self.requests.summary.setText(f"⚠ {e.message}")

        run_async(lambda: self.api.admin_overview(cached), ok, err)
