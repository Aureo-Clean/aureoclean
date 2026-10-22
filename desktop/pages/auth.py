"""Sign-in, janitor request, activation, admin registration and status pages."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ..widgets import (
    BusyButton,
    Field,
    Message,
    button,
    card,
    label,
    logo_label,
    run_async,
    scroll_page,
    show_password_toggle,
)

FORM_WIDTH = 720  # wide and short: suits the 7-inch 800x480 display


class _FormPage(QWidget):
    """Common frame: centred card in a scroll area, back link, title."""

    def __init__(self, nav, title: str, subtitle: str = "", back: bool = True, wrap: bool = True):
        super().__init__()
        self.nav = nav
        self.api = nav.api
        self.frame, self.body = card()
        head = QHBoxLayout()
        head.addWidget(label(title, "PageTitle", wrap=False))
        head.addStretch(1)
        if back:
            head.addWidget(button("← Back to sign in", "Link", nav.show_login))
        self.body.addLayout(head)
        if subtitle:
            self.body.addWidget(label(subtitle, "Muted"))
        self.message = Message()
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        if wrap:
            self.outer.addWidget(scroll_page(self.frame, FORM_WIDTH))

    def add_fields(self, *fields, columns: int = 2) -> None:
        """Lay fields out in a grid (two per row) to save height on the small screen."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        for i, f in enumerate(fields):
            grid.addWidget(f, i // columns, i % columns, Qt.AlignTop)
        for c in range(columns):
            grid.setColumnStretch(c, 1)
        self.body.addLayout(grid)

    def finish_form(self, submit_btn):
        self.body.addWidget(self.message)
        self.body.addWidget(submit_btn)
        for field in self.findChildren(Field):
            field.edit.returnPressed.connect(submit_btn.click)

    def reset(self):
        self.message.clear()
        for field in self.findChildren(Field):
            field.clear()

    def check_required(self, *fields: Field) -> bool:
        for f in fields:
            if not f.value():
                self.message.error("Please fill in every required field.")
                f.edit.setFocus()
                return False
        return True

    def check_passwords(self, pw: Field, confirm: Field) -> bool:
        if pw.text() != confirm.text():
            self.message.error("The two passwords do not match.")
            confirm.edit.setFocus()
            return False
        return True


# ======================================================================= login
class LoginPage(_FormPage):
    def __init__(self, nav):
        super().__init__(nav, "Sign in", back=False, wrap=False)
        self.identifier = Field("Username or email", "e.g. markagot")
        self.password = Field("Password", "Your password", password=True)
        self.body.addWidget(self.identifier)
        self.body.addWidget(self.password)
        self.body.addWidget(show_password_toggle(self.password))
        self.submit = button("Sign in", "Primary", self.do_login)
        self.busy = BusyButton(self.submit, "Signing in…")
        self.finish_form(self.submit)
        self.service_status = label("Checking connection to the AureoClean service…", "Muted")
        self.body.addWidget(self.service_status)
        self.body.addStretch(1)

        # Left: logo, name and the other options.
        brand_panel = QWidget()
        bl = QVBoxLayout(brand_panel)
        bl.setContentsMargins(4, 4, 8, 4)
        bl.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(logo_label(58))
        name = QHBoxLayout()
        name.setSpacing(0)
        name.addWidget(label("Aureo", "BrandGold", wrap=False))
        name.addWidget(label("Clean", "Brand", wrap=False))
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        name_col.addLayout(name)
        name_col.addWidget(label("AUTOMATED HYGIENE MONITORING\n& MAINTENANCE SYSTEM", "Tagline"))
        head.addLayout(name_col)
        head.addStretch(1)
        bl.addLayout(head)
        bl.addSpacing(8)
        bl.addWidget(label("Janitors", "FieldLabel"))
        bl.addWidget(button("Request janitor access", "Link", lambda: nav.show_page("janitor_request")))
        bl.addWidget(button("I have an activation code", "Link", lambda: nav.show_page("activate")))
        bl.addWidget(button("Check my request status", "Link", lambda: nav.show_page("status")))
        bl.addSpacing(4)
        bl.addWidget(label("Administrators", "FieldLabel"))
        bl.addWidget(button("Register with an admin invite code", "Link", lambda: nav.show_page("admin_register")))
        bl.addStretch(1)
        bl.addWidget(label("STI College Caloocan", "Muted"))

        content = QFrame()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(brand_panel, 1)
        row.addWidget(self.frame, 1)
        self.outer.addWidget(scroll_page(content, 760))

    def prefill(self, identifier: str = "", note: str = ""):
        self.reset()
        if identifier:
            self.identifier.edit.setText(identifier)
            self.password.edit.setFocus()
        else:
            self.identifier.edit.setFocus()
        if note:
            self.message.success(note)
        self.check_service()

    def check_service(self):
        def ok(result):
            mode = result.get("mode", "?")
            self.service_status.setText(f"● Connected to the AureoClean service ({mode} mode)")

        def err(e):
            self.service_status.setText(f"● {e.message}")

        run_async(self.api.health, ok, err)

    def do_login(self):
        self.message.clear()
        if not self.check_required(self.identifier, self.password):
            return
        ident, pw = self.identifier.value(), self.password.text()
        self.busy.start()

        def ok(result):
            self.password.clear()
            self.nav.signed_in(result["user"])

        def err(e):
            self.password.clear()
            self.message.error(e.message)
            self.password.edit.setFocus()

        run_async(lambda: self.api.login(ident, pw), ok, err, self.busy.stop)


# ======================================================================= janitor request
class JanitorRequestPage(_FormPage):
    def __init__(self, nav):
        super().__init__(
            nav,
            "Request janitor access",
            "No password yet: an administrator reviews your request and gives you a one-time "
            "activation code.",
        )
        self.full_name = Field("Full name *", "e.g. Mark Dave Agot")
        self.username = Field("Username *", "e.g. markagot", hint="3–24 letters, numbers, dots or underscores.")
        self.email = Field("Email *", "you@example.com")
        self.add_fields(self.full_name, self.email, self.username)
        self.submit = button("Send request", "Primary", self.do_submit)
        self.busy = BusyButton(self.submit, "Sending…")
        self.finish_form(self.submit)

        # Confirmation shown instead of the form after a successful request,
        # so a second tap or Enter can't replace it with a "fill in" error.
        self.done_box = QWidget()
        dl = QVBoxLayout(self.done_box)
        dl.setContentsMargins(0, 4, 0, 0)
        dl.setSpacing(10)
        self.done_text = label("", "Success")
        dl.addWidget(self.done_text)
        steps = label(
            "What happens next:\n"
            "1. An administrator reviews your request.\n"
            "2. They give you a one-time activation code.\n"
            "3. Choose “I have an activation code” on the sign-in screen and set your password.", "Muted")
        dl.addWidget(steps)
        row = QHBoxLayout()
        row.addWidget(button("Back to sign in", "Primary", nav.show_login))
        row.addWidget(button("Check my status", on_click=lambda: nav.show_page("status")))
        row.addStretch(1)
        dl.addLayout(row)
        self.done_box.hide()
        self.body.addWidget(self.done_box)
        self.form_parts = [self.full_name, self.username, self.email, self.submit]

    def reset(self):
        super().reset()
        self.done_box.hide()
        for w in self.form_parts:
            w.show()

    def do_submit(self):
        if self.done_box.isVisible():
            return
        self.message.clear()
        if not self.check_required(self.full_name, self.username, self.email):
            return
        args = (self.full_name.value(), self.username.value(), self.email.value(), "")
        self.busy.start()

        def ok(_result):
            username = args[1]
            for w in self.form_parts:
                w.hide()
            self.message.clear()
            self.done_text.setText(f"✓ Request sent successfully!\n\nYour username is “{username}”. "
                                   "Remember it — you'll need it to activate your account.")
            self.done_box.show()

        run_async(lambda: self.api.request_janitor(*args), ok, lambda e: self.message.error(e.message), self.busy.stop)


# ======================================================================= activation
class ActivatePage(_FormPage):
    def __init__(self, nav):
        super().__init__(
            nav,
            "Activate janitor account",
            "Enter the activation code your administrator gave you and choose your password.",
        )
        self.username = Field("Username *", "The username you requested")
        self.code = Field("Activation code *", "XXXX-XXXX")
        self.password = Field("New password *", "8+ characters, letter and number", password=True)
        self.confirm = Field("Confirm password *", "Type it again", password=True)
        self.add_fields(self.username, self.code, self.password, self.confirm)
        self.body.addWidget(show_password_toggle(self.password, self.confirm))
        self.submit = button("Activate account", "Primary", self.do_activate)
        self.busy = BusyButton(self.submit, "Activating…")
        self.finish_form(self.submit)

    def do_activate(self):
        self.message.clear()
        if not self.check_required(self.username, self.code, self.password, self.confirm):
            return
        if not self.check_passwords(self.password, self.confirm):
            return
        username, code, pw = self.username.value(), self.code.value(), self.password.text()
        self.busy.start()

        def ok(result):
            self.nav.show_login(result["username"], result["message"])

        def err(e):
            self.password.clear()
            self.confirm.clear()
            self.message.error(e.message)

        run_async(lambda: self.api.activate(username, code, pw), ok, err, self.busy.stop)


# ======================================================================= admin registration
class AdminRegisterPage(_FormPage):
    def __init__(self, nav):
        super().__init__(
            nav,
            "Register as administrator",
            "Administrator accounts need a one-time invite code from an existing administrator. "
            "The first one is created on the Pi with tools/create_admin.py.",
        )
        self.invite = Field("Admin invite code *", "XXXXX-XXXXX-XXXXX")
        self.full_name = Field("Full name *", "Your full name")
        self.username = Field("Username *", "e.g. headadmin")
        self.email = Field("Email *", "The email the invite was for")
        self.password = Field("Password *", "8+ characters, letter and number", password=True)
        self.confirm = Field("Confirm password *", "Type it again", password=True)
        self.add_fields(self.invite, self.full_name, self.username, self.email, self.password, self.confirm)
        self.body.addWidget(show_password_toggle(self.password, self.confirm))
        self.submit = button("Create admin account", "Primary", self.do_register)
        self.busy = BusyButton(self.submit, "Creating…")
        self.finish_form(self.submit)

    def do_register(self):
        self.message.clear()
        fields = (self.invite, self.full_name, self.username, self.email, self.password, self.confirm)
        if not self.check_required(*fields) or not self.check_passwords(self.password, self.confirm):
            return
        args = (self.invite.value(), self.full_name.value(), self.username.value(), self.email.value(), self.password.text())
        self.busy.start()

        def ok(result):
            self.nav.show_login(result["username"], result["message"])

        def err(e):
            self.password.clear()
            self.confirm.clear()
            self.message.error(e.message)

        run_async(lambda: self.api.register_admin(*args), ok, err, self.busy.stop)


# ======================================================================= request status
class StatusPage(_FormPage):
    def __init__(self, nav):
        super().__init__(nav, "Check request status", "Enter the username and email you used when requesting access.")
        self.username = Field("Username *", "e.g. markagot")
        self.email = Field("Email *", "you@example.com")
        self.add_fields(self.username, self.email)
        self.submit = button("Check status", "Primary", self.do_check)
        self.busy = BusyButton(self.submit, "Checking…")
        self.finish_form(self.submit)

    def do_check(self):
        self.message.clear()
        if not self.check_required(self.username, self.email):
            return
        username, email = self.username.value(), self.email.value()
        self.busy.start()

        def ok(result):
            text = f"Status: {result['status'].capitalize()}\n{result['message']}"
            (self.message.error if result["status"] == "rejected" else self.message.info)(text)

        run_async(lambda: self.api.request_status(username, email), ok, lambda e: self.message.error(e.message), self.busy.stop)
