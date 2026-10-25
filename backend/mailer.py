"""Sending email (admin invites) through a Gmail account, or any SMTP server.

Set up once in the app (System → SMS & email → Email) with a Gmail address and a
Gmail *app password* (Google Account → Security → 2-Step Verification → App
passwords). The password is kept only on this Pi, in ~/.config/aureoclean/mail.json
(readable by this user only), or can be given in the config file instead:

    AUREOCLEAN_SMTP_USER=yourname@gmail.com
    AUREOCLEAN_SMTP_PASSWORD=abcd efgh ijkl mnop
    # optional: AUREOCLEAN_SMTP_HOST=smtp.gmail.com  AUREOCLEAN_SMTP_PORT=587
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Optional


class MailError(Exception):
    pass


class Mailer:
    def __init__(self, settings_file: Path, smtp_factory=None):
        self.file = settings_file
        self._smtp_factory = smtp_factory  # tests plug in a fake server here

    # ------------------------------------------------------------------ settings
    def _saved(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def settings(self) -> dict:
        saved = self._saved()
        return {
            "user": saved.get("user") or os.environ.get("AUREOCLEAN_SMTP_USER", "").strip(),
            "password": saved.get("password") or os.environ.get("AUREOCLEAN_SMTP_PASSWORD", "").strip(),
            "host": saved.get("host") or os.environ.get("AUREOCLEAN_SMTP_HOST", "").strip() or "smtp.gmail.com",
            "port": int(saved.get("port") or os.environ.get("AUREOCLEAN_SMTP_PORT", "") or 587),
            "fromName": saved.get("fromName") or "AureoClean",
        }

    @property
    def configured(self) -> bool:
        s = self.settings()
        return bool(s["user"] and s["password"])

    def public_status(self) -> dict:
        s = self.settings()
        return {"configured": self.configured, "user": s["user"], "host": s["host"]}

    def save(self, user: str, password: str) -> None:
        user = (user or "").strip()
        password = re.sub(r"\s+", "", password or "")  # Google shows app passwords as "abcd efgh ijkl mnop"
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", user):
            raise MailError("Enter the full Gmail address, e.g. aureoclean.sti@gmail.com.")
        if len(password) < 8:
            raise MailError("Paste the 16-letter app password from your Google Account.")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        data = {**self._saved(), "user": user, "password": password}
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.file.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ sending
    def send(self, to: str, subject: str, text: str, html: Optional[str] = None) -> None:
        s = self.settings()
        if not (s["user"] and s["password"]):
            raise MailError("Email isn't set up yet (System → SMS & email → Email).")
        msg = EmailMessage()
        msg["From"] = formataddr((s["fromName"], s["user"]))
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            if self._smtp_factory:
                server = self._smtp_factory(s["host"], s["port"])
            elif s["port"] == 465:
                server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=25, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(s["host"], s["port"], timeout=25)
            with server:
                if s["port"] != 465 and not self._smtp_factory:
                    server.starttls(context=ssl.create_default_context())
                server.login(s["user"], s["password"])
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError("Gmail refused the sign-in. Use an app password (not your normal password), "
                            "made under Google Account → Security → 2-Step Verification → App passwords.") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"The email address {to} was refused.") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Couldn't send the email ({type(exc).__name__}). Check the Pi's internet.") from exc
