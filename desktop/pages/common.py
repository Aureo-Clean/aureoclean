"""Pieces shared by the admin and janitor dashboards."""
from __future__ import annotations

import shutil

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget

from ..widgets import BusyButton, Field, Message, button, card, label, run_async, show_password_toggle


class TopBar(QFrame):
    def __init__(self, title: str, on_logout):
        super().__init__()
        self.setObjectName("TopBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 10, 14, 10)
        row.addWidget(label(title, "TopTitle", wrap=False))
        row.addStretch(1)
        self.who = label("", wrap=False)
        row.addWidget(self.who)
        row.addSpacing(10)
        row.addWidget(button("Sign out", "TopButton", on_logout))

    def set_user(self, user: dict):
        self.who.setText(f"{user.get('fullName') or user.get('username')}  ·  {user.get('role', '').capitalize()}")


class SmsCard(QWidget):
    """Add / verify a mobile number and switch SMS alerts on or off."""

    def __init__(self, api, intro: str):
        super().__init__()
        self.api = api
        self.profile: dict = {}
        frame, body = card()
        body.addWidget(label("SMS alerts", "Section"))
        body.addWidget(label(intro, "Muted"))

        self.state = label("")
        self.state.setObjectName("Info")
        body.addWidget(self.state)

        self.alerts = QCheckBox("Send me alerts by SMS")
        self.alerts.setStyleSheet("font-size: 15px; color: #1B2428;")
        self.alerts.toggled.connect(self.toggle_alerts)
        body.addWidget(self.alerts)

        # Step 1: number
        self.number = Field("Mobile number", "0917 123 4567")
        body.addWidget(self.number)
        self.send_btn = button("Send verification code", "Primary", self.send_code)
        self.send_busy = BusyButton(self.send_btn, "Sending…")
        body.addWidget(self.send_btn)

        # Step 2: code
        self.code_box = QWidget()
        cl = QVBoxLayout(self.code_box)
        cl.setContentsMargins(0, 6, 0, 0)
        cl.setSpacing(8)
        self.code = Field("6-digit code from the SMS", "123456")
        cl.addWidget(self.code)
        row = QHBoxLayout()
        self.verify_btn = button("Verify number", "Gold", self.verify)
        self.verify_busy = BusyButton(self.verify_btn, "Checking…")
        row.addWidget(self.verify_btn)
        row.addStretch(1)
        cl.addLayout(row)
        cl.addWidget(label("Didn't get it? Wait a minute, then send the code again.", "Muted"))
        body.addWidget(self.code_box)

        self.remove_btn = button("Remove my number", "Danger", self.remove)
        body.addWidget(self.remove_btn, 0, Qt.AlignLeft)
        self.message = Message()
        body.addWidget(self.message)

        self.number.edit.returnPressed.connect(self.send_btn.click)
        self.code.edit.returnPressed.connect(self.verify_btn.click)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        frame.setMaximumWidth(460)
        layout.addWidget(frame)
        layout.addStretch(1)

    def load(self, profile: dict):
        self.profile = profile
        verified = profile.get("smsPhone")
        pending = profile.get("smsPendingPhone")
        if verified:
            on = "on" if profile.get("smsAlerts") else "off"
            self.state.setText(f"✓ Verified number: {verified}\nSMS alerts are {on}.")
            self.send_btn.setText("Change number")
        else:
            self.state.setText("No mobile number yet. Add one to receive SMS alerts.")
            self.send_btn.setText("Send verification code")
        self.send_busy.idle_text = self.send_btn.text()
        self.alerts.blockSignals(True)
        self.alerts.setChecked(bool(profile.get("smsAlerts")))
        self.alerts.setEnabled(bool(verified))
        self.alerts.blockSignals(False)
        self.remove_btn.setVisible(bool(verified))
        self.code_box.setVisible(bool(pending))
        if pending and not self.number.value():
            self.number.edit.setText(pending)

    def _fail(self, e):
        self.message.error(e.message)

    def send_code(self):
        self.message.clear()
        phone = self.number.value()
        if not phone:
            self.message.error("Type your mobile number first.")
            return
        self.send_busy.start()

        def ok(result):
            self.number.edit.setText(result["phone"])
            self.code_box.setVisible(True)
            self.code.clear()
            self.code.edit.setFocus()
            (self.message.info if result.get("smsMode") == "log" else self.message.success)(result["message"])

        run_async(lambda: self.api.phone_start(phone), ok, self._fail, self.send_busy.stop)

    def verify(self):
        self.message.clear()
        code = self.code.value()
        if not code:
            self.message.error("Type the code from the SMS.")
            return
        self.verify_busy.start()

        def ok(profile):
            self.number.clear()
            self.code.clear()
            self.load(profile)
            self.message.success("Number verified. SMS alerts are now on.")

        run_async(lambda: self.api.phone_confirm(code), ok, self._fail, self.verify_busy.stop)

    def toggle_alerts(self, on: bool):
        self.message.clear()

        def ok(profile):
            self.load(profile)
            self.message.success("SMS alerts turned on." if on else "SMS alerts turned off.")

        def err(e):
            self.load(self.profile)
            self._fail(e)

        run_async(lambda: self.api.set_sms_alerts(on), ok, err)

    def remove(self):
        if QMessageBox.question(self, "Remove number", "Remove your mobile number? You will stop getting SMS alerts.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        def ok(profile):
            self.number.clear()
            self.load(profile)
            self.message.success("Number removed.")

        run_async(self.api.phone_remove, ok, self._fail)


# Browsers to try, in order. Chromium comes with Raspberry Pi OS.
BROWSERS = ("chromium-browser", "chromium", "firefox", "firefox-esr")


def _make_room(parent: QWidget) -> None:
    """A full-screen AureoClean window would hide the browser, so step aside."""
    window = parent.window()
    if window.isFullScreen() or window.isMaximized():
        window.showMinimized()


def open_in_browser(url: str, parent: QWidget) -> None:
    """Open a web page. Launches the browser directly, because a Pi often has
    no default browser set and the system "open link" request then does nothing."""
    for name in BROWSERS:
        path = shutil.which(name)
        if path:
            started = QProcess.startDetached(path, [url])
            # PySide6 returns (ok, pid); some versions return just ok.
            if (started[0] if isinstance(started, tuple) else started):
                _make_room(parent)
                return
    if QDesktopServices.openUrl(QUrl(url)):
        _make_room(parent)
        return
    box = QMessageBox(parent)
    box.setWindowTitle("Couldn't open the browser")
    box.setText("No web browser was found. Open Chromium and go to this address:")
    box.setInformativeText(url)
    box.setTextInteractionFlags(Qt.TextSelectableByMouse)
    box.exec()


class LinkButton(QWidget):
    """A card-style button that opens an external page in the browser."""

    def __init__(self, link: dict):
        super().__init__()
        frame, body = card()
        body.setContentsMargins(16, 12, 16, 12)
        body.addWidget(label(link["title"], "Section"))
        body.addWidget(label(link.get("description", ""), "Muted"))
        btn = button("Open ↗", "Primary", lambda: open_in_browser(link["url"], self))
        body.addWidget(btn, 0, Qt.AlignLeft)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(frame)


class ChangePasswordCard(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        frame, body = card()
        body.addWidget(label("Change password", "Section"))
        self.current = Field("Current password", password=True)
        self.new = Field("New password", password=True, hint="At least 8 characters with a letter and a number.")
        self.confirm = Field("Confirm new password", password=True)
        for f in (self.current, self.new, self.confirm):
            body.addWidget(f)
        body.addWidget(show_password_toggle(self.current, self.new, self.confirm))
        self.message = Message()
        body.addWidget(self.message)
        self.submit = button("Update password", "Primary", self.do_change)
        self.busy = BusyButton(self.submit, "Updating…")
        body.addWidget(self.submit)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        frame.setMaximumWidth(460)
        layout.addWidget(frame)
        layout.addStretch(1)

    def do_change(self):
        self.message.clear()
        if not (self.current.text() and self.new.text()):
            self.message.error("Fill in your current and new password.")
            return
        if self.new.text() != self.confirm.text():
            self.message.error("The new passwords do not match.")
            return
        current, new = self.current.text(), self.new.text()
        self.busy.start()

        def ok(result):
            for f in (self.current, self.new, self.confirm):
                f.clear()
            self.message.success(result.get("message", "Password changed."))

        run_async(lambda: self.api.change_password(current, new), ok, lambda e: self.message.error(e.message), self.busy.stop)
