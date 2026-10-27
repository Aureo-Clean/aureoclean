"""Sensor alerts: a reading over its limit becomes a report and texts (fixed rules, no AI)."""
from datetime import datetime, timezone

from backend.sensor_alerts import AFTER_DONE_SECONDS, CLEAR_SECONDS, kind_of, show, suggest
from backend.service import BadRequest
from tests.test_arduino import Resp
from tests.test_service import Base
from tests.test_sms import FakeSender

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeCloud:
    """Arduino Cloud with values the test changes."""

    def __init__(self, clock):
        self.clock = clock
        self.values = {"temp": 29.0, "gas": 120000.0, "smoke": False}

    def post(self, url, data=None, headers=None, timeout=None):
        return Resp(200, {"access_token": "tok", "expires_in": 300})

    def get(self, url, headers=None, params=None, timeout=None):
        at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")
        if url.endswith("/iot/v2/things"):
            return Resp(200, [{"id": "t1", "name": "2F Male node"}])
        if url.endswith("/iot/v2/things/t1/properties"):
            return Resp(200, [
                {"id": "temp", "variable_name": "temperature", "type": "TEMPERATURE_C", "last_value": self.values["temp"], "value_updated_at": at},
                {"id": "gas", "variable_name": "bme680Gas", "type": "FLOAT", "last_value": self.values["gas"], "value_updated_at": at},
                {"id": "smoke", "variable_name": "smoke", "type": "BOOL", "last_value": self.values["smoke"], "value_updated_at": at},
            ])
        if url.endswith("/timeseries"):
            return Resp(200, {"data": [{"time": "2026-10-27T08:00:00Z", "value": 100000}, {"time": "2026-10-27T08:30:00Z", "value": 110000}]})
        return Resp(403, {})


