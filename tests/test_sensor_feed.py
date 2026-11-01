"""Firebase Realtime Database feed: /sites/.../devices/<id>/{meta,status,live,samples} (written by the boards)."""
import unittest
from datetime import datetime, timezone

from backend.sensor_feed import SensorFeed
from tests.test_arduino import Resp
from tests.test_smart import Boards, SmartBase

NOW = 1793152800.0          # unix seconds
MS = int(NOW * 1000)


def tree():
    return {
        "urinal_01": {
            "meta": {"type": "urinal", "name": "Urinal 1", "thing": "Urinal01", "board": "esp32", "uid": "x"},
            "status": {"lastSeen": MS - 20_000, "uptimeS": 900, "rssiDbm": -61, "firmware": "1.4.0"},
            "live": {"air": {"temperatureC": 31.5, "humidityPct": 70, "gasKOhm": 88.2, "iaq": 120, "iaqAccuracy": 2},
                     "presence": {"inUse": True, "distanceCm": 42.0},
                     "usage": {"totalUsers": 57, "usersSinceFlush": 0, "totalFlushes": 55, "flushThreshold": 1},
                     "updatedAt": MS - 2_000},
            "samples": {"s3": {"air": {"iaq": 90}, "t": MS - 120_000},
                        "s1": {"air": {"iaq": 100}, "t": MS - 60_000},
                        "s7": {"air": {"iaq": 40}, "t": MS - 3600_000}},      # before a restart: ignored
        },
        "room_01": {
            "status": {"lastSeen": MS - 300_000},                             # 5 min: offline
            "live": {"people": {"inRoom": 2, "entriesToday": 31}, "cleaning": {"needsCleaning": True, "visitsSinceCleaning": 16,
                                                                               "cleaningThreshold": 15},
                     "updatedAt": MS - 300_000},
        },
    }


class Layout(unittest.TestCase):
    def setUp(self):
        self.f = SensorFeed(lambda: None, clock=lambda: NOW)
        self.f.apply("put", "/", tree())

    def test_fields_map_to_the_arduino_variable_names(self):
        u = self.f.lookup("some-uuid", "Urinal01")
        self.assertEqual(u["temperature"][0], 31.5)
        self.assertEqual(u["totaltofusers"][0], 57)
        self.assertEqual(u["cubicleoccupied"][0], True)
        self.assertEqual(u["temperature"][1], NOW - 2)
        self.assertNotIn("pressure", u)                       # left out by the board → not invented
        r = self.f.lookup("", "room01")                        # no meta: device id → Room01
        self.assertEqual((r["needscleaning"][0], r["visitssincecleaning"][0], r["peopleinroom"][0]), (True, 16, 2))

    def test_online_is_last_seen_under_90_seconds(self):
        st = {d["id"]: d["online"] for d in self.f.status()["devices"]}
        self.assertEqual(st, {"urinal_01": True, "room_01": False})

    def test_samples_sorted_by_time_and_old_ones_ignored(self):
        rows = self.f.samples("", "Urinal01")
        self.assertEqual([r["iaq"] for r in rows], [90, 100])

    def test_live_rewrite_and_overlay(self):
        self.f.apply("put", "/urinal_01/live", {"air": {"temperatureC": 33.0}, "updatedAt": MS})
        self.assertNotIn("iaq", self.f.lookup("", "Urinal01"))          # whole live object replaced
        data = {"things": [{"id": "uuid-u", "name": "Urinal01", "properties": [
            {"name": "temperature", "value": 30.0, "updatedAt": "2026-10-01T00:00:00+00:00"},
            {"name": "iaq", "value": 77, "updatedAt": "2026-10-01T00:00:00+00:00"}]}]}
        self.f.overlay(data)
        props = {p["name"]: p for p in data["things"][0]["properties"]}
        self.assertEqual((props["temperature"]["value"], props["temperature"]["via"]), (33.0, "firebase"))
        self.assertEqual(props["iaq"]["value"], 77)                     # not in Firebase: Arduino's kept
        self.assertEqual(data["things"][0]["lastSeen"], NOW - 20)

    def test_screen_from_firebase_alone_keeps_the_arduino_ids(self):
        out = self.f.as_readings([{"id": "uuid-u", "name": "Urinal01"}])
        things = {t["name"]: t for t in out["things"]}
        self.assertEqual(things["Urinal01"]["id"], "uuid-u")
        self.assertEqual(things["Room01"]["id"], "room_01")
        temp = next(p for p in things["Urinal01"]["properties"] if p["name"] == "temperature")
        self.assertEqual((temp["value"], temp["unit"]), (31.5, "°C"))
        self.assertTrue(out["pages"])


