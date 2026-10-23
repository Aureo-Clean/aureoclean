import unittest
from unittest import mock

import requests

from backend import network


class Resp:
    def __init__(self, code):
        self.status_code = code


class NetworkCheck(unittest.TestCase):
    def test_states(self):
        with mock.patch.object(network.requests, "get", return_value=Resp(204)):
            self.assertEqual(network.check_once(), network.ONLINE)
        with mock.patch.object(network.requests, "get", return_value=Resp(302)):  # Wi-Fi login page
            self.assertEqual(network.check_once(), network.PORTAL)
        with mock.patch.object(network.requests, "get", side_effect=requests.ConnectionError()):
            self.assertEqual(network.check_once(), network.OFFLINE)

    def test_watch_status(self):
        watch = network.NetworkWatch()
        self.assertEqual(watch.status()["internet"], "unknown")
        with mock.patch.object(network.requests, "get", return_value=Resp(200)):
            watch.check_now()
        self.assertIn("sign-in page", watch.status()["message"])


if __name__ == "__main__":
    unittest.main()