class SensorAlerts(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.cloud = FakeCloud(self.clock)
        self.svc.arduino.http = self.cloud
        self.svc.sensor_alerts.clock = self.clock
        self.svc.sync_notifications = True
        self.sms = FakeSender()
        self.svc.sms = self.sms
        self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        self.janitor = self.make_janitor()

    def make_janitor(self):
        req = self.svc.submit_janitor_request("Ana Cruz", "ana", "ana@example.com", "+639171234567")
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor("ana", code, "Janitor123")
        uid = next(u["uid"] for u in self.svc.list_users() if u["username"] == "ana")
        self.svc._update_profile(uid, lambda p: p.update(smsPhone="+639171234567", smsAlerts=True))
        return self.svc.require_session(self.svc.login("ana", "Janitor123")["token"])

    def tick(self, seconds=15, **values):
        self.clock.t += seconds
        self.cloud.values.update(values)
        self.svc.arduino._cache = None
        return self.svc.sensor_alerts.check()

    def rule(self, **kw):
        data = {"thingId": "t1", "propertyId": "temp", "op": "above", "limit": 35, "level": "urgent", "minutes": 2,
                "location": "2nd Floor · Male"}
        data.update(kw)
        return self.svc.save_sensor_alert(self.admin, data)["rule"]

    def sensor_reports(self):
        return [r for r in self.svc.list_reports() if r.get("source") == "sensor"]

    def test_hot_for_two_minutes_makes_one_report_and_texts_janitors(self):
        rule = self.rule()
        self.assertEqual(self.svc.sensor_alerts.describe(rule), "Temperature above 35 °C for 2 min")
        self.assertEqual(self.tick(temp=36.5), 0)          # over, but not for 2 minutes yet
        self.assertEqual(self.svc.sensors(self.admin)["alerts"]["temp"][0]["state"], "over")
        self.assertEqual(self.tick(60), 0)
        self.assertEqual(self.tick(70, temp=38.2), 1)      # 2 min 10 s over → report
        [r] = self.sensor_reports()
        self.assertEqual((r["title"], r["priority"], r["category"], r["location"]),
                         ("Temperature too high: 38.2 °C", "high", "facility", "2nd Floor · Male"))
        self.assertEqual(r["sensor"]["limit"], 35)
        self.assertEqual(len(self.sms.sent), 1)
        to, text = self.sms.sent[0]
        self.assertEqual(to, "+639171234567")
        self.assertTrue(text.startswith(f"AureoClean SENSOR ALERT #{r['smsRef']}: Temperature too high: 38.2 C"), text)
        self.assertIn(f"Reply OK {r['smsRef']}", text)
        self.assertLessEqual(len(text), 160)
        # Still hot: no second report, no second text.
        for _ in range(10):
            self.assertEqual(self.tick(60, temp=40), 0)
        self.assertEqual(len(self.sensor_reports()), 1)
        self.assertEqual(len(self.sms.sent), 1)
        self.assertEqual(self.svc.sensors(self.janitor)["alerts"]["temp"][0]["ref"], r["smsRef"])
        self.assertIn("sensor_alert", [a["action"] for a in self.svc.list_audit()])

    def test_back_to_normal_adds_a_note_and_over_again_reuses_the_open_report(self):
        self.rule(minutes=0)
        self.assertEqual(self.tick(temp=37), 1)
        self.tick(30, temp=35.5)        # still over
        self.tick(30, temp=34.9)        # under, but inside the 2 % margin: not "normal" yet
        self.tick(30, temp=33)
        self.tick(CLEAR_SECONDS, temp=33)
        [r] = self.sensor_reports()
        self.assertTrue(any("Back to normal: Temperature 33 °C (worst was 37 °C)" in n["text"] for n in r["notes"]))
        self.assertEqual(self.tick(30, temp=36), 0)   # over again while the job is open → a note
        [r] = self.sensor_reports()
        self.assertTrue(any("Over the limit again" in n["text"] for n in r["notes"]))
        self.assertEqual(len(self.sms.sent), 1)

    def test_after_done_waits_before_a_new_report(self):
        self.rule(minutes=0)
        self.tick(temp=37)
        [r] = self.sensor_reports()
        self.svc.janitor_take(self.janitor, r["id"])
        self.svc.janitor_done(self.janitor, r["id"])
        self.assertEqual(self.tick(30, temp=37), 0)                      # done but still hot: wait
        self.assertEqual(self.tick(AFTER_DONE_SECONDS - 60, temp=37), 0)
        self.assertEqual(self.tick(90, temp=37), 1)                       # 30 min later: a new report
        self.assertEqual(len(self.sensor_reports()), 2)

    def test_gas_resistance_drops_and_smoke_texts_everyone(self):
        self.rule(propertyId="gas", op="below", limit=40000, level="urgent", minutes=0)
        self.rule(propertyId="smoke", op="on", limit=None, level="emergency", minutes=0)
        self.assertEqual(self.tick(gas=60000), 0)
        self.assertEqual(self.tick(gas=31000, smoke=True), 2)
        by = {r["sensor"]["propertyId"]: r for r in self.sensor_reports()}
        self.assertEqual(by["gas"]["title"], "Bme680 gas too low: 31,000")
        self.assertEqual((by["gas"]["category"], by["gas"]["issues"]), ("cleanliness", ["bad_odor"]))
        self.assertEqual((by["smoke"]["title"], by["smoke"]["category"], by["smoke"]["priority"]),
                         ("Smoke detected", "emergency", "critical"))
        texts = [t for _, t in self.sms.sent]
        self.assertTrue(any(t.startswith("AureoClean EMERGENCY") for t in texts))
        # Emergency → everyone with SMS on (the janitor is the only one with a number here).
        self.assertEqual(len(texts), 2)

    def test_report_only_level_sends_nothing_and_old_readings_are_ignored(self):
        self.rule(level="report", minutes=0)
        self.tick(temp=40)
        [r] = self.sensor_reports()
        self.assertEqual(r["priority"], "normal")
        self.assertEqual(self.sms.sent, [])
        # Escalation only follows texted reports.
        self.assertIsNone(r.get("notifiedAt"))

    def test_stale_value_does_not_alert(self):
        self.rule(minutes=0)
        self.cloud.values["temp"] = 40
        self.svc.arduino._cache = None
        self.svc.arduino.readings()               # cached with the current time…
        self.clock.t += 4 * 3600                   # …then the device goes quiet for 4 hours
        self.assertEqual(self.svc.sensor_alerts.check(), 0)

    def test_switched_off_and_deleted_rules_do_nothing(self):
        rule = self.rule(minutes=0, on=False)
        self.assertEqual(self.tick(temp=45), 0)
        self.svc.save_sensor_alert(self.admin, {**rule, "propertyId": "temp", "on": True})
        self.svc.delete_sensor_alert(self.admin, rule["id"])
        self.assertEqual(self.tick(temp=45), 0)
        self.assertEqual(self.svc.sensor_alerts_overview()["rules"], [])

    def test_bad_input(self):
        with self.assertRaises(BadRequest):
            self.rule(limit=None)
        with self.assertRaises(BadRequest):
            self.rule(level="loud")
        with self.assertRaises(BadRequest):
            self.rule(location="The Moon")
        with self.assertRaises(BadRequest):
            self.rule(minutes=500)

    def test_suggestions(self):
        s = self.svc.sensor_alert_suggestion("t1", "gas")
        self.assertEqual((s["kind"], s["op"]), ("gas", "below"))
        self.assertEqual(s["limit"], 55000)   # half of the usual ~110 kΩ
        self.assertEqual(self.svc.sensor_alert_suggestion("t1", "temp")["limit"], 35)
        smoke = self.svc.sensor_alert_suggestion("t1", "smoke")
        self.assertEqual((smoke["op"], smoke["level"]), ("on", "emergency"))
        self.assertEqual(suggest({"name": "iaq", "value": 80})["limit"], 200)
        self.assertEqual(kind_of("ammoniaPpm"), "ammonia")
        self.assertEqual(show(52000.0, "Ω"), "52,000 Ω")
        self.assertEqual(show(3.14159), "3.14")

    def test_says_why_it_has_not_alerted(self):
        rule = self.rule(minutes=5)
        why = lambda: next(r for r in self.svc.sensor_alerts_overview()["rules"] if r["id"] == rule["id"])["why"]  # noqa: E731
        self.assertEqual(why(), "Not checked yet.")
        self.tick(temp=30)
        self.assertEqual(why(), "Normal: now 30 °C.")
        self.tick(temp=36)
        self.tick(60, temp=36)
        self.assertEqual(why(), "Over the limit (now 36 °C) for 1 min — a report after 5 min.")
        self.tick(300, temp=36)
        self.assertRegex(why(), r"^Alert sent: report #\d+ \(now 36 °C\)\.$")
        # A device that stopped sending: old readings are ignored, and the list says so.
        self.svc.arduino._cache = None
        self.svc.arduino.readings()
        self.clock.t += 4 * 3600
        self.svc.sensor_alerts.check()
        self.assertIn("the last reading is 4 h old", why())
        self.assertEqual(self.svc.sensor_alerts_overview()["checker"]["error"], None)

    def test_test_button_makes_a_report_and_texts(self):
        rule = self.rule(minutes=10)
        r = self.svc.test_sensor_alert(self.admin, rule["id"])
        self.assertIn("Test report #", r["message"])
        self.assertIn("Texting 1 people", r["message"])
        rep = self.sensor_reports()[0]
        self.assertTrue(rep["title"].startswith("TEST Temperature too high"))
        self.assertTrue(rep["sensor"]["test"])
        self.assertIn("nothing is wrong", rep["details"])
        self.assertTrue(self.sms.sent[0][1].startswith(f"AureoClean TEST SENSOR ALERT #{rep['smsRef']}: Temperature too high"), self.sms.sent[0][1])
        with self.assertRaises(BadRequest):   # one test a minute
            self.svc.test_sensor_alert(self.admin, rule["id"])
        # The test doesn't change the alert itself.
        self.assertEqual(self.tick(temp=30), 0)
        self.svc.sms = type("Log", (), {"mode": "log", "send": lambda s, to, text: None})()
        self.clock.t += 61
        self.assertIn("TEST MODE", self.svc.test_sensor_alert(self.admin, rule["id"])["message"])
