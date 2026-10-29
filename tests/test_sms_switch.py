"""Finding the modem, switching SMS mode from the app, and remembering it."""
import dataclasses
import tempfile
import unittest
from pathlib import Path

from backend.config import load_settings
from backend.factory import build_service
from backend.providers.local import LocalBackend
from backend.service import AureoService, BadRequest, ServiceUnavailable
from backend.sms import LogSender, ModemSender, SmsError, detect_modem
from tests.test_service import PW
from tests.test_sms import FakeSerial


class FakePorts:
    """Only /dev/ttyS0 at 115200 has a modem; /dev/serial0 is a symlink to it."""

    def __init__(self):
        self.opened = []

    def exists(self, port):
        return port in ("/dev/serial0", "/dev/ttyS0", "/dev/ttyUSB0")

    def opener(self, port, baud):
        self.opened.append((port, baud))
        if port in ("/dev/serial0", "/dev/ttyS0") and baud == 115200:
            fake = FakeSerial()
            orig = fake.write

            def write(data):
                if data == b"ATI\r":
                    fake.out += b"\r\nSIM900 R11.0\r\n\r\nOK\r\n"
                else:
                    orig(data)
            fake.write = write
            return fake
        return _Silent()


class _Silent:
    in_waiting = 0

    def read(self, n=1):
        return b""

    def write(self, data):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


class Detection(unittest.TestCase):
    def test_finds_port_and_speed(self):
        ports = FakePorts()
        found = detect_modem(ports=("/dev/serial0", "/dev/ttyS0", "/dev/ttyUSB0"), bauds=(9600, 115200),
                             opener=ports.opener, exists=ports.exists)
        self.assertEqual((found["port"], found["baud"]), ("/dev/serial0", 115200))
        self.assertIn("SIM900", found["model"])

    def test_nothing_answers(self):
        ports = FakePorts()
        with self.assertRaises(SmsError) as ctx:
            detect_modem(ports=("/dev/ttyUSB0",), bauds=(9600,), opener=ports.opener, exists=ports.exists, console=lambda: None)
        self.assertIn("nothing came back at all", str(ctx.exception))

    def test_garbled_bytes_point_to_ground_or_5v(self):
        class Noisy(_Silent):
            in_waiting = 3

            def read(self, n=1):
                return b"\xf8\x80\xfe"
        with self.assertRaises(SmsError) as ctx:
            detect_modem(ports=("/dev/ttyS0",), bauds=(9600,), opener=lambda p, b: Noisy(), exists=lambda p: True,
                         console=lambda: None)
        self.assertIn("unreadable at every speed", str(ctx.exception))

    def test_serial_console_still_on(self):
        with self.assertRaises(SmsError) as ctx:
            detect_modem(ports=("/dev/ttyS0",), bauds=(9600,), opener=lambda p, b: _Silent(), exists=lambda p: True,
                         console=lambda: "/boot/firmware/cmdline.txt")
        self.assertIn("serial login console is still switched on", str(ctx.exception))

    def test_no_serial_port(self):
        with self.assertRaises(SmsError) as ctx:
            detect_modem(ports=("/dev/nothing",), exists=lambda p: False)
        self.assertIn("raspi-config", str(ctx.exception))


class Switching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = dataclasses.replace(
            load_settings(), mode="local", data_dir=Path(self.tmp.name), log_dir=Path(self.tmp.name),
            sms_settings_file=Path(self.tmp.name) / "sms.json", sms_mode="log")
        self.backend = LocalBackend(":memory:")
        self.addCleanup(self.backend.close)
        self.svc = AureoService(self.settings, self.backend, self.backend)
        self.svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", PW)
        self.admin = self.svc.require_session(self.svc.login("headadmin", PW)["token"])

    def test_detect_switches_live_and_is_remembered(self):
        self.assertEqual(self.svc.sms_settings()["mode"], "log")
        result = self.svc.use_detected_modem(
            self.admin, detector=lambda: {"port": "/dev/serial0", "baud": 9600, "model": "SIM900"})
        self.assertEqual((result["mode"], result["port"], result["baud"]), ("modem", "/dev/serial0", 9600))
        self.assertIsInstance(self.svc.sms.inner, ModemSender)

        restarted = build_service(self.settings)  # e.g. after a reboot
        self.addCleanup(restarted.store.close)
        self.assertEqual(restarted.sms_settings()["mode"], "modem")
        self.assertEqual(restarted.sms.baud, 9600)

        self.svc.use_test_mode(self.admin)
        self.assertIsInstance(self.svc.sms.inner, LogSender)
        again = build_service(self.settings)
        self.addCleanup(again.store.close)
        self.assertEqual(again.sms_settings()["mode"], "log")

    def test_detect_failure_keeps_current_mode(self):
        def broken():
            raise SmsError("The modem didn't answer")
        with self.assertRaises(ServiceUnavailable):
            self.svc.use_detected_modem(self.admin, detector=broken)
        self.assertEqual(self.svc.sms_settings()["mode"], "log")

    def test_send_test_sms_to_any_number(self):
        sent = []

        class Recorder:
            mode = "modem"
            port, baud = "/dev/serial0", 9600

            def send(self, to, text):
                sent.append((to, text))

        self.svc.sms = Recorder()
        self.assertIn("sent to +639171234567", self.svc.send_test_sms_to(self.admin, "0917 123 4567")["message"])
        self.assertEqual(sent[0][0], "+639171234567")
        with self.assertRaises(BadRequest):
            self.svc.send_test_sms_to(self.admin, "12345")


if __name__ == "__main__":
    unittest.main()