class RoomBoards(Boards):
    """Adds Room01 (door counter) and Deodorizer01, with writable buttons, and records publishes."""

    def __init__(self, clock):
        super().__init__(clock)
        self.vals["r"] = {"peopleInRoom": 0, "visitsSinceCleaning": 3, "cleaningThreshold": 15, "needsCleaning": False}
        self.vals["d"] = {"spraysToday": 4, "sprayActive": False, "eStopPressed": False}
        self.published = []
        self.down = False

    def get(self, url, headers=None, params=None, timeout=None):
        if self.down:
            raise ConnectionError("Arduino Cloud unreachable")
        if url.endswith("/iot/v2/things"):
            return Resp(200, [{"id": "u", "name": "Urinal01"}, {"id": "t", "name": "Toilet01"},
                              {"id": "r", "name": "Room01"}, {"id": "d", "name": "Deodorizer01"}])
        for tid in ("r", "d"):
            if url.endswith(f"/iot/v2/things/{tid}/properties"):
                stamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")
                props = [{"id": f"{tid}-{k}", "variable_name": k, "type": "", "last_value": v, "value_updated_at": stamp,
                          "permission": "READ_ONLY"} for k, v in self.vals[tid].items()]
                if tid == "r":
                    props.append({"id": "r-markCleaned", "variable_name": "markCleaned", "type": "", "last_value": False,
                                  "value_updated_at": stamp, "permission": "READ_WRITE"})
                return Resp(200, props)
        return super().get(url, headers, params, timeout)

    def put(self, url, headers=None, json=None, timeout=None):
        self.published.append((url, json))
        if url.endswith("/r-markCleaned/publish") and json == {"value": True}:
            self.vals["r"].update(needsCleaning=False, visitsSinceCleaning=0)
        return Resp(200, {})


class RoomAndDeodorizer(SmartBase):
    def setUp(self):
        super().setUp()
        self.boards = RoomBoards(self.clock)
        self.svc.arduino.http = self.boards
        self.svc.arduino._things = None

    def test_room_board_needs_cleaning_makes_a_job_and_done_presses_mark_cleaned(self):
        self.run_for(30)
        self.assertEqual(self.smart_reports(), [])
        self.assertEqual(self.run_for(10, r_needsCleaning=True, r_visitsSinceCleaning=15), 1)
        [rep] = self.smart_reports()
        self.assertEqual((rep["smart"]["kind"], rep["smart"]["tier"], rep["category"]), ("clean", 1, "cleanliness"))
        self.assertIn("15 visits", rep["details"])
        self.svc.smart.report_done(rep)
        import time
        for _ in range(50):
            if self.boards.published:
                break
            time.sleep(0.02)
        self.assertTrue(self.boards.published[0][0].endswith("/things/r/properties/r-markCleaned/publish"))
        self.assertFalse(self.boards.vals["r"]["needsCleaning"])

    def test_cleaned_button_on_the_room_unit_presses_mark_cleaned(self):
        self.run_for(10)
        self.assertTrue(self.svc.smart.cleaned("r")["markCleaned"])
        self.assertEqual(self.boards.published[-1][1], {"value": True})

    def test_deodorizer_emergency_stop_is_a_job_without_texts(self):
        self.run_for(10)
        self.assertEqual(self.run_for(10, d_eStopPressed=True), 1)
        [rep] = self.smart_reports()
        self.assertEqual((rep["smart"]["kind"], rep["smart"]["tier"]), ("estop", 1))
        self.assertEqual(self.texts(), [])

    def test_firebase_keeps_the_screen_going_when_arduino_cloud_is_down(self):
        self.run_for(10)
        feed = SensorFeed(lambda: None, clock=self.clock)
        ms = int(self.clock() * 1000)
        feed.apply("put", "/", {"urinal_01": {"status": {"lastSeen": ms}, "live": {"air": {"temperatureC": 29.5}, "updatedAt": ms}}})
        self.svc.arduino.feed = feed
        self.boards.down = True
        self.svc.arduino._cache = None
        data = self.svc.arduino.readings()
        self.assertEqual(data["source"], "firebase")
        [u] = data["things"]
        self.assertEqual((u["id"], u["properties"][0]["value"]), ("u", 29.5))      # same id as Arduino's Urinal01


if __name__ == "__main__":
    unittest.main()
