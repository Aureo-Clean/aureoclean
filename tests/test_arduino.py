"""Live sensors from Arduino Cloud with an API key (no Arduino sign-in in the app)."""
import os
import stat

from backend.arduino import nice_name
from backend.service import BadRequest, ServiceUnavailable
from tests.test_service import PW, Base


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.ok = 200 <= status < 300

    def json(self):
        return self._data


class FakeArduino:
    """Answers like api2.arduino.cc."""

    def __init__(self, good_secret="s3cret-secret"):
        self.good_secret = good_secret
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("token", data))
        if data["client_secret"] != self.good_secret or data["audience"] != "https://api2.arduino.cc/iot":
            return Resp(401, {"error": "invalid_client"})
        return Resp(200, {"access_token": "tok", "expires_in": 300})

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url))
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("/iot/v2/things"):
            return Resp(200, [{"id": "t1", "name": "Restroom 2F Male", "device_name": "ESP32-2FM"}])
        if url.endswith("/iot/v2/things/t1/properties"):
            return Resp(200, [
                {"variable_name": "waterLevel", "type": "PERCENTAGE", "last_value": 72.5, "value_updated_at": "2026-10-25T05:00:00Z",
                 "permission": "READ_ONLY"},
                {"variable_name": "smoke_detected", "type": "BOOL", "last_value": False, "value_updated_at": "2026-10-25T05:00:00Z"},
                {"variable_name": "temperature", "type": "TEMPERATURE_C", "last_value": 29.4, "value_updated_at": "2026-10-25T05:00:00Z"},
            ])
        return Resp(404, {})


class LiveSensors(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeArduino()
        self.svc.arduino.http = self.fake

    def test_not_connected(self):
        self.assertEqual(self.svc.sensors(), {"configured": False, "things": []})

    def test_connect_and_read(self):
        r = self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        self.assertIn("1 thing(s), 3 sensor value(s)", r["message"])
        data = self.svc.sensors()
        thing = data["things"][0]
        self.assertEqual((thing["name"], thing["device"]), ("Restroom 2F Male", "ESP32-2FM"))
        by = {p["name"]: p for p in thing["properties"]}
        self.assertEqual(by["waterLevel"]["label"], "Water level")
        self.assertEqual(by["waterLevel"]["unit"], "%")
        self.assertEqual(by["temperature"]["unit"], "°C")
        self.assertIs(by["smoke_detected"]["value"], False)
        # Several screens asking at once share one answer (Arduino rate limits).
        n = len(self.fake.calls)
        self.svc.sensors()
        self.assertEqual(len(self.fake.calls), n)
        # The secret file is private.
        self.assertEqual(stat.S_IMODE(os.stat(self.svc.arduino.file).st_mode), 0o600)

    def test_wrong_secret_is_explained(self):
        with self.assertRaises(BadRequest) as cm:
            self.svc.set_arduino(self.admin, "client-id-123", "wrong-secret")
        self.assertIn("refused the API key", str(cm.exception))
        self.svc.arduino.save("client-id-123", "wrong-secret")
        with self.assertRaises(ServiceUnavailable):
            self.svc.sensors()

    def test_janitors_can_see_sensors(self):
        self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        req = self.svc.submit_janitor_request("Ana Reyes", "AnaReyes", "ana@example.com", "")
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor("AnaReyes", code, PW)
        self.svc.require_session(self.svc.login("AnaReyes", PW)["token"], "janitor")
        self.assertTrue(self.svc.sensors()["configured"])

    def test_names(self):
        self.assertEqual(nice_name("waterLevel"), "Water level")
        self.assertEqual(nice_name("soap_dispenser_2"), "Soap dispenser 2")
        self.assertEqual(nice_name("CO2ppm"), "Co2ppm")


class FastUnlock(Base):
    def test_pin_checks_dont_hit_the_database_every_time(self):
        self.svc.set_lock_pin(self.admin, PW, "4826")
        self.svc.unlock(self.admin, "4826")
        reads = []
        original = self.svc.store.get
        self.svc.store.get = lambda c, d: (reads.append(c), original(c, d))[1]
        self.svc.unlock(self.admin, "4826")
        self.assertNotIn(self.svc.C_USERS, reads)
        self.svc.store.get = original
        # Changing the PIN takes effect at once.
        self.svc.set_lock_pin(self.admin, PW, "7391")
        with self.assertRaises(BadRequest):
            self.svc.unlock(self.admin, "4826")
        self.svc.unlock(self.admin, "7391")
