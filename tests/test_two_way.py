"""Two-way SMS: OK/DONE replies, escalation, and reading the SIM's inbox."""
import unittest
from datetime import timedelta

from backend.security import iso, utcnow
from backend.sms import LogSender, ModemSender
from tests.test_service import Base
from tests.test_sms import FakeSender, FakeSerial

RALPH, MARK = "+639171234567", "+639181234567"


class Recorder(LogSender):
    """Test-mode sender that also records what was sent."""

    def __init__(self):
        super().__init__(outbox=__import__("pathlib").Path("/dev/null"))
        self.sent = []

    def send(self, to, text):
        self.sent.append((to, text))

    def to(self, phone):
        return [t for p, t in self.sent if p == phone]


class TwoWay(Base):
    def setUp(self):
        super().setUp()
        self.svc.sms = Recorder()
        self.svc.sync_notifications = True
        for name, phone in (("Ralph Roque", RALPH), ("Mark Agot", MARK)):
            self.svc.store.set(self.svc.C_WEB_SIGNUPS, phone, {"fullName": name, "phone": phone})
        self.svc.import_web_signups()
        for req in self.svc.list_requests("pending"):
            self.svc.approve_request(self.admin, req["id"])
        self.svc.sms.sent.clear()
        self.report = self.svc.create_report(self.admin, "facility", "Clogged toilet", "2F Male", notify="janitors")
        self.ref = self.svc.list_reports()[0]["smsRef"]

    def reply(self, phone, text):
        self.svc.sms.inject(phone, text)
        return self.svc.two_way.poll()

    def current(self):
        return next(r for r in self.svc.list_reports() if r["id"] == self.report["id"])

    def test_alert_has_number_and_reply_instructions(self):
        self.assertEqual(self.ref, 1)
        texts = self.svc.sms.to(RALPH)
        self.assertTrue(texts[0].startswith("AureoClean BROKEN FACILITY #1: Clogged toilet at 2F Male"))
        self.assertTrue(texts[0].endswith("Reply OK 1 or DONE 1"))
        self.assertEqual(len(self.current()["notifiedTo"]), 2)

    def test_accept_then_done(self):
        self.reply(RALPH, "ok 1")
        r = self.current()
        self.assertEqual((r["status"], r["assignedTo"]["name"]), ("in_progress", "Ralph"))
        self.assertIn("#1 is yours", self.svc.sms.to(RALPH)[-1])

        self.reply(MARK, "OK 1")  # someone else tries
        self.assertIn("already taken by Ralph", self.svc.sms.to(MARK)[-1])

        self.reply(RALPH, "Tapos")  # no number: uses his open job
        r = self.current()
        self.assertEqual((r["status"], r["resolvedBy"]["name"]), ("resolved", "Ralph"))
        self.assertTrue(r["resolvedAt"])
        self.assertIn("marked done", self.svc.sms.to(RALPH)[-1])
        self.assertIn("Accepted by Ralph via SMS", [n["text"] for n in r["notes"]])

        # The dashboard numbers pick up who did it and how fast.
        from backend.analysis import dataset, summarize
        s = summarize(dataset(self.svc))
        self.assertEqual(s["medianMinutesToAccept"], 0)
        self.assertEqual(s["medianMinutesToResolve"], 0)
        self.assertEqual(s["byJanitor"]["Ralph"]["handled"], 1)

    def test_done_without_accept_and_repeat(self):
        self.reply(MARK, "DONE 1")
        self.assertEqual(self.current()["status"], "resolved")
        self.reply(MARK, "done 1")
        self.assertIn("already marked done", self.svc.sms.to(MARK)[-1])

    def test_unknown_numbers_and_promos_are_ignored(self):
        before = len(self.svc.sms.sent)
        self.reply("+639990000000", "OK 1")
        self.reply("SMART", "Promo: load now")
        self.assertEqual(len(self.svc.sms.sent), before)  # no replies to strangers
        self.assertEqual(self.current()["status"], "open")

    def test_help_and_unknown_report(self):
        self.reply(RALPH, "hello?")
        self.assertIn("reply OK <number>", self.svc.sms.to(RALPH)[-1])
        n = len(self.svc.sms.to(RALPH))
        self.reply(RALPH, "what")  # help is rate-limited: no second help text
        self.assertEqual(len(self.svc.sms.to(RALPH)), n)
        self.reply(RALPH, "OK 99")
        self.assertIn("#99 not found", self.svc.sms.to(RALPH)[-1])

    def test_escalation_after_no_reply(self):
        self.svc.bootstrap_admin("Boss", "boss", "boss@example.com", "BossPass123")
        boss = next(u for u in self.svc.list_users() if u["username"] == "boss")
        self.svc.store.transact(self.svc.C_USERS, boss["uid"],
                                lambda u: {**u, "smsPhone": "+639190000000", "smsAlerts": True})
        self.assertEqual(self.svc.two_way.check_escalations(), 0)  # too early
        self.svc.store.transact(self.svc.C_REPORTS, self.report["id"],
                                lambda r: {**r, "notifiedAt": iso(utcnow() - timedelta(minutes=11))})
        self.assertEqual(self.svc.two_way.check_escalations(), 1)
        self.assertIn("UNANSWERED #1", self.svc.sms.to("+639190000000")[-1])
        self.assertEqual(self.svc.two_way.check_escalations(), 0)  # only once
        self.assertTrue(self.current()["escalatedAt"])

    def test_accepted_reports_are_not_escalated(self):
        self.reply(RALPH, "OK 1")
        self.svc.store.transact(self.svc.C_REPORTS, self.report["id"],
                                lambda r: {**r, "notifiedAt": iso(utcnow() - timedelta(minutes=30))})
        self.assertEqual(self.svc.two_way.check_escalations(), 0)

    def test_simulate_reply_button(self):
        result = self.svc.simulate_sms_reply(self.admin, RALPH, "OK 1")
        self.assertEqual(result, {"action": "accepted", "ref": 1})


class ModemInbox(unittest.TestCase):
    def test_reads_and_deletes_messages_from_the_sim(self):
        fake = FakeSerial()
        stored = {1: ("+639171234567", "OK 12"), 2: ("+639181234567", "Done 12")}
        deleted = []
        orig = fake.write

        def write(data):
            cmd = data.decode(errors="replace")
            if cmd.startswith('AT+CMGL="ALL"'):
                body = "".join(f'\r\n+CMGL: {i},"REC UNREAD","{p}","","26/10/24,21:55:12+32"\r\n{t}'
                               for i, (p, t) in stored.items())
                fake.out += (body + "\r\n\r\nOK\r\n").encode()
            elif cmd.startswith("AT+CMGD="):
                deleted.append(int(cmd.strip()[8:]))
                fake.out += b"\r\nOK\r\n"
            else:
                orig(data)
        fake.write = write
        modem = ModemSender("/dev/fake", 9600, opener=lambda *_: fake)
        msgs = modem.read_inbox()
        self.assertEqual([(m.sender, m.text) for m in msgs], list(stored.values()))
        self.assertEqual(deleted, [1, 2])


if __name__ == "__main__":
    unittest.main()
