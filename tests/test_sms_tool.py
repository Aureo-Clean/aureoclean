"""The modem test tool, against a simulated SIM900A that only answers at 115200 baud."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sms_test  # noqa: E402


class FakeSim900:
    def __init__(self, baud, registered_after=1, accept=True, smsc="+639180000101"):
        self.baud, self.out, self.sent = baud, b"", []
        self.creg_calls, self.registered_after, self.accept, self.smsc = 0, registered_after, accept, smsc

    in_waiting = property(lambda self: len(self.out))

    def read(self, n=1):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def reset_input_buffer(self):
        self.out = b""

    def close(self):
        pass

    def write(self, data):
        if self.baud != 115200:  # wrong speed: garbage, never OK
            self.out += b"\xff\xfe"
            return
        cmd = data.decode(errors="replace")
        replies = {
            "ATI": "SIM900 R11.0", "AT+CPIN?": "+CPIN: READY", "AT+CSQ": "+CSQ: 17,0",
            "AT+COPS?": '+COPS: 0,0,"SMART"', "AT+CSCA?": f'+CSCA: "{self.smsc}",145',
        }
        if cmd.startswith("AT+CREG?"):
            self.creg_calls += 1
            state = "1" if self.creg_calls > self.registered_after else "2"
            self.out += f"\r\n+CREG: 0,{state}\r\n\r\nOK\r\n".encode()
        elif cmd.startswith("AT+CMGS="):
            self.cmgs = cmd.strip()
            self.out += b"\r\n> "
        elif cmd.endswith("\x1a"):
            self.sent.append(cmd)
            self.out += b"\r\n+CMGS: 7\r\n\r\nOK\r\n" if self.accept else b"\r\n+CMS ERROR: 500\r\n"
        else:
            body = replies.get(cmd.strip())
            self.out += (f"\r\n{body}\r\n" if body else "").encode() + b"\r\nOK\r\n"


class ToolTests(unittest.TestCase):
    def run_tool(self, *args, **fake_kw):
        modems = {}

        def fake_open(port, baud):
            modems[baud] = FakeSim900(baud, **fake_kw)
            return modems[baud]

        out = io.StringIO()
        with mock.patch.object(sms_test, "open_serial", fake_open), \
             mock.patch.object(sms_test, "check_port", lambda p: True), \
             mock.patch.object(sms_test.time, "sleep", lambda s: None), \
             mock.patch.object(sys, "argv", ["sms_test.py", *args]), \
             mock.patch.dict(sys.modules, {"serial": mock.Mock()}), \
             mock.patch.dict(os.environ, {"AUREOCLEAN_SMS_PORT": ""}), \
             redirect_stdout(out):
            code = sms_test.main()
        return code, out.getvalue(), modems

    def test_finds_baud_registers_and_sends(self):
        code, out, modems = self.run_tool("--send", "0917 123 4567")
        self.assertEqual(code, 0, out)
        self.assertIn("answers at 115200 baud", out)
        self.assertIn("SMART", out)
        self.assertIn("Sent!", out)
        self.assertIn("AUREOCLEAN_SMS_BAUD=115200", out)
        self.assertEqual(len(modems[115200].sent), 1)
        self.assertEqual(modems[115200].cmgs, 'AT+CMGS="+639171234567"')
        self.assertIn("AureoClean test SMS", modems[115200].sent[0])

    def test_network_rejects_sms(self):
        code, out, _ = self.run_tool("--send", "09171234567", accept=False)
        self.assertEqual(code, 1)
        self.assertIn("no load/credit", out)

    def test_missing_smsc_is_flagged(self):
        code, out, _ = self.run_tool(smsc="")
        self.assertEqual(code, 1)
        self.assertIn("No SMS centre number", out)

    def test_bad_number(self):
        code, out, _ = self.run_tool("--send", "12345")
        self.assertEqual(code, 1)
        self.assertIn("valid mobile number", out)


if __name__ == "__main__":
    unittest.main()
