"""Odor-activated deodorizer: extremely bad air → one spray, then a long wait; never during the e-stop
or while someone is in the room."""
import unittest

from tests.test_sensor_feed import RoomBoards
from tests.test_smart import SmartBase


class SprayBoards(RoomBoards):
    def __init__(self, clock):
        super().__init__(clock)
        self.vals["r"].update(roomOccupied=False, peopleInRoom=0)

    def get(self, url, headers=None, params=None, timeout=None):
        r = super().get(url, headers, params, timeout)
        if url.endswith("/iot/v2/things/d/properties"):
            r._data.append({"id": "d-sprayNow", "variable_name": "sprayNow", "type": "", "last_value": False,
                         "value_updated_at": r._data[0]["value_updated_at"], "permission": "READ_WRITE"})
        return r

    def sprays(self):
        return [u for u, j in self.published if u.endswith("/d-sprayNow/publish") and j == {"value": True}]


class OdorSpray(SmartBase):
    def setUp(self):
        super().setUp()
        self.boards = SprayBoards(self.clock)
        self.svc.arduino.http = self.boards
        self.svc.arduino._things = None

    def test_extremely_bad_air_for_two_minutes_sprays_once_then_waits(self):
        self.run_for(30)
        self.run_for(90, t_iaq=330)
        self.assertEqual(self.boards.sprays(), [])               # not 2 minutes yet
        self.run_for(40)
        self.assertEqual(len(self.boards.sprays()), 1)
        self.run_for(10 * 60)
        self.assertEqual(len(self.boards.sprays()), 1)           # at most one every 30 min
        self.run_for(21 * 60)
        self.assertEqual(len(self.boards.sprays()), 2)
        self.assertIn("auto_spray", [a["action"] for a in self.svc.list_audit()])
        self.assertEqual(self.svc.smart.overview()["spray"]["lastUnit"], "Toilet01")

    def test_bad_but_not_extreme_air_never_sprays(self):
        self.run_for(5 * 60, t_iaq=260)
        self.assertEqual(self.boards.sprays(), [])

    def test_never_during_e_stop_or_while_someone_is_inside(self):
        self.run_for(3 * 60, t_iaq=330, d_eStopPressed=True)
        self.assertEqual(self.boards.sprays(), [])
        self.run_for(30, d_eStopPressed=False, r_roomOccupied=True, r_peopleInRoom=1)
        self.assertEqual(self.boards.sprays(), [])
        self.assertEqual(self.svc.smart.state["spray"]["status"], "waiting: room occupied")
        self.run_for(10, r_roomOccupied=False, r_peopleInRoom=0)
        self.assertEqual(len(self.boards.sprays()), 1)

    def test_can_be_switched_off_and_the_limit_changed(self):
        self.svc.smart.save_config({"kindsOff": ["spray"]})
        self.run_for(3 * 60, t_iaq=330)
        self.assertEqual(self.boards.sprays(), [])
        self.svc.smart.save_config({"kindsOff": [], "odorSprayIaq": 400})
        self.run_for(3 * 60)
        self.assertEqual(self.boards.sprays(), [])
        rules = {r["kind"]: r for r in self.svc.smart.automatic_rules(self.svc.smart.config())}
        self.assertIn("IAQ 400", rules["spray"]["when"])


if __name__ == "__main__":
    unittest.main()
