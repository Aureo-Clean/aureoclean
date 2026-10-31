"""Deleting reports: a deleted bin (30 days, restorable), hidden from every list."""
from backend.service import BadRequest, NotFound
from tests.test_service import Base


class Bin(Base):
    def setUp(self):
        super().setUp()
        self.r = self.svc.create_report(self.admin, "facility", "Broken faucet", "2nd Floor · Male")

    def test_delete_restore_purge(self):
        with self.assertRaises(BadRequest):
            self.svc.delete_report(self.admin, self.r["id"], "")          # a reason is required
        msg = self.svc.delete_report(self.admin, self.r["id"], "duplicate of #2")["message"]
        self.assertIn("deleted bin", msg)
        self.assertEqual(self.svc.list_reports(), [])
        self.assertEqual(self.svc.admin_overview()["openReports"], 0)
        [d] = self.svc.deleted_reports()
        self.assertEqual((d["deleteReason"], d["deletedBy"]["username"], d["daysLeft"]), ("duplicate of #2", "headadmin", 29))
        self.svc.restore_report(self.admin, self.r["id"])
        [back] = self.svc.list_reports()
        self.assertEqual(back["title"], "Broken faucet")
        self.assertNotIn("deletedAt", back)
        self.assertEqual(back["notes"][-1]["text"], "Restored from the deleted bin")
        self.svc.delete_report(self.admin, self.r["id"], "test report")
        self.svc.purge_report(self.admin, self.r["id"])
        self.assertEqual(self.svc.deleted_reports(), [])
        with self.assertRaises(NotFound):
            self.svc.restore_report(self.admin, self.r["id"])
        actions = [a["action"] for a in self.svc.list_audit()]
        for a in ("report_deleted", "report_restored", "report_purged"):
            self.assertIn(a, actions)

    def test_old_ones_are_emptied_after_30_days(self):
        self.svc.delete_report(self.admin, self.r["id"], "prank")
        self.svc.store.transact(self.svc.C_DELETED_REPORTS, self.r["id"], lambda d: {**d, "purgeAt": "2000-01-01T00:00:00+00:00"})
        self.assertEqual(self.svc.purge_old_deleted(), 1)
        self.assertEqual(self.svc.deleted_reports(), [])


class LocalData(Base):
    def test_password_needed_and_secrets_hidden(self):
        from backend.service import Forbidden as Unauthorized
        self.svc.create_report(self.admin, "facility", "Leak", "2nd Floor · Male")
        st = self.svc.data_status()
        self.assertEqual(st["mode"], "Local only")
        self.assertEqual(next(c for c in st["collections"] if c["key"] == "reports")["count"], 1)
        with self.assertRaises(Unauthorized):
            self.svc.browse_data(self.admin, "", "reports")
        with self.assertRaises(Unauthorized):
            self.svc.unlock_data(self.admin, "wrong-password")
        token = self.svc.unlock_data(self.admin, "CleanPass123")["token"]
        rows = self.svc.browse_data(self.admin, token, "reports")["rows"]
        self.assertEqual(len(rows), 1)
        self.svc.submit_janitor_request("Mark Agot", "MarkAgot", "mark@example.com", "+639171234567")
        req = self.svc.list_requests("pending")[0]
        self.svc.approve_request(self.admin, req["id"])
        data = self.svc.browse_data(self.admin, token, "janitorRequests")["rows"][0]["data"]
        self.assertEqual(data["activationCodeHash"], "•••• (hidden)")
        actions = [a["action"] for a in self.svc.list_audit()]
        self.assertIn("local_data_opened", actions)
        self.assertIn("local_data_refused", actions)
