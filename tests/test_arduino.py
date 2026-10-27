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

    dashboards = None   # None = the key may not read dashboards (403)
    dashboards_v3 = None  # None = no v3 API here (404) → the app falls back to v2

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("get", url))
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("/iot/v3/dashboards"):
            return Resp(404, {}) if self.dashboards_v3 is None else Resp(200, self.dashboards_v3)
        if url.endswith("/iot/v2/dashboards"):
            return Resp(403, {}) if self.dashboards is None else Resp(200, self.dashboards)
        if url.endswith("/timeseries"):
            self.calls.append(("params", params))
            if "/properties/p1/" in url:
                return Resp(200, {"data": [{"time": "2026-10-25T04:00:00Z", "value": 70}, {"time": "2026-10-25T04:06:00Z", "value": 71.5}]})
            return Resp(500, {})
        if url.endswith("/iot/v2/things"):
            return Resp(200, [{"id": "t1", "name": "Restroom 2F Male", "device_name": "ESP32-2FM"}])
        if url.endswith("/iot/v2/things/t1/properties"):
            return Resp(200, [
                {"id": "p1", "variable_name": "waterLevel", "type": "PERCENTAGE", "last_value": 72.5, "value_updated_at": "2026-10-25T05:00:00Z",
                 "permission": "READ_ONLY"},
                {"id": "p2", "variable_name": "smoke_detected", "type": "BOOL", "last_value": False, "value_updated_at": "2026-10-25T05:00:00Z"},
                {"id": "p3", "variable_name": "temperature", "type": "TEMPERATURE_C", "last_value": 29.4, "value_updated_at": "2026-10-25T05:00:00Z"},
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
        self.assertIn("1 page (Restroom 2F Male), 3 sensor values", r["message"])
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

    def test_pages_follow_arduino_dashboards(self):
        self.fake.dashboards = [
            {"id": "d2", "name": "Page 2", "widgets": []},
            {"id": "d1", "name": "Urinal", "widgets": [
                {"id": "w2", "type": "Chart", "name": "Water", "x": 0, "y": 2, "width": 4, "height": 2,
                 "options": {"nested": {"a": 1}}, "variables": [{"id": "p1", "thing_id": "t1", "variable_name": "waterLevel"}]},
                {"id": "w1", "type": "Push Button", "name": "Smoke", "x": 0, "y": 0, "width": 1, "height": 1,
                 "options": {"min": 0, "max": 1}, "variables": [{"id": "p2", "thing_id": "t1", "variable_name": "smoke_detected"}]},
            ]},
        ]
        r = self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        self.assertIn("2 pages (Page 2, Urinal)", r["message"])
        pages = self.svc.sensors()["pages"]
        self.assertEqual([p["name"] for p in pages], ["Page 2", "Urinal"])     # same names as in Arduino
        self.assertEqual(pages[0]["widgets"], [])
        urinal = pages[1]["widgets"]
        self.assertEqual([w["id"] for w in urinal], ["w1", "w2"])               # top to bottom like Arduino
        self.assertEqual(urinal[0]["type"], "pushbutton")
        self.assertEqual(urinal[0]["options"], {"min": 0, "max": 1})
        self.assertEqual(urinal[1]["values"][0]["value"], 72.5)                 # live value of the linked variable
        self.assertEqual(urinal[1]["values"][0]["unit"], "%")
        # Small charts: the history of number variables; a failing series is just left out.
        hist = self.svc.sensor_history()
        self.assertEqual(hist["series"], {"p1": [[1792900800, 70.0], [1792901160, 71.5]]})
        params = [c[1] for c in self.fake.calls if c[0] == "params"][0]
        self.assertEqual(params["interval"], 360)

    def test_multi_page_dashboard(self):
        def widget(wid, page, var, y):
            return {"id": wid, "type": "Value", "page_id": page, "x": 0, "y": y, "width": 1, "height": 1, "options": {},
                    "variables": [{"id": var, "thing_id": "t1", "variable_name": var}]}
        self.fake.dashboards_v3 = [{"id": "d1", "name": "AureoClean", "pages": [
            {"id": "pg3", "name": "Page 3", "position": 2},
            {"id": "pg1", "name": "Urinal", "position": 0},
            {"id": "pg2", "name": "Page 2", "position": 1},
        ], "widgets": [widget("w1", "pg1", "p1", 0), widget("w2", "pg2", "p3", 0), widget("w3", "gone", "p2", 1)]}]
        self.fake.dashboards = [{"id": "d1", "name": "AureoClean", "widgets": []}]   # v2 would hide the pages
        r = self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        self.assertIn("3 pages (Urinal, Page 2, Page 3)", r["message"])
        pages = self.svc.sensors()["pages"]
        self.assertEqual([(p["name"], p["dashboard"]) for p in pages],
                         [("Urinal", "AureoClean"), ("Page 2", "AureoClean"), ("Page 3", "AureoClean")])
        self.assertEqual([w["id"] for w in pages[0]["widgets"]], ["w1", "w3"])   # unknown page → first page
        self.assertEqual([w["id"] for w in pages[1]["widgets"]], ["w2"])
        self.assertEqual(pages[2]["widgets"], [])                                # new, empty page still shows
        self.assertEqual(len({p["id"] for p in pages}), 3)
        self.assertFalse(any("/iot/v2/dashboards" in str(c[1]) for c in self.fake.calls))

    def test_without_dashboards_one_page_per_thing(self):
        self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        page = self.svc.sensors()["pages"][0]
        self.assertEqual(page["name"], "Restroom 2F Male")
        self.assertEqual(len(page["widgets"]), 3)

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


class FakeArduinoWithControls(FakeArduino):
    """Adds writable variables (switch, number, text, colour) and accepts PUT …/publish."""

    def __init__(self):
        super().__init__()
        self.published = []
        self.extra = [
            {"id": "f1", "variable_name": "exhaustFan", "type": "BOOL", "last_value": False, "permission": "READ_WRITE"},
            {"id": "n1", "variable_name": "targetLevel", "type": "INT", "last_value": 40, "permission": "READ_WRITE"},
            {"id": "m1", "variable_name": "lcdMessage", "type": "CHARSTRING", "last_value": "Hi", "permission": "READ_WRITE"},
            {"id": "c1", "variable_name": "ledColor", "type": "COLOR", "last_value": {"hue": 1}, "permission": "READ_WRITE"},
        ]
        self.dashboards = [{"id": "d1", "name": "Urinal", "widgets": [
            {"id": "w1", "type": "Slider", "name": "Target", "x": 0, "y": 0, "width": 1, "height": 1,
             "options": {"min": 0, "max": 100}, "variables": [{"id": "n1", "thing_id": "t1", "variable_name": "targetLevel"}]}]}]

    def get(self, url, headers=None, params=None, timeout=None):
        resp = super().get(url, headers, params, timeout)
        if url.endswith("/iot/v2/things/t1/properties"):
            return Resp(200, resp.json() + self.extra)
        if url.endswith("/timeseries") and "/properties/n1/" in url:
            self.calls.append(("series", params))
            return Resp(200, {"data": [{"time": "2026-10-27T01:00:00Z", "value": 40}, {"time": "2026-10-27T02:00:00Z", "value": 55}]})
        return resp

    def put(self, url, headers=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        self.published.append((url.split("/iot/v2/things/")[1], json))
        return Resp(200, {})


class Controls(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeArduinoWithControls()
        self.svc.arduino.http = self.fake
        self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")

    def janitor(self):
        req = self.svc.submit_janitor_request("Ana Reyes", "AnaReyes", "ana@example.com", "")
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor("AnaReyes", code, PW)
        return self.svc.require_session(self.svc.login("AnaReyes", PW)["token"], "janitor")

    def test_admin_switches_a_fan(self):
        self.assertTrue(self.svc.sensors(self.admin)["canControl"])
        r = self.svc.sensor_control(self.admin, "t1", "f1", True)
        self.assertEqual(r["message"], "Exhaust fan → On")
        self.assertEqual(self.fake.published, [("t1/properties/f1/publish", {"value": True})])
        # Every screen sees it straight away, before the device's next reading.
        fan = next(v for t in self.svc.sensors(self.admin)["things"] for v in t["properties"] if v["id"] == "f1")
        self.assertIs(fan["value"], True)
        audit = [d for _, d in self.svc.store.query(self.svc.C_AUDIT) if d.get("action") == "sensor_control"]
        self.assertEqual(audit[-1]["details"]["variable"], "exhaustFan")

    def test_checks(self):
        with self.assertRaises(BadRequest):
            self.svc.sensor_control(self.admin, "t1", "p1", 10)          # waterLevel is read-only
        with self.assertRaises(BadRequest):
            self.svc.sensor_control(self.admin, "t1", "f1", "yes")       # on/off needs true/false
        with self.assertRaises(BadRequest) as cm:
            self.svc.sensor_control(self.admin, "t1", "n1", 150)         # the slider goes 0–100
        self.assertIn("between 0 and 100", str(cm.exception))
        self.assertEqual(self.svc.sensor_control(self.admin, "t1", "n1", 55.0)["value"], 55)   # INT stays whole
        self.assertEqual(self.svc.sensor_control(self.admin, "t1", "m1", "  Clean   now ")["value"], "Clean now")
        with self.assertRaises(BadRequest):
            self.svc.sensor_control(self.admin, "t1", "c1", "red")       # colours: use Arduino Cloud
        from backend.service import NotFound
        with self.assertRaises(NotFound):
            self.svc.sensor_control(self.admin, "t1", "nope", True)

    def test_janitors_only_when_allowed(self):
        ana = self.janitor()
        self.assertFalse(self.svc.sensors(ana)["canControl"])
        from backend.service import Forbidden
        with self.assertRaises(Forbidden):
            self.svc.sensor_control(ana, "t1", "f1", True)
        self.svc.set_arduino_options(self.admin, True)
        self.assertTrue(self.svc.sensors(ana)["canControl"])
        self.svc.sensor_control(ana, "t1", "f1", False)
        # Reconnecting with a new key keeps the choice.
        self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        self.assertTrue(self.svc.arduino.settings()["janitorsControl"])

    def test_too_many_changes(self):
        for _ in range(30):
            self.svc.sensor_control(self.admin, "t1", "f1", True)
        with self.assertRaises(BadRequest):
            self.svc.sensor_control(self.admin, "t1", "f1", True)

    def test_detail_chart_history(self):
        r = self.svc.sensor_series("t1", "n1", 24)
        self.assertEqual([v for _, v in r["points"]], [40.0, 55.0])
        self.assertEqual(self.fake.calls[-1][1]["interval"], 720)        # 24 h in ~120 points
        n = len(self.fake.calls)
        self.svc.sensor_series("t1", "n1", 24)
        self.assertEqual(len(self.fake.calls), n)                         # cached for a minute
