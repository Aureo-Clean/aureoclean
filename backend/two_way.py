"""Two-way SMS: janitors reply to alerts to accept and finish jobs.

Alert texts end with "Reply OK 12 or DONE 12" (12 = the report's short number).

    OK 12 / ACCEPT / YES / SIGE / GO      -> report 12 is "in progress", assigned to the sender
    DONE 12 / FIXED / TAPOS / CLEAN       -> report 12 is resolved
    HELP                                  -> how to reply

The number may be left out: then the newest open report that was texted to the
sender is used. Texts from numbers that aren't AureoClean contacts are ignored.

Escalation: if nobody accepts an alerted report within N minutes (default 10),
the administrators are texted once; critical reports are also re-sent to the
people first alerted.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from typing import Optional

from .security import FailureTracker, iso, parse_iso, utcnow
from .sms import SmsError, gsm_text, mask_phone, normalize_phone

log = logging.getLogger("aureoclean.two_way")

ACCEPT_WORDS = ("OK", "OKAY", "ACCEPT", "YES", "SIGE", "GO", "ON IT", "COPY")
DONE_WORDS = ("DONE", "FIXED", "TAPOS", "CLEAN", "CLEANED", "FINISHED", "RESOLVED")
HELP_WORDS = ("HELP", "?", "TULONG")

_WORDS = sorted(ACCEPT_WORDS + DONE_WORDS + HELP_WORDS, key=len, reverse=True)
_COMMAND = re.compile(
    r"^\s*(?P<word>" + "|".join(re.escape(w).replace(r"\ ", r"\s*") for w in _WORDS) + r")"
    r"\s*#?\s*(?P<num>\d{1,6})?(?=$|[\s.,!?])", re.I)


def parse_reply(text: str) -> tuple[Optional[str], Optional[int]]:
    """'ok 12' -> ('accept', 12); 'Tapos #7' -> ('done', 7); 'help' -> ('help', None)."""
    m = _COMMAND.match((text or "").strip() + " ")
    if not m:
        return None, None
    word = re.sub(r"\s+", " ", m.group("word").upper())
    num = int(m.group("num")) if m.group("num") else None
    if word in ACCEPT_WORDS or word.replace(" ", "") == "ONIT":
        return "accept", num
    if word in DONE_WORDS:
        return "done", num
    return "help", None


def alert_text(label: str, ref: int, title: str, location: str = "", details: str = "") -> str:
    """One SMS (<=160 chars) that always keeps the reply instructions."""
    prefix = f"AureoClean {label} #{ref}: "
    suffix = f". Reply OK {ref} or DONE {ref}"
    body = title + (f" at {location}" if location else "")
    if details:
        body += f". {details}"
    room = 160 - len(prefix) - len(suffix)
    body = gsm_text(body, 10_000)
    if len(body) > room:
        body = body[: max(0, room - 3)].rstrip() + "..."
    return prefix + body.rstrip(". ") + suffix


class TwoWaySms:
    def __init__(self, service, escalate_minutes: Optional[int] = None):
        self.svc = service
        self.escalate_minutes = escalate_minutes or int(os.environ.get("AUREOCLEAN_ESCALATE_MINUTES", "10") or 10)
        self._help_limit = FailureTracker(1, 10)  # at most one help reply per number per 10 min

    # ------------------------------------------------------------------ incoming
    def poll(self) -> int:
        """Read the modem's inbox and handle every text. Returns how many were handled."""
        reader = getattr(self.svc.sms, "read_inbox", None)
        if reader is None:
            return 0
        handled = 0
        for msg in reader():
            try:
                self.handle(msg.sender, msg.text)
                handled += 1
            except Exception:  # noqa: BLE001 - one bad text must not stop the others
                log.exception("Couldn't handle SMS from %s", mask_phone(msg.sender))
        return handled

    def handle(self, sender: str, text: str) -> dict:
        """Process one incoming text. Returns what happened (also used by the simulate button)."""
        svc = self.svc
        try:
            phone = normalize_phone(sender)
        except ValueError:
            return {"action": "ignored", "reason": "not a mobile number (e.g. network promo)"}
        users = [u for _, u in svc.store.query(svc.C_USERS, smsPhone=phone) if u.get("status") == "active"]
        if not users:
            svc.audit("sms_from_unknown", {"type": "system", "username": "sms"}, None,
                      {"phone": mask_phone(phone), "text": (text or "")[:40]})
            return {"action": "ignored", "reason": "number is not an AureoClean contact"}
        user = users[0]
        actor = {"type": user["role"], "uid": user["uid"], "username": user["username"], "via": "sms"}
        command, ref = parse_reply(text)
        svc.audit("sms_reply_received", actor, None, {"text": (text or "")[:60]})

        if command in (None, "help"):
            if not self._help_limit.locked_for(phone):
                self._help_limit.record_failure(phone)
                self._reply(phone, "AureoClean: reply OK <number> when you take a job, DONE <number> when "
                                   "it's finished. Example: OK 12")
            return {"action": "help"}

        report_id, report = self._find_report(user, ref)
        if not report:
            self._reply(phone, f"AureoClean: report #{ref} not found or already closed." if ref else
                        "AureoClean: you have no open alerts. Reply OK <number> to take a job.")
            return {"action": "not_found", "ref": ref}
        ref = report.get("smsRef")
        name = (user.get("fullName") or user["username"]).split()[0]

        if command == "accept":
            holder = report.get("assignedTo") or {}
            if report["status"] == "resolved":
                self._reply(phone, f"AureoClean: #{ref} is already done. Thank you!")
                return {"action": "already_done", "ref": ref}
            if holder and holder.get("uid") != user["uid"]:
                self._reply(phone, f"AureoClean: #{ref} is already taken by {holder.get('name', 'someone')}.")
                return {"action": "taken", "ref": ref}

            def accept(current):
                current.update(status="in_progress", acceptedAt=iso(utcnow()),
                               assignedTo={"uid": user["uid"], "username": user["username"], "name": name})
                return current
            self._update(report_id, accept, actor, f"Accepted by {name} via SMS", "in_progress")
            self._reply(phone, f"AureoClean: thanks {name}, #{ref} is yours. Reply DONE {ref} when finished.")
            return {"action": "accepted", "ref": ref}

        # done
        if report["status"] == "resolved":
            self._reply(phone, f"AureoClean: #{ref} was already marked done. Thank you!")
            return {"action": "already_done", "ref": ref}

        def finish(current):
            current.setdefault("assignedTo", {"uid": user["uid"], "username": user["username"], "name": name})
            current["resolvedBy"] = {"uid": user["uid"], "username": user["username"], "name": name}
            return current
        self._update(report_id, finish, actor, f"Marked done by {name} via SMS", "resolved")
        self._reply(phone, f"AureoClean: #{ref} marked done. Thank you, {name}!")
        return {"action": "done", "ref": ref}

    def _find_report(self, user: dict, ref: Optional[int]):
        svc = self.svc
        if ref is not None:
            rows = svc.store.query(svc.C_REPORTS, smsRef=ref)
            return rows[0] if rows else (None, None)
        # No number: newest open report that was texted to this person (or that they hold).
        mine = [(i, r) for i, r in svc.store.query(svc.C_REPORTS)
                if r.get("status") != "resolved" and r.get("smsRef")
                and (user["uid"] in (r.get("notifiedTo") or []) or (r.get("assignedTo") or {}).get("uid") == user["uid"])]
        mine.sort(key=lambda ir: ir[1].get("notifiedAt") or ir[1].get("createdAt") or "", reverse=True)
        return mine[0] if mine else (None, None)

    def _update(self, report_id: str, change, actor: dict, note: str, status: str) -> None:
        self.svc.store.transact(self.svc.C_REPORTS, report_id, lambda cur: (change(cur), cur)[1])
        self.svc._add_report_note(report_id, actor, note, status)
        self.svc.audit("report_updated", actor, report_id, {"status": status, "via": "sms"})

    def _reply(self, phone: str, text: str) -> None:
        try:
            self.svc.sms.send(phone, text)
        except SmsError as exc:
            log.warning("Couldn't reply to %s: %s", mask_phone(phone), exc)

    # ------------------------------------------------------------------ escalation
    def check_escalations(self) -> int:
        """Text the admins about alerted reports nobody accepted in time. Returns how many."""
        svc = self.svc
        cutoff = utcnow() - timedelta(minutes=self.escalate_minutes)
        count = 0
        for report_id, r in svc.store.query(svc.C_REPORTS, status="open"):
            if not r.get("notifiedAt") or r.get("escalatedAt") or parse_iso(r["notifiedAt"]) > cutoff:
                continue

            def mark(current):
                if current.get("escalatedAt") or current.get("status") != "open":
                    return None
                current["escalatedAt"] = iso(utcnow())
                return current
            if svc.store.transact(svc.C_REPORTS, report_id, mark) is None:
                continue
            admins = svc.sms_recipients("admin")
            text = alert_text("UNANSWERED", r["smsRef"], r["title"], r.get("location", ""),
                              f"nobody replied in {self.escalate_minutes} min")
            recipients = list(admins)
            if r.get("priority") == "critical":  # remind the people first alerted too
                ids = set(r.get("notifiedTo") or [])
                recipients += [x for x in svc.sms_recipients() if x["uid"] in ids and x not in admins]
            for x in recipients:
                self._reply(x["phone"], text)
            svc._add_report_note(report_id, {"type": "system", "username": "sms"},
                                 f"No reply after {self.escalate_minutes} min: escalated to {len(recipients)} people.")
            svc.audit("report_escalated", {"type": "system", "username": "sms"}, report_id, {"to": len(recipients)})
            count += 1
        return count
