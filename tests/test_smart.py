"""Smart monitoring: trends from the two ESP32 units become jobs and texts (fixed rules)."""
import json
from datetime import datetime, timedelta, timezone

from backend.security import iso
from tests.test_arduino import Resp
from tests.test_service import Base
from tests.test_sms import FakeSender

MANILA = timezone(timedelta(hours=8))


def at(hour, minute=0):
    return datetime(2026, 10, 28, hour, minute, tzinfo=MANILA).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Boards:
    """Arduino Cloud with our two Things (Urinal01, Toilet01) and the variables from the ESP32 sketches."""

    def __init__(self, clock):
        self.clock = clock
        self.vals = {
            "u": {"temperature": 30.0, "humidity": 70.0, "iaq": 60.0, "iaqAccuracy": 3, "gasResistance": 100.0,
                  "sensorStatus": 0, "totalToFUsers": 0, "totalFlushes": 0, "flushThreshold": 1, "cubicleOccupied": False},
            "t": {"temperature": 30.0, "iaq": 60.0, "iaqAccuracy": 3, "gasResistance": 100.0, "sensorStatus": 0,
                  "totalUses": 0, "totalFlushes": 0},
        }
        self.frozen = {}   # thing → time its values stopped updating (offline)

    def post(self, url, data=None, headers=None, timeout=None):
        return Resp(200, {"access_token": "tok", "expires_in": 300})

    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith("/iot/v2/things"):
            return Resp(200, [{"id": "u", "name": "Urinal01"}, {"id": "t", "name": "Toilet01"}])
        for tid in ("u", "t"):
            if url.endswith(f"/iot/v2/things/{tid}/properties"):
                when = self.frozen.get(tid, self.clock())
                stamp = datetime.fromtimestamp(when, timezone.utc).isoformat().replace("+00:00", "Z")
                return Resp(200, [{"id": f"{tid}-{k}", "variable_name": k, "type": "FLOAT", "last_value": v,
                                   "value_updated_at": stamp} for k, v in self.vals[tid].items()])
        if url.endswith("/timeseries"):
            return Resp(200, {"data": []})
        return Resp(403, {})


