"""HTTP-level tests (role checks over the real routes).

Needs FastAPI + httpx:  .venv/bin/pip install httpx && .venv/bin/python -m unittest discover -s tests -v
Skipped automatically if they are not installed.
"""
import unittest

try:
    from fastapi.testclient import TestClient

    from backend.main import create_app
except ImportError:  # pragma: no cover
    TestClient = None

from tests.test_service import PW, make_service


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", PW)
        self.client = TestClient(create_app(self.svc))

    def login(self, ident, pw):
        r = self.client.post("/auth/login", json={"identifier": ident, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def test_full_flow_and_role_separation(self):
        admin = self.login("headadmin", PW)
        r = self.client.post("/register/janitor", json={"fullName": "Mark Agot", "username": "MarkAgot", "email": "mark@example.com"})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["requestId"]

        self.assertEqual(self.client.get("/admin/requests").status_code, 401)
        pending = self.client.get("/admin/requests", params={"status": "pending"}, headers=admin).json()
        self.assertEqual([p["id"] for p in pending], [rid])

        code = self.client.post(f"/admin/requests/{rid}/approve", headers=admin).json()["code"]
        r = self.client.post("/register/activate", json={"username": "MarkAgot", "code": code, "password": "Janitor123"})
        self.assertEqual(r.status_code, 200, r.text)

        janitor = self.login("markagot", "Janitor123")
        self.assertEqual(self.client.get("/admin/users", headers=janitor).status_code, 403)
        self.assertEqual(self.client.get("/janitor/overview", headers=janitor).status_code, 200)
        self.assertEqual(self.client.get("/janitor/overview", headers=admin).status_code, 403)

        self.assertEqual(self.client.post("/auth/logout", headers=janitor).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=janitor).status_code, 401)

    def test_errors_are_readable(self):
        r = self.client.post("/auth/login", json={"identifier": "headadmin", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Incorrect", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
