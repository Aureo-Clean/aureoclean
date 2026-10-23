#!/usr/bin/env python3
"""Step-by-step check of the SIM900A (or any AT-command GSM/LTE modem).

Run on the Raspberry Pi, with AureoClean closed (only one program can use
the serial port at a time):

    .venv/bin/python tools/sms_test.py                       # check everything
    .venv/bin/python tools/sms_test.py --send 09171234567    # ...and send a test text
    .venv/bin/python tools/sms_test.py --port /dev/ttyUSB0   # USB-serial adapter

Every step says in plain words what is wrong and what to check.
"""
from __future__ import annotations

import argparse
import grp
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.sms import gsm_text, normalize_phone  # noqa: E402

BAUDS = (9600, 115200, 57600, 38400, 19200, 4800)
OK, BAD, WARN = "\033[1;32m✓\033[0m", "\033[1;31m✗\033[0m", "\033[1;33m!\033[0m"
REG_STATES = {"0": "not registered (not searching)", "1": "registered on the home network",
              "2": "still searching for a network…", "3": "registration DENIED by the network",
              "4": "unknown", "5": "registered (roaming)"}
CMS_ERRORS = {"38": "network out of order", "41": "temporary network failure", "42": "network congestion",
              "304": "invalid text-mode parameter", "310": "no SIM card", "311": "SIM needs a PIN",
              "330": "SMS centre (SMSC) number unknown", "331": "no network service",
              "500": "unknown error (often: no load/credit, or SIM can't send SMS)"}


def step(title: str) -> None:
    print(f"\n\033[1;36m── {title}\033[0m")


class Modem:
    """Minimal AT-command conversation over a pyserial-like object."""

    def __init__(self, ser):
        self.ser = ser

    def read_until(self, tokens, timeout: float) -> str:
        buf, end = "", time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk.decode(errors="replace")
                if any(t in buf for t in tokens) or "ERROR" in buf:
                    time.sleep(0.05)
                    buf += self.ser.read(self.ser.in_waiting or 0).decode(errors="replace")
                    break
        return buf

    def cmd(self, command: str, timeout: float = 3.0, expect=("OK",)) -> str:
        self.ser.reset_input_buffer()
        self.ser.write((command + "\r").encode())
        return self.read_until(expect, timeout)

    def responds(self) -> bool:
        # SIM900A auto-detects the speed from the first "AT"s it receives.
        for _ in range(3):
            if "OK" in self.cmd("AT", timeout=1.0):
                return True
        return False


def open_serial(port: str, baud: int):
    import serial  # pyserial

    return serial.Serial(port, baud, timeout=0.3, write_timeout=3)


def check_port(port: str) -> bool:
    step(f"1. Serial port {port}")
    if not os.path.exists(port):
        print(f"{BAD} {port} doesn't exist.")
        print("   Turn on the serial port: sudo raspi-config → Interface Options → Serial Port →")
        print("   'login shell over serial?' = No, 'serial port hardware enabled?' = Yes → reboot.")
        print("   Using a USB-serial adapter instead? Try --port /dev/ttyUSB0")
        return False
    if not os.access(port, os.R_OK | os.W_OK):
        print(f"{BAD} No permission to use {port}.")
        try:
            user = os.environ.get("USER", "")
            listed = user in grp.getgrnam("dialout").gr_mem
        except KeyError:
            listed = False
        print("   You're in the 'dialout' group but haven't rebooted since — reboot the Pi." if listed else
              "   Run:  sudo usermod -aG dialout $USER   then reboot.")
        return False
    real = os.path.realpath(port)
    print(f"{OK} {port} is available (→ {real}).")
    return True


def find_baud(port: str, preferred: int | None):
    step("2. Is the modem answering?")
    for baud in ([preferred] if preferred else []) + [b for b in BAUDS if b != preferred]:
        try:
            ser = open_serial(port, baud)
        except Exception as exc:  # noqa: BLE001
            if "busy" in str(exc).lower() or "resource" in str(exc).lower():
                print(f"{BAD} The port is in use. Close AureoClean (and anything else using the modem) first.")
                return None, None
            print(f"{BAD} Couldn't open {port}: {exc}")
            return None, None
        modem = Modem(ser)
        print(f"   trying {baud} baud…", end=" ", flush=True)
        if modem.responds():
            print("answered!")
            print(f"{OK} The modem answers at {baud} baud.")
            return modem, baud
        print("no answer")
        ser.close()
    print(f"{BAD} The modem didn't answer at any speed. Check, in this order:")
    print("   • Power: is the modem's LED on? It needs its own 5 V supply that can give 2 A")
    print("     (NOT the Pi's 3.3 V pin). Some boards need the PWR/power key held ~2 s to start.")
    print("   • Ground: modem GND must be connected to a Pi GND pin.")
    print("   • TX/RX crossed: modem TXD → Pi RXD (GPIO15, pin 10); modem RXD → Pi TXD (GPIO14, pin 8).")
    print("     If unsure, swap the two wires and run this again (it can't damage anything).")
    print("   • Serial console still on: raspi-config → Serial Port → login shell = No.")
    return None, None


