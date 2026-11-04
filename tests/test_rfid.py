"""ID-card (RFID, RC522) sign-in: the driver, tap debouncing, linking and signing in."""
import unittest

from backend.rfid import RC522, CardReader, ReaderError
from backend.service import BadRequest, Forbidden, ServiceUnavailable, Unauthorized
from tests.test_service import PW, Base


class FakeChip:
    """Register-level stand-in for an MFRC522 with an ISO 14443-A card (4- or 7-byte UID) on it."""

    def __init__(self, uid=None, version=0x92):
        self.regs = {0x37: version, 0x14: 0x80}
        self.fifo, self.out = [], []
        self.uid = uid
        self.halted = False

    def card_answer(self, data):
        if not self.uid:
            return None
        cmd = data[0]
        u = self.uid
        cl1 = [0x88] + u[0:3] if len(u) == 7 else u[0:4]
        cl2 = u[3:7] if len(u) == 7 else None
        bcc = lambda b: [b[0] ^ b[1] ^ b[2] ^ b[3]]  # noqa: E731
        if cmd == 0x52:
            self.halted = False
            return [0x44 if cl2 else 0x04, 0x00]
        if self.halted:
            return None
        if data[:2] == [0x93, 0x20]:
            return cl1 + bcc(cl1)
        if data[:2] == [0x95, 0x20] and cl2:
            return cl2 + bcc(cl2)
        if data[:2] in ([0x93, 0x70], [0x95, 0x70]):
            return [0x04 if (data[0] == 0x93 and cl2) else 0x08, 0xB6, 0xDD]
        if cmd == 0x50:
            self.halted = True
            return None
        return None

    def xfer2(self, b):
        addr = (b[0] >> 1) & 0x3F
        if b[0] & 0x80:                       # read
            if addr == 0x09:
                return [0, self.out.pop(0) if self.out else 0]
            if addr == 0x0A:
                return [0, len(self.out)]
            if addr == 0x04:
                return [0, self.regs.get(0x04, 0)]
            if addr == 0x05:
                return [0, 0x04]              # CRC done
            return [0, self.regs.get(addr, 0)]
        val = b[1]
        if addr == 0x09:
            self.fifo.append(val)
        elif addr == 0x0A and val & 0x80:
            self.fifo, self.out = [], []
        elif addr == 0x01:
            if val == 0x0C:                   # transceive: answer once BitFraming StartSend is set (below)
                self.pending = list(self.fifo)
            if val == 0x03:
                self.fifo = []
            self.regs[0x01] = val
        elif addr == 0x0D:
            if val & 0x80 and self.regs.get(0x01) == 0x0C:
                ans = self.card_answer(self.pending)
                self.fifo = []
                if ans is None:
                    self.regs[0x04] = 0x01    # timer: no answer
                else:
                    self.out = ans
                    self.regs[0x04] = 0x30
            self.regs[0x0D] = val
        else:
            self.regs[addr] = val
        return [0, 0]


