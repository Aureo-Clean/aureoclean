"""Idle lock screen (kiosk mode).

After a few minutes with no touches, the signed-in screen is covered by this
page. The PIN (or the password, if no PIN is set) brings the person straight
back to where they were; five wrong tries sign the session out."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..widgets import Message, button, card, label, logo_label, run_async


class LockPage(QWidget):
    def __init__(self, nav):
        super().__init__()
        self.nav = nav
        self.back_to = None
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        frame, body = card()
        frame.setMaximumWidth(420)
        body.setSpacing(6)

        head = QHBoxLayout()
        head.addWidget(logo_label(36))
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.clock = label("", "PageTitle", wrap=False)
        self.who = label("", "Muted", wrap=False)
        titles.addWidget(self.clock)
        titles.addWidget(self.who)
        head.addLayout(titles, 1)
        body.addLayout(head)

        self.prompt = label("", "FieldLabel")
        body.addWidget(self.prompt)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret.setAlignment(Qt.AlignCenter)
        self.secret.setMinimumHeight(38)
        self.secret.returnPressed.connect(self.unlock)
        body.addWidget(self.secret)

        # Big on-screen keypad for the touchscreen.
        self.keypad = QWidget()
        grid = QGridLayout(self.keypad)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "Unlock"]
        for i, key in enumerate(keys):
            b = QPushButton(key)
            b.setMinimumHeight(40)
            b.setFocusPolicy(Qt.NoFocus)
            if key == "Unlock":
                b.setObjectName("Primary")
                b.clicked.connect(self.unlock)
            elif key == "⌫":
                b.clicked.connect(self.secret.backspace)
            else:
                b.clicked.connect(lambda _=False, k=key: self.secret.insert(k))
            grid.addWidget(b, i // 3, i % 3)
        body.addWidget(self.keypad)
        self.unlock_btn = button("Unlock", "Primary", self.unlock)
        body.addWidget(self.unlock_btn)

        self.message = Message()
        body.addWidget(self.message)
        foot = QHBoxLayout()
        self.hint = label("", "Muted")
        foot.addWidget(self.hint, 1)
        foot.addWidget(button("Sign out", "Small", self.sign_out))
        body.addLayout(foot)
        row.addWidget(frame, 3)
        row.addStretch(1)
        root.addLayout(row)
        root.addStretch(1)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_clock)

    def _update_clock(self):
        self.clock.setText(datetime.now().strftime("%I:%M %p · %a %b %d").lstrip("0"))

    def lock(self, back_to: QWidget, user: dict, minutes: int):
        self.back_to = back_to
        has_pin = bool(user.get("hasLockPin"))
        self.who.setText(f"Locked · {user.get('fullName') or user.get('username', '')}")
        self.prompt.setText("Enter your PIN to unlock" if has_pin else "Enter your password to unlock")
        self.keypad.setVisible(has_pin)
        self.unlock_btn.setVisible(not has_pin)
        self.secret.setMaxLength(6 if has_pin else 128)
        self.hint.setText((f"Locked after {minutes} min without use." if minutes else "Locked.")
                          + ("" if has_pin else " Tip: set a PIN in Account for faster unlocking."))
        self.secret.clear()
        self.message.clear()
        self._update_clock()
        self._tick.start(10_000)
        self.secret.setFocus()

    def unlock(self):
        secret = self.secret.text()
        if not secret:
            return
        self.secret.clear()

        def ok(_):
            self._tick.stop()
            self.nav.unlocked(self.back_to)

        def err(e):
            if not self.nav.handle_session_error(e):
                self.message.error(e.message)

        run_async(lambda: self.nav.api.unlock(secret), ok, err)

    def sign_out(self):
        self._tick.stop()
        self.nav.logout()
