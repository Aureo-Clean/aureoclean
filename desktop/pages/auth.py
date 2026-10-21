"""Sign-in, janitor request, activation, admin registration and status pages."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ..widgets import (
    BusyButton,
    Field,
    Message,
    button,
    card,
    label,
    run_async,
    scroll_page,
    show_password_toggle,
)

FORM_WIDTH = 460


class _FormPage(QWidget):
    """Common frame: centred card in a scroll area, back link, title."""

    def __init__(self, nav, title: str, subtitle: str = "", back: bool = True):
        super().__init__()
        self.nav = nav
        self.api = nav.api
        self.frame, self.body = card()
        if back:
            self.body.addWidget(button("← Back to sign in", "Link", nav.show_login), 0, Qt.AlignLeft)
        self.body.addWidget(label(title, "PageTitle"))
        if subtitle:
            self.body.addWidget(label(subtitle, "Muted"))
        self.message = Message()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll_page(self.frame, FORM_WIDTH))

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
        super().__init__(nav, "Sign in", back=False)
        # Brand block above the title.
        brand = QHBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(label("Aureo", "BrandGold", wrap=False))
        brand.addWidget(label("Clean", "Brand", wrap=False))
        brand.addStretch(1)
        self.body.insertLayout(0, brand)
        self.body.insertWidget(1, label("Restroom hygiene monitoring · STI College Caloocan", "Muted"))
        self.body.insertSpacing(2, 8)

        self.identifier = Field("Username or email", "e.g. markagot")
        self.password = Field("Password", "Your password", password=True)
        self.body.addWidget(self.identifier)
        self.body.addWidget(self.password)
        self.body.addWidget(show_password_toggle(self.password))
        self.submit = button("Sign in", "Primary", self.do_login)
        self.busy = BusyButton(self.submit, "Signing in…")
        self.finish_form(self.submit)

        self.body.addSpacing(10)
        self.body.addWidget(label("Janitors", "FieldLabel"))
        self.body.addWidget(button("Request janitor access", "Link", lambda: nav.show_page("janitor_request")))
        self.body.addWidget(button("I have an activation code", "Link", lambda: nav.show_page("activate")))
        self.body.addWidget(button("Check my request status", "Link", lambda: nav.show_page("status")))
        self.body.addWidget(label("Administrators", "FieldLabel"))
        self.body.addWidget(button("Register with an admin invite code", "Link", lambda: nav.show_page("admin_register")))

        self.service_status = label("Checking connection to the AureoClean service…", "Muted")
        self.body.addSpacing(6)
        self.body.addWidget(self.service_status)

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
            "You don't choose a password yet. An administrator reviews your request and "
            "gives you a one-time activation code. Then use “I have an activation code” to finish.",
        )
        self.full_name = Field("Full name *", "e.g. Mark Dave Agot")
        self.username = Field("Username *", "e.g. markagot", hint="3–24 letters, numbers, dots or underscores.")
        self.email = Field("Email *", "you@example.com")
        for f in (self.full_name, self.username, self.email):
            self.body.addWidget(f)
        self.body.addWidget(label(
            "Want SMS alerts? After you sign in, add your mobile number under “My profile”.", "Muted"))
        self.submit = button("Send request", "Primary", self.do_submit)
        self.busy = BusyButton(self.submit, "Sending…")
        self.finish_form(self.submit)

    def do_submit(self):
        self.message.clear()
        if not self.check_required(self.full_name, self.username, self.email):
            return
        args = (self.full_name.value(), self.username.value(), self.email.value(), "")
        self.busy.start()

        def ok(result):
            username = self.username.value()
            self.reset()
            self.message.success(
                f"{result['message']}\n\nYour username is “{username}”. "
                "Keep it — you'll need it to activate your account."
            )

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
        self.password = Field("New password *", "At least 8 characters", password=True,
                              hint="At least 8 characters with a letter and a number.")
        self.confirm = Field("Confirm password *", "Type it again", password=True)
        for f in (self.username, self.code, self.password, self.confirm):
            self.body.addWidget(f)
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
            "The first administrator is created on the Raspberry Pi with tools/create_admin.py.",
        )
        self.invite = Field("Admin invite code *", "XXXXX-XXXXX-XXXXX")
        self.full_name = Field("Full name *", "Your full name")
        self.username = Field("Username *", "e.g. headadmin", hint="3–24 letters, numbers, dots or underscores.")
        self.email = Field("Email *", "you@example.com", hint="Must match the email the invite was issued for, if any.")
        self.password = Field("Password *", "At least 8 characters", password=True,
                              hint="At least 8 characters with a letter and a number.")
        self.confirm = Field("Confirm password *", "Type it again", password=True)
        for f in (self.invite, self.full_name, self.username, self.email, self.password, self.confirm):
            self.body.addWidget(f)
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
        self.body.addWidget(self.username)
        self.body.addWidget(self.email)
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