class Driver(unittest.TestCase):
    def test_reads_a_4_byte_uid(self):
        chip = FakeChip([0x04, 0xA1, 0xB2, 0xC3])
        self.assertEqual(RC522(spi=chip).read_uid(), "04A1B2C3")

    def test_reads_a_7_byte_uid(self):
        chip = FakeChip([0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
        self.assertEqual(RC522(spi=chip).read_uid(), "04112233445566")

    def test_no_card_and_no_chip(self):
        self.assertIsNone(RC522(spi=FakeChip(None)).read_uid())
        with self.assertRaises(ReaderError):
            RC522(spi=FakeChip(None, version=0x00))

    def test_a_card_held_on_the_reader_counts_once(self):
        t = [0.0]
        r = CardReader(lambda uid: None, clock=lambda: t[0])
        self.assertTrue(r.seen("AA"))
        for _ in range(20):
            t[0] += 0.15
            self.assertFalse(r.seen("AA"))
        t[0] += 3
        self.assertTrue(r.seen("AA"))          # lifted and tapped again
        self.assertTrue(r.seen("BB"))          # another card at once


class CardBase(Base):
    def setUp(self):
        super().setUp()
        self.svc.card_reader.available = True
        rid, code = self.approved()
        self.svc.activate_janitor("markagot", code, "Janitor123")
        self.jan = self.svc.require_session(self.svc.login("markagot", "Janitor123")["token"])

    def tap(self, uid):
        self.svc.card_scanned(uid)

    def link(self, who, uid, target=None):
        self.svc.start_card_link(who, target)
        self.tap(uid)
        return self.svc.card_link_status(who, target)


class CardSignIn(CardBase):

    def test_janitor_links_a_card_then_signs_in_by_tapping(self):
        self.assertEqual(self.link(self.jan, "04A1B2C3")["state"], "linked")
        profile = self.svc.store.get(self.svc.C_USERS, self.jan.uid)
        self.assertNotIn("04A1B2C3", str(profile))             # only a keyed hash is kept
        self.assertTrue(self.svc.me(self.jan)["hasCard"])
        self.assertEqual(self.svc.card_poll(0)["result"], "none")
        self.tap("04a1b2c3")
        r = self.svc.card_poll(0)
        self.assertEqual(r["result"], "signedIn")
        self.assertEqual(r["user"]["username"].lower(), "markagot")
        self.assertTrue(self.svc.require_session(r["token"]))
        self.assertEqual(self.svc.card_poll(0)["result"], "none")   # one tap, one sign-in
        self.assertIn("card_login", [a["action"] for a in self.svc.list_audit()])

    def test_unknown_card_and_old_taps(self):
        self.tap("DEADBEEF")
        r = self.svc.card_poll(0)
        self.assertEqual(r["result"], "unknown")
        self.assertIn("isn't linked", r["message"])
        self.link(self.jan, "01020304")
        self.tap("01020304")
        self.svc._card_scan["at"] -= 60                             # tapped a minute ago: forgotten
        self.assertEqual(self.svc.card_poll(0)["result"], "none")

    def test_admin_card_needs_the_pin(self):
        self.svc.set_lock_pin(self.admin, PW, "4826")
        self.link(self.admin, "0A0B0C0D")
        self.tap("0A0B0C0D")
        r = self.svc.card_poll(0)
        self.assertEqual((r["result"], r["usePin"]), ("needPin", True))
        self.assertNotIn("token", r)
        with self.assertRaises(BadRequest):
            self.svc.card_pin(r["ticket"], "0000")
        ok = self.svc.card_pin(r["ticket"], "4826")
        self.assertEqual(ok["user"]["role"], "admin")
        with self.assertRaises(Unauthorized):
            self.svc.card_pin(r["ticket"], "4826")                 # a ticket works once

    def test_admin_without_a_pin_uses_the_password_and_three_wrong_tries_end_it(self):
        self.link(self.admin, "0A0B0C0D")
        self.tap("0A0B0C0D")
        r = self.svc.card_poll(0)
        self.assertFalse(r["usePin"])
        for _ in range(2):
            with self.assertRaises(BadRequest):
                self.svc.card_pin(r["ticket"], "nope")
        with self.assertRaises(Unauthorized):
            self.svc.card_pin(r["ticket"], "nope")
        with self.assertRaises(Unauthorized):
            self.svc.card_pin(r["ticket"], PW)

    def test_a_card_belongs_to_one_account(self):
        self.link(self.jan, "11223344")
        r = self.link(self.admin, "11223344")
        self.assertEqual(r["state"], "taken")
        self.assertIn("Mark Agot", r["message"])
        self.svc.remove_card(self.admin, self.jan.uid)              # admin removes a lost card
        self.assertEqual(self.link(self.admin, "11223344")["state"], "linked")

    def test_disabled_account_and_link_timeout(self):
        self.link(self.jan, "55667788")
        self.svc.set_user_status(self.admin, self.jan.uid, False)
        self.tap("55667788")
        self.assertEqual(self.svc.card_poll(0)["result"], "error")
        self.svc.start_card_link(self.admin)
        self.svc._card_link["until"] -= 60
        self.assertEqual(self.svc.card_link_status(self.admin)["state"], "expired")

    def test_janitor_unlocks_the_lock_screen_with_their_own_card(self):
        self.link(self.jan, "CAFEBABE")
        self.tap("DEADBEEF")
        self.assertEqual(self.svc.card_unlock(self.jan, 0)["result"], "wrongCard")
        self.tap("CAFEBABE")
        self.assertEqual(self.svc.card_unlock(self.jan, 0)["result"], "unlocked")

    def test_reader_missing(self):
        self.svc.card_reader.available = False
        with self.assertRaises(ServiceUnavailable):
            self.svc.start_card_link(self.jan)
        with self.assertRaises(Forbidden):
            self.svc.start_card_link(self.jan, self.admin.uid)


class CardOnly(CardBase):
    def test_turning_it_on_needs_the_reader_and_your_own_card(self):
        with self.assertRaises(BadRequest):
            self.svc.set_card_only(self.admin, True)          # admin has no card yet
        self.link(self.admin, "0A0B0C0D")
        self.assertTrue(self.svc.set_card_only(self.admin, True)["active"])
        self.svc.card_reader.available = False
        with self.assertRaises(BadRequest):
            self.svc.set_card_only(self.admin, True)

    def test_pi_screen_refuses_passwords_but_the_wifi_website_and_a_broken_reader_dont(self):
        self.link(self.admin, "0A0B0C0D")
        self.svc.set_card_only(self.admin, True)
        with self.assertRaises(Forbidden):
            self.svc.login("markagot", "Janitor123", at_pi=True)
        self.assertTrue(self.svc.login("markagot", "Janitor123", at_pi=False)["token"])   # phone on the Wi-Fi
        self.assertTrue(self.svc.card_status()["cardOnly"])
        self.svc.card_reader.available = False                                            # reader broke
        self.assertTrue(self.svc.login("markagot", "Janitor123", at_pi=True)["token"])
        self.svc.card_reader.available = True
        import os
        os.environ["AUREOCLEAN_CARD_ONLY"] = "off"                                         # emergency override
        try:
            self.assertTrue(self.svc.login("markagot", "Janitor123", at_pi=True)["token"])
        finally:
            del os.environ["AUREOCLEAN_CARD_ONLY"]
        access = self.svc.card_access(self.admin)
        self.assertEqual((access["withCard"], access["withoutCard"]), (1, ["Mark Agot"]))


if __name__ == "__main__":
    unittest.main()