class SmartBase(Base):
    START = at(10)

    def setUp(self):
        super().setUp()
        self.clock = Clock(self.START)
        self.boards = Boards(self.clock)
        self.svc.arduino.http = self.boards
        self.svc.smart.clock = self.clock
        self.svc.sync_notifications = True
        self.sms = FakeSender()
        self.svc.sms = self.sms
        self.svc.set_arduino(self.admin, "client-id-123", "s3cret-secret")
        self.svc.smart.save_config({"quietStart": 19, "quietEnd": 5,
                                    "units": {"u": {"location": "2nd Floor · Male"}, "t": {"location": "2nd Floor · Male"}}})
        self.janitor = self.add_janitor("ana", "+639171234567")

    def add_janitor(self, username, phone, sign_in=True):
        req = self.svc.submit_janitor_request(username.title() + " Cruz", username, f"{username}@example.com", phone)
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor(username, code, "Janitor123")
        uid = next(u["uid"] for u in self.svc.list_users() if u["username"] == username)
        self.svc._update_profile(uid, lambda p: p.update(smsPhone=phone, smsAlerts=True))
        if sign_in:
            return self.svc.require_session(self.svc.login(username, "Janitor123")["token"])
        return uid

    def run_for(self, seconds, step=5, **values):
        """Advance the clock in steps, setting values (per thing: u_x / t_x), checking each step. Returns reports made."""
        for k, v in values.items():
            tid, name = k.split("_", 1)
            self.boards.vals[tid][name] = v
        made = 0
        for _ in range(max(1, int(seconds // step))):
            self.clock.t += step
            self.svc.arduino._cache = None
            made += self.svc.smart.check()
        return made

    def smart_reports(self):
        return sorted((r for r in self.svc.list_reports() if r.get("source") == "smart"), key=lambda r: r["smsRef"])

    def texts(self):
        return [t for _, t in self.sms.sent]


class FireAndHeat(SmartBase):
    def test_very_hot_for_30_seconds_is_an_emergency_for_everyone_active_today(self):
        self.run_for(60)                                   # normal
        self.assertEqual(self.run_for(20, u_temperature=55), 0)   # 20 s: not yet
        self.assertEqual(self.run_for(15), 1)              # 35 s → emergency
        [r] = self.smart_reports()
        self.assertEqual((r["category"], r["priority"], r["smart"]["kind"], r["smart"]["tier"]), ("emergency", "critical", "fire", 3))
        self.assertTrue(r["title"].startswith("Very high temperature: 55"), r["title"])
        self.assertEqual(r["location"], "2nd Floor · Male")
        self.assertTrue(self.texts()[0].startswith(f"AureoClean EMERGENCY #{r['smsRef']}: Very high temperature"), self.texts())
        self.assertIn("(Urinal01)", self.texts()[0])
        # Still hot: no second report or text.
        self.run_for(60)
        self.assertEqual(len(self.smart_reports()), 1)
        self.assertEqual(len(self.sms.sent), 1)

    def test_one_glitchy_reading_is_ignored(self):
        self.run_for(30)
        self.run_for(5, u_temperature=85)
        self.run_for(60, u_temperature=30)
        self.assertEqual(self.smart_reports(), [])

    def test_temperature_rising_fast(self):
        self.run_for(60)
        temp = 30.0
        made = 0
        for _ in range(12):            # +1 °C every 5 s → +12 °C in a minute
            temp += 1
            made += self.run_for(5, u_temperature=temp)
        self.assertEqual(made, 1)
        [r] = self.smart_reports()
        self.assertTrue(r["title"].startswith("Temperature rising fast"), r["title"])
        self.assertEqual(r["priority"], "critical")

    def test_emergencies_ignore_quiet_hours(self):
        self.clock.t = at(22)
        self.run_for(60, u_temperature=56)
        self.assertEqual(len(self.smart_reports()), 1)
        self.assertEqual(len(self.sms.sent), 1)

    def test_hot_room_job_then_texts_janitors(self):
        self.assertEqual(self.run_for(9 * 60, step=30, t_temperature=36.5), 0)
        self.assertEqual(self.run_for(2 * 60, step=30), 1)           # 10 min → a job, no text
        [r] = self.smart_reports()
        self.assertEqual((r["priority"], r["category"]), ("normal", "facility"))
        self.assertEqual(self.sms.sent, [])
        self.run_for(10 * 60, step=30)                               # 20 min → janitors texted
        [r] = self.smart_reports()
        self.assertEqual(r["priority"], "high")
        self.assertEqual(len(self.sms.sent), 1)
        self.assertTrue(self.texts()[0].startswith(f"AureoClean ALERT #{r['smsRef']}: Very hot"))
        self.assertTrue(any("Texting the janitors" in n["text"] for n in r["notes"]))


class Air(SmartBase):
    def test_bad_iaq_after_heavy_use(self):
        self.run_for(60)
        uses = 0
        for _ in range(26):                                          # 26 uses in 13 min, air goes bad
            uses += 1
            self.run_for(30, step=30, u_totalToFUsers=uses, u_totalFlushes=uses, u_iaq=190)
        [r] = self.smart_reports()
        self.assertTrue(r["title"].startswith("Bad air: IAQ 190 (bad)"), r["title"])
        self.assertIn("It follows heavy use", r["details"])
        self.assertEqual(r["issues"], ["bad_odor"])
        self.assertEqual(self.sms.sent, [])
        self.run_for(11 * 60, step=30)                               # 20 min+ → text the janitors
        self.assertEqual(self.smart_reports()[0]["priority"], "high")
        self.assertEqual(len(self.sms.sent), 1)

    def test_bad_air_while_barely_used_points_elsewhere(self):
        self.run_for(13 * 60, step=30, t_iaq=210)
        [r] = self.smart_reports()
        self.assertIn("barely being used", r["details"])
        self.assertIn("floor drain", r["details"])

    def test_raw_gas_used_while_iaq_calibrates(self):
        self.boards.vals["u"]["iaqAccuracy"] = 0
        self.run_for(40 * 60, step=30)                               # learn: gas ~100 kΩ is normal at 10 AM
        self.run_for(13 * 60, step=30, u_gasResistance=45.0)
        [r] = self.smart_reports()
        self.assertRegex(r["title"], r"^Bad air: gas reading 5\d% below normal$")
        self.assertIn("IAQ is still calibrating", r["details"])

    def test_back_to_normal_note(self):
        self.run_for(13 * 60, step=30, t_iaq=200)
        self.run_for(4 * 60, step=30, t_iaq=60)
        [r] = self.smart_reports()
        self.assertTrue(any(n["text"].startswith("Back to normal on Toilet01") for n in r["notes"]))

    def test_sensors_join_a_report_people_already_made(self):
        self.svc.report_problem(self.janitor, "2nd Floor · Male", ["bad_odor"])
        self.run_for(13 * 60, step=30, u_iaq=200)
        self.assertEqual(self.smart_reports(), [])
        [r] = self.svc.list_reports()
        self.assertTrue(any(n["text"].startswith("Sensors agree (Urinal01): Bad air") for n in r["notes"]))

    def test_quiet_hours_hold_janitor_texts_until_morning(self):
        self.clock.t = at(19, 0)
        self.run_for(25 * 60, step=30, u_iaq=260)                    # tier 2 at night → held
        [r] = self.smart_reports()
        self.assertEqual(r["priority"], "high")
        self.assertEqual(self.sms.sent, [])
        self.assertTrue(r.get("smsDeferred"))
        self.assertTrue(any("Quiet hours (7 PM–5 AM)" in n["text"] for n in r["notes"]))
        self.clock.t = at(5, 0) + 86400
        self.svc.smart._deferred_at = 0
        self.run_for(5)
        self.assertEqual(len(self.sms.sent), 1)
        self.assertFalse(self.smart_reports()[0].get("smsDeferred"))


class UsageAndFlush(SmartBase):
    def test_cleaning_due_after_uses_and_done_resets_it(self):
        self.svc.smart.save_config({"cleanEvery": 50})
        self.run_for(10)
        self.run_for(10, u_totalToFUsers=52, u_totalFlushes=52)
        [r] = self.smart_reports()
        self.assertEqual(r["title"], "Cleaning due: 52 uses since the last cleaning")
        self.svc.janitor_take(self.janitor, r["id"])
        self.svc.janitor_done(self.janitor, r["id"])
        self.run_for(60)
        self.assertEqual(len(self.smart_reports()), 1)
        unit = next(u for u in self.svc.smart_overview()["units"] if u["id"] == "u")
        self.assertEqual(unit["usesSinceCleaned"], 0)
        self.run_for(10, u_totalToFUsers=60, u_totalFlushes=60)
        self.svc.smart_cleaned(self.janitor, "u")
        self.assertEqual(next(u for u in self.svc.smart.units_seen.values() if u["id"] == "u")["usesSinceCleaned"], 8)
        self.run_for(5)
        self.assertEqual(next(u for u in self.svc.smart_overview()["units"] if u["id"] == "u")["usesSinceCleaned"], 0)

    def test_board_counter_reset_keeps_counting(self):
        self.svc.smart.save_config({"cleanEvery": 50})
        self.run_for(10)
        self.run_for(10, u_totalToFUsers=40, u_totalFlushes=40)
        self.run_for(10, u_totalToFUsers=12, u_totalFlushes=12)      # board re-flashed: counter from 0
        self.assertEqual(len(self.smart_reports()), 1)               # 40 + 12 = 52 since cleaning

    def test_toilet_uses_without_flushes(self):
        self.run_for(10)
        self.run_for(10, t_totalUses=3, t_totalFlushes=3)
        self.assertEqual(self.smart_reports(), [])
        self.run_for(10, t_totalUses=8)
        [r] = self.smart_reports()
        self.assertEqual(r["title"], "Auto-flush may be broken: 5 uses, no flush")
        self.assertEqual(r["issues"], ["broken_flush"])

    def test_urinal_flush_threshold_is_respected(self):
        self.boards.vals["u"]["flushThreshold"] = 3        # flushes every 3 users
        self.run_for(10)
        self.run_for(10, u_totalToFUsers=6)                 # 6 uses, no flush: within 3 + 5 - 1
        self.assertEqual(self.smart_reports(), [])
        self.run_for(10, u_totalToFUsers=7)
        self.assertEqual(len(self.smart_reports()), 1)


class Faults(SmartBase):
    def test_offline_unit_then_back(self):
        self.run_for(30)
        self.boards.frozen["t"] = self.clock.t
        self.run_for(11 * 60, step=60)
        reps = self.smart_reports()
        self.assertEqual([r["title"] for r in reps], ["Toilet01 is offline"])
        self.assertEqual(reps[0]["category"], "system")
        self.boards.frozen.pop("t")
        self.run_for(3 * 60, step=30)
        self.assertTrue(any("Back to normal on Toilet01" in n["text"] for n in self.smart_reports()[0]["notes"]))

    def test_bme680_error_and_no_air_alerts_while_broken(self):
        self.run_for(6 * 60, step=30, u_sensorStatus=-2, u_iaq=400)
        [r] = self.smart_reports()
        self.assertEqual(r["title"], "Air sensor not working")


class Recipients(SmartBase):
    def test_everyone_active_today_gets_emergencies(self):
        contact_uid = self.add_janitor("ben", "+639181234567", sign_in=False)   # not signed in today
        old = iso(datetime.now(timezone.utc) - timedelta(days=2))
        self.svc.store.transact(self.svc.C_USERS, contact_uid, lambda u: {**u, "lastLoginAt": old, "lastSeenAt": old})
        names = {r["username"] for r in self.svc.emergency_recipients()}
        self.assertEqual(names, {"ana"})
        # SMS-only contacts never sign in, so they always count.
        self.svc.store.transact(self.svc.C_USERS, contact_uid, lambda u: {**u, "smsOnly": True})
        self.assertEqual({r["username"] for r in self.svc.emergency_recipients()}, {"ana", "ben"})

    def test_nobody_active_today_falls_back_to_everyone(self):
        old = iso(datetime.now(timezone.utc) - timedelta(days=3))
        for u in self.svc.list_users():
            self.svc.store.transact(self.svc.C_USERS, u["uid"], lambda x: {**x, "lastLoginAt": old, "lastSeenAt": old})
        self.svc._seen_at.clear()
        self.assertEqual({r["username"] for r in self.svc.emergency_recipients()}, {"ana"})

    def test_sms_reply_counts_as_active(self):
        uid = self.add_janitor("cara", "+639191234567", sign_in=False)
        old = iso(datetime.now(timezone.utc) - timedelta(days=2))
        self.svc.store.transact(self.svc.C_USERS, uid, lambda u: {**u, "lastLoginAt": old, "lastSeenAt": old})
        self.assertNotIn("cara", {r["username"] for r in self.svc.emergency_recipients()})
        self.svc.two_way.handle("+639191234567", "HELP")
        self.assertIn("cara", {r["username"] for r in self.svc.emergency_recipients()})


class FakeLlm:
    configured = True

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def ask(self, system, user, max_tokens=0, fast=False):
        self.prompts.append(user)
        return json.dumps(self.answer)

    def settings(self):
        return {"provider": "gemini", "model": "x", "url": "", "daily": False, "dailyHour": 6, "autoTips": False}


class DailyAdvice(SmartBase):
    def busy_history(self):
        st = self.svc.smart.state["usage"].setdefault("u", {})
        for day in range(1, 6):
            d = datetime.fromtimestamp(self.START - day * 86400, MANILA).strftime("%Y-%m-%d")
            st[f"{d}T11"] = 40
            st[f"{d}T14"] = 10

    def test_rules_plan_cleaning_before_the_busy_hour(self):
        self.run_for(10)
        self.busy_history()
        self.svc.llm = type("NoAi", (), {"configured": False})()
        doc = self.svc.advice.run({"type": "system", "username": "daily"}, "daily")
        self.assertEqual(doc["source"], "rules")
        jobs = [j for j in doc["janitor"] if "rush" in j["task"]]
        self.assertTrue(jobs, doc["janitor"])
        self.assertEqual(jobs[0]["time"], "10:45 AM")
        self.assertIn("Busiest at 11 AM (about 40 uses)", jobs[0]["why"])
        jan = self.svc.advice_today(self.janitor)
        self.assertNotIn("admin", jan)
        self.assertIn("admin", self.svc.advice_today(self.admin))

    def test_ai_advice_is_cleaned_and_suggestions_are_whitelisted(self):
        self.run_for(10)
        self.busy_history()
        self.svc.llm = FakeLlm({
            "headline": "Busy late morning.",
            "janitor": [{"time": "10:40 AM", "where": "Urinal01", "task": "Clean before the 11 AM rush", "why": "40 uses at 11 AM", "priority": "high"}],
            "admin": [{"title": "Peak at 11 AM", "detail": "40 uses", "priority": "medium"}],
            "suggestions": [{"setting": "cleanEvery", "value": 80, "why": "busy"},
                            {"setting": "fireTemp", "value": 90, "why": "nope"},          # not tunable from advice
                            {"setting": "cleanEvery", "value": 99999, "why": "out of range"}],
        })
        doc = self.svc.advice.run({"type": "system", "username": "daily"}, "daily")
        self.assertEqual(doc["source"], "ai")
        self.assertIn('"busiestHours"', self.svc.llm.prompts[0])
        self.assertEqual([s["setting"] for s in doc["suggestions"]], ["cleanEvery"])
        r = self.svc.apply_advice(self.admin, "cleanEvery", 80)
        self.assertIn("Cleaning due after (uses) → 80", r["message"])
        self.assertEqual(self.svc.smart.config()["cleanEvery"], 80)
        self.assertEqual(self.svc.advice.saved()["suggestions"], [])


class AlertsPage(SmartBase):
    def test_page_lists_automatic_rules_and_whats_happening(self):
        self.run_for(60, u_temperature=56)
        page = self.svc.alerts_page()
        self.assertEqual([r["kind"] for r in page["automatic"]], ["fire", "hot", "air", "clean", "flush", "sensor", "offline"])
        self.assertIn("50 °C or more for 30 s", page["automatic"][0]["when"])
        [now] = [x for x in page["now"] if x["tier"] == 3]
        self.assertEqual((now["unit"], now["source"]), ("Urinal01", "automatic"))
        self.assertEqual(self.svc.admin_overview()["activeAlerts"], 1)

    def test_an_automatic_type_can_be_switched_off_but_not_fire(self):
        self.svc.save_smart_settings(self.admin, {"kindsOff": ["air", "fire"]})
        self.assertEqual(self.svc.smart.config()["kindsOff"], ["air"])
        self.run_for(13 * 60, step=30, t_iaq=230)
        self.assertEqual(self.smart_reports(), [])
        self.assertFalse(next(r for r in self.svc.alerts_page()["automatic"] if r["kind"] == "air")["on"])

    def test_suggested_alerts_trend_and_one_tap_add(self):
        self.boards.vals["t"]["cubicleOccupied"] = False
        self.svc.arduino._cache = None
        page = self.svc.refresh_alert_ideas(self.admin)
        by = {i["id"]: i for i in page["ideas"]}
        idea = by["t:t-cubicleOccupied:on"]
        self.assertEqual((idea["rule"]["level"], idea["rule"]["minutes"]), ("urgent", 20))
        self.assertIn("may need help", idea["why"])
        r = self.svc.add_alert_idea(self.admin, idea["id"])
        self.assertEqual(r["rule"]["location"], "2nd Floor · Male")
        self.assertNotIn(idea["id"], {i["id"] for i in self.svc.alerts_page()["ideas"]["ideas"]})   # added → gone
        self.svc.dismiss_alert_idea(self.admin, "u:u-cubicleOccupied:on")
        self.assertNotIn("u:u-cubicleOccupied:on", {i["id"] for i in self.svc.alert_ideas.view()["ideas"]})

    def test_ai_ideas_are_checked(self):
        self.svc.llm = FakeLlm({"ideas": [
            {"thingId": "u", "propertyId": "u-humidity", "op": "above", "limit": 88, "minutes": 20, "level": "report",
             "title": "Damp urinal area", "why": "p95 86%"},
            {"thingId": "u", "propertyId": "u-humidity", "op": "above", "limit": 88, "level": "emergency", "title": "x"},   # no emergencies
            {"thingId": "nope", "propertyId": "x", "op": "above", "limit": 1, "level": "report", "title": "made up"},   # not a real variable
        ]})
        page = self.svc.refresh_alert_ideas(self.admin)
        ai = [i for i in page["ideas"] if i["source"] == "ai"]
        self.assertEqual([(i["title"], i["rule"]["limit"]) for i in ai], [("Damp urinal area", 88)])