def run_checks(modem: Modem, wait_network: int = 60) -> bool:
    ok = True
    modem.cmd("ATE0")
    modem.cmd("AT+CMEE=2")  # readable error messages

    step("3. Modem")
    info = modem.cmd("ATI").replace("OK", "").strip()
    print(f"{OK} {info or 'model unknown'}")

    step("4. SIM card")
    pin = modem.cmd("AT+CPIN?")
    if "READY" in pin:
        print(f"{OK} SIM card found and ready.")
    elif "SIM PIN" in pin:
        print(f"{BAD} The SIM is locked with a PIN. Put it in a phone and turn the SIM PIN off.")
        return False
    else:
        print(f"{BAD} No SIM detected ({pin.strip() or 'no answer'}). Check it's inserted the right way, "
              "and that it's a full-size/adapter SIM pushed fully in.")
        return False

    step("5. Network")
    end, reg = time.monotonic() + wait_network, ""
    while True:
        m = re.search(r"\+CREG:\s*\d+,(\d+)", modem.cmd("AT+CREG?"))
        reg = m.group(1) if m else "?"
        if reg in ("1", "5") or time.monotonic() > end:
            break
        print(f"   {REG_STATES.get(reg, 'checking…')}", flush=True)
        time.sleep(5)
    csq = re.search(r"\+CSQ:\s*(\d+),", modem.cmd("AT+CSQ"))
    rssi = int(csq.group(1)) if csq else 99
    signal = ("unknown" if rssi == 99 else "very weak" if rssi < 10 else "fair" if rssi < 15
              else "good" if rssi < 20 else "excellent")
    if reg in ("1", "5"):
        op = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', modem.cmd("AT+COPS?", timeout=10))
        print(f"{OK} {REG_STATES[reg]}: {op.group(1) if op else 'operator name not given'}.")
    else:
        ok = False
        print(f"{BAD} Not on a network ({REG_STATES.get(reg, reg)}).")
        print("   The SIM900A only works on 2G. Philippine networks are switching 2G off, so it may")
        print("   simply have no 2G service where you are. Try: near a window, attach the antenna,")
        print("   a different network's SIM (Smart / TNT vs Globe / TM). If none works → 4G module (SIM7600/A7670).")
    mark = OK if rssi != 99 and rssi >= 10 else WARN
    print(f"{mark} Signal: {signal} ({'?' if rssi == 99 else rssi}/31). "
          + ("" if rssi != 99 and rssi >= 10 else "Attach/move the antenna."))

    step("6. SMS settings")
    print(f"{OK} Text mode on." if "OK" in modem.cmd("AT+CMGF=1") else f"{BAD} Text mode not accepted.")
    modem.cmd('AT+CSCS="GSM"')
    csca = re.search(r'\+CSCA:\s*"([^"]*)"', modem.cmd("AT+CSCA?"))
    if csca and csca.group(1):
        print(f"{OK} SMS centre number: {csca.group(1)}")
    else:
        ok = False
        print(f"{WARN} No SMS centre number on the SIM. Ask your network for it, then run e.g.")
        print('   AT+CSCA="+63XXXXXXXXXX"  (we can add a --smsc option if you need it).')
    return ok


def send_test(modem: Modem, number: str) -> bool:
    step(f"7. Sending a test SMS to {number}")
    text = gsm_text(f"AureoClean test SMS {time.strftime('%H:%M:%S')}. If you got this, SMS works!")
    modem.ser.reset_input_buffer()
    modem.ser.write(f'AT+CMGS="{number}"\r'.encode())
    if ">" not in modem.read_until((">",), 5):
        print(f"{BAD} The modem didn't open a message. Is the SIM ready and registered (steps 4–5)?")
        return False
    modem.ser.write(text.encode() + b"\x1a")
    print("   waiting for the network (up to 60 s)…", flush=True)
    reply = modem.read_until(("+CMGS",), 60)
    if "+CMGS" in reply:
        print(f"{OK} Sent! Check the phone {number}.")
        return True
    code = re.search(r"\+CMS ERROR:\s*(\d+)", reply)
    reason = CMS_ERRORS.get(code.group(1), "") if code else ""
    print(f"{BAD} Not sent: {reply.strip() or 'no reply'}" + (f" — {reason}" if reason else ""))
    print("   Common causes: no load/credit on the SIM, weak signal, or no SMS centre number.")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the GSM modem step by step.")
    parser.add_argument("--port", default=os.environ.get("AUREOCLEAN_SMS_PORT") or "/dev/serial0")
    parser.add_argument("--baud", type=int, default=None, help="try this speed first")
    parser.add_argument("--send", metavar="NUMBER", help="also send a test SMS to this mobile number")
    args = parser.parse_args()

    number = None
    if args.send:
        try:
            number = normalize_phone(args.send)
        except ValueError as exc:
            print(f"{BAD} {exc}")
            return 1

    try:
        import serial  # noqa: F401
    except ImportError:
        print(f"{BAD} pyserial isn't installed. Run: .venv/bin/pip install pyserial")
        return 1

    if not check_port(args.port):
        return 1
    modem, baud = find_baud(args.port, args.baud)
    if not modem:
        return 1
    try:
        healthy = run_checks(modem)
        sent = send_test(modem, number) if number else None
    finally:
        modem.ser.close()

    step("Result")
    if healthy and sent is not False:
        print(f"{OK} The modem is ready. To use it in AureoClean, put these in ~/.config/aureoclean/aureoclean.env:")
    else:
        print(f"{WARN} Fix the ✗ items above first. When it's working, AureoClean needs these settings:")
    print(f"   AUREOCLEAN_SMS_MODE=modem\n   AUREOCLEAN_SMS_PORT={args.port}\n   AUREOCLEAN_SMS_BAUD={baud}")
    if not number:
        print("   Send a real test text with:  .venv/bin/python tools/sms_test.py --send 09XXXXXXXXX")
    return 0 if healthy and sent is not False else 1


if __name__ == "__main__":
    sys.exit(main())
