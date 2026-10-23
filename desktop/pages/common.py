"""Pieces shared by the admin and janitor dashboards."""
from __future__ import annotations

import shutil

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QDialog, QFrame, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget

from ..widgets import PASSWORD_MAX, BusyButton, Field, Message, button, card, label, logo_label, run_async, show_password_toggle


class TopBar(QFrame):
    def __init__(self, title: str, on_logout):
        super().__init__()
        self.setObjectName("TopBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(8)
        row.addWidget(logo_label(26, white_tile=True))
        row.addWidget(label(title, "TopTitle", wrap=False))
        row.addStretch(1)
        self.who = label("", wrap=False)
        row.addWidget(self.who)
        row.addSpacing(6)
        row.addWidget(button("Sign out", "TopButton", on_logout))

    def set_user(self, user: dict):
        self.who.setText(user.get("fullName") or user.get("username") or "")


class SmsCard(QWidget):
    """Add / verify a mobile number and switch SMS alerts on or off."""

    def __init__(self, api, intro: str, on_open_tools=None):
        super().__init__()
        self.api = api
        self.profile: dict = {}
        frame, body = card()
        body.addWidget(label("SMS alerts", "Section"))
        body.addWidget(label(intro, "Muted"))

        # Shown while SMS is in test mode: real codes can't arrive yet.
        self.test_banner = QWidget()
        tb = QHBoxLayout(self.test_banner)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(6)
        warn = label("SMS is in test mode, so no real texts (or codes) are sent yet. "
                     + ("Turn on the modem first:" if on_open_tools else
                        "Ask an administrator to turn on the GSM modem."))
        warn.setObjectName("Error")
        tb.addWidget(warn, 1)
        if on_open_tools:
            tb.addWidget(button("Turn on modem", "Primary", on_open_tools), 0, Qt.AlignVCenter)
        self.test_banner.hide()
        body.addWidget(self.test_banner)

        self.state = label("")
        self.state.setObjectName("Info")
        body.addWidget(self.state)

        self.alerts = QCheckBox("Send me alerts by SMS")
        self.alerts.toggled.connect(self.toggle_alerts)
        body.addWidget(self.alerts)

        # Step 1: number + send button on one row
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.number = Field("Mobile number", "0917 123 4567")
        row1.addWidget(self.number, 1)
        self.send_btn = button("Send code", "Primary", self.send_code)
        self.send_busy = BusyButton(self.send_btn, "Sending…")
        row1.addWidget(self.send_btn, 0, Qt.AlignBottom)
        body.addLayout(row1)

        # Step 2: code + verify button on one row
        self.code_box = QWidget()
        row2 = QHBoxLayout(self.code_box)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)
        self.code = Field("6-digit code from the SMS", "123456")
        row2.addWidget(self.code, 1)
        self.verify_btn = button("Verify", "Gold", self.verify)
        self.verify_busy = BusyButton(self.verify_btn, "Checking…")
        row2.addWidget(self.verify_btn, 0, Qt.AlignBottom)
        body.addWidget(self.code_box)

        self.remove_btn = button("Remove my number", "Danger", self.remove)
        body.addWidget(self.remove_btn, 0, Qt.AlignLeft)
        self.message = Message()
        body.addWidget(self.message)

        self.number.edit.returnPressed.connect(self.send_btn.click)
        self.code.edit.returnPressed.connect(self.verify_btn.click)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(frame)

    def load(self, profile: dict):
        self.profile = profile
        self.message.clear()  # don't leave an old message from a previous attempt
        self.test_banner.setVisible(profile.get("smsMode") == "log")
        verified = profile.get("smsPhone")
        pending = profile.get("smsPendingPhone")
        if verified:
            on = "on" if profile.get("smsAlerts") else "off"
            self.state.setText(f"✓ {verified} verified · SMS alerts {on}")
            self.send_btn.setText("Change")
        else:
            self.state.setText("No mobile number yet.")
            self.send_btn.setText("Send code")
        self.send_busy.idle_text = self.send_btn.text()
        self.alerts.blockSignals(True)
        self.alerts.setChecked(bool(profile.get("smsAlerts")))
        self.alerts.setEnabled(bool(verified))
        self.alerts.setText("Send me alerts by SMS" if verified else "Send me alerts by SMS (verify your number first)")
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


# Browsers to try, in order: Google Chrome first (keeps you signed in with your
# Google account), then the Chromium that comes with Raspberry Pi OS.
BROWSERS = ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium", "firefox", "firefox-esr")


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
    box.setText("No web browser was found. Open Chrome and go to this address:")
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


class ChangePasswordDialog(QDialog):
    """Pop-up for changing your own password (keeps the profile page short)."""

    def __init__(self, api, parent: QWidget):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Change password")
        self.setModal(True)
        body = QVBoxLayout(self)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(7)
        body.addWidget(label("Change password", "Section"))
        self.current = Field("Current password", password=True)
        self.new = Field("New password", password=True, hint="8–16 characters with a letter and a number.",
                         max_length=PASSWORD_MAX)
        self.confirm = Field("Confirm new password", password=True, max_length=PASSWORD_MAX)
        for f in (self.current, self.new, self.confirm):
            body.addWidget(f)
        body.addWidget(show_password_toggle(self.current, self.new, self.confirm))
        self.message = Message()
        body.addWidget(self.message)
        row = QHBoxLayout()
        row.addWidget(button("Cancel", on_click=self.reject))
        row.addStretch(1)
        self.submit = button("Update password", "Primary", self.do_change)
        self.busy = BusyButton(self.submit, "Updating…")
        row.addWidget(self.submit)
        body.addLayout(row)
        self.setMinimumWidth(min(380, parent.window().width() - 20))

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

        def ok(_result):
            QMessageBox.information(self, "Password changed", "Your password has been changed.")
            self.accept()

        run_async(lambda: self.api.change_password(current, new), ok, lambda e: self.message.error(e.message), self.busy.stop)


def change_password_button(api, parent: QWidget):
    return button("Change password…", on_click=lambda: ChangePasswordDialog(api, parent).exec())
