"""Sending SMS.

Two senders:

* ``log``   - test mode (default). Messages are written to logs/backend.log
              and logs/sms_outbox.log instead of being sent. Use this until
              the GSM module is wired and tested.
* ``modem`` - a GSM/LTE modem on a serial port, driven with standard AT
              commands (text mode). Works with the SIM900A and with 4G
              modules such as the SIM7600 or A7670, so the modem can be
              swapped without changing code.

SIM900A note: it is 2G-only. The Philippine NTC has ordered 2G/3G networks
switched off (nationwide deadline 31 Dec 2026), so plan for a 4G module.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
import os
import re
import threading
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger("aureoclean.sms")

PHONE_PH_LOCAL = re.compile(r"^0?9\d{9}$")          # 09171234567 or 9171234567
PHONE_PH_INTL = re.compile(r"^(\+?63)9\d{9}$")      # +639171234567 / 639171234567
PHONE_INTL = re.compile(r"^\+[1-9]\d{7,14}$")       # any E.164 number


CMS_HINTS = {
    "38": "network out of order", "41": "temporary network failure", "42": "network congestion",
    "310": "no SIM card", "311": "SIM needs a PIN", "330": "no SMS centre number on the SIM",
    "331": "no network service", "500": "often no load/credit",
}


class SmsError(Exception):
    """Sending failed. The message is safe to show to users."""


def normalize_phone(raw: str) -> str:
    """Return the number in international (E.164) form, e.g. +639171234567.

    Philippine numbers may be typed as 0917 123 4567, 917-123-4567, 63917…
    Raises ValueError for anything else."""
    digits = re.sub(r"[\s\-().]", "", raw or "")
    if PHONE_PH_LOCAL.fullmatch(digits):
        return "+63" + digits[-10:]
    if PHONE_PH_INTL.fullmatch(digits):
        return "+63" + digits[-10:]
    if PHONE_INTL.fullmatch(digits):
        return digits
    raise ValueError("Enter a valid mobile number, e.g. 0917 123 4567.")


def mask_phone(phone: str) -> str:
    """+639171234567 -> +63917•••4567 (for audit logs)."""
    return phone[:6] + "•••" + phone[-4:] if phone and len(phone) > 10 else phone


def gsm_text(text: str, limit: int = 160) -> str:
    """Keep to plain ASCII so one SMS = one segment in GSM 7-bit text mode."""
    replacements = {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "·": "-", "ñ": "n", "Ñ": "N"}
    for a, b in replacements.items():
        text = text.replace(a, b)
    text = text.encode("ascii", "replace").decode()
    return text[:limit]


class SmsSender(Protocol):
    mode: str

    def send(self, to: str, text: str) -> None: ...

    def check(self) -> dict: ...

    def read_inbox(self) -> list["Incoming"]:
        """Texts received since the last call (they are removed from the SIM)."""


@dataclass
class Incoming:
    sender: str
    text: str
    received: str = ""


_CMGL = re.compile(r'\+CMGL:\s*(\d+),"[^"]*","([^"]*)"[^\r\n]*\r?\n(.*?)(?=\r?\n\+CMGL:|\r?\n\r?\nOK|\r?\nOK\s*$)',
                   re.S)


def parse_cmgl(reply: str) -> list[tuple[int, str, str]]:
    """Parse AT+CMGL output into (index, sender, text)."""
    out = []
    for m in _CMGL.finditer(reply):
        out.append((int(m.group(1)), m.group(2), _decode_text(m.group(3).strip())))
    return out


def _decode_text(text: str) -> str:
    # Phones send texts with emoji/accents as UCS-2, which text mode shows as hex.
    if len(text) >= 4 and len(text) % 4 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", text) and not text.isdigit():
        try:
            return bytes.fromhex(text).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return text
    return text


# ---------------------------------------------------------------- test mode
class LogSender:
    mode = "log"

    def __init__(self, outbox: Path):
        self.outbox = outbox
        self._inbox: list[Incoming] = []

    def inject(self, sender: str, text: str) -> None:
        """Pretend a text arrived (test mode / demos)."""
        self._inbox.append(Incoming(sender, text, time.strftime("%Y-%m-%d %H:%M:%S")))

    def read_inbox(self) -> list[Incoming]:
        items, self._inbox = self._inbox, []
        return items

    def send(self, to: str, text: str) -> None:
        text = gsm_text(text)
        log.warning("SMS TEST MODE - not sent. To %s: %s", to, text)
        try:
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            with self.outbox.open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  to {to}: {text}\n")
        except OSError:
            pass

    def check(self) -> dict:
        return {
            "mode": "log",
            "ok": True,
            "detail": f"Test mode: messages are not sent, they are written to {self.outbox}.",
        }


# ---------------------------------------------------------------- real modem
# Only one conversation with the modem at a time (sending, checking, detecting).
MODEM_LOCK = threading.RLock()

# Where a SIM900A / USB modem usually appears on a Raspberry Pi.
CANDIDATE_PORTS = ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0", "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0")
CANDIDATE_BAUDS = (9600, 115200, 57600, 38400, 19200)


def open_port(port: str, baud: int):
    try:
        import serial  # pyserial
    except ImportError as exc:
        raise SmsError("pyserial is not installed. Run setup_pi.sh again.") from exc
    try:
        return serial.Serial(port, baud, timeout=0.3, write_timeout=5)
    except (serial.SerialException, OSError) as exc:
        if "busy" in str(exc).lower():
            raise SmsError(f"{port} is busy: another program (e.g. tools/sms_test.py) is using the modem.") from exc
        raise SmsError(f"Cannot open the SMS modem on {port}. Is it connected and is the serial port enabled?") from exc


class ModemSender:
    mode = "modem"

    def __init__(self, port: str, baud: int, opener=None):
        self.port = port
        self.baud = baud
        self._opener = opener or open_port

    def _open(self):
        return self._opener(self.port, self.baud)

    @staticmethod
    def _read_until(ser, tokens: tuple[str, ...], timeout: float) -> str:
        buf = ""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                buf += chunk.decode(errors="replace")
                if any(t in buf for t in tokens) or "ERROR" in buf:
                    # small grace period for trailing lines
                    time.sleep(0.05)
                    buf += ser.read(ser.in_waiting or 0).decode(errors="replace")
                    return buf
        return buf

    def _cmd(self, ser, cmd: str, timeout: float = 3.0, expect: tuple[str, ...] = ("OK",)) -> str:
        ser.reset_input_buffer()
        ser.write((cmd + "\r").encode())
        reply = self._read_until(ser, expect, timeout)
        if "ERROR" in reply or not any(t in reply for t in expect):
            raise SmsError(f"The modem did not accept '{cmd}' (reply: {reply.strip() or 'nothing'}).")
        return reply

    def _prepare(self, ser) -> None:
        # The SIM900A may need a couple of "AT"s to wake up / lock onto the speed.
        for attempt in range(3):
            try:
                self._cmd(ser, "AT", timeout=1.5)
                break
            except SmsError as exc:
                if attempt == 2:
                    raise SmsError("The modem is not answering. Check its power supply, wiring (TX/RX) "
                                   "and baud rate (Tools → Find & use modem).") from exc
        self._cmd(ser, "ATE0")          # no echo
        self._cmd(ser, "AT+CMGF=1")     # text mode
        self._cmd(ser, 'AT+CSCS="GSM"')

    def send(self, to: str, text: str) -> None:
        text = gsm_text(text)
        with MODEM_LOCK:
            ser = self._open()
            try:
                self._prepare(ser)
                ser.reset_input_buffer()
                ser.write(f'AT+CMGS="{to}"\r'.encode())
                prompt = self._read_until(ser, (">",), 5)
                if ">" not in prompt:
                    raise SmsError("The modem did not open a message. Is a SIM card inserted?")
                ser.write(text.encode() + b"\x1a")  # Ctrl+Z ends the message
                reply = self._read_until(ser, ("+CMGS",), 60)
                if "+CMGS" not in reply:
                    code = re.search(r"\+CMS ERROR:\s*(\d+)", reply)
                    hint = CMS_HINTS.get(code.group(1), f"error {code.group(1)}") if code else "no reply"
                    raise SmsError(f"The network did not accept the SMS ({hint}). "
                                   "Check signal, SIM load and network registration.")
                log.info("SMS sent to %s", mask_phone(to))
            finally:
                ser.close()

    def read_inbox(self) -> list[Incoming]:
        """Read every text on the SIM, then delete them so the SIM never fills up."""
        with MODEM_LOCK:
            ser = self._open()
            try:
                self._prepare(ser)
                reply = self._cmd(ser, 'AT+CMGL="ALL"', timeout=8)
                messages = parse_cmgl(reply)
                for index, _, _ in messages:
                    try:
                        self._cmd(ser, f"AT+CMGD={index}", timeout=5)
                    except SmsError:
                        log.warning("Couldn't delete SMS %s from the SIM", index)
            finally:
                ser.close()
        if messages:
            log.info("Received %d SMS", len(messages))
        return [Incoming(sender, text) for _, sender, text in messages]

    def check(self) -> dict:
        with MODEM_LOCK:
            ser = self._open()
            try:
                self._prepare(ser)
                creg = self._cmd(ser, "AT+CREG?")
                csq = self._cmd(ser, "AT+CSQ")
                try:
                    cops = self._cmd(ser, "AT+COPS?", timeout=10)
                except SmsError:
                    cops = ""
            finally:
                ser.close()
        op = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', cops)
        reg = re.search(r"\+CREG:\s*\d+,(\d+)", creg)
        sig = re.search(r"\+CSQ:\s*(\d+),", csq)
        reg_state = {"0": "not registered", "1": "registered (home)", "2": "searching", "3": "denied", "5": "registered (roaming)"}
        registered = bool(reg and reg.group(1) in ("1", "5"))
        rssi = int(sig.group(1)) if sig else 99
        signal = "unknown" if rssi == 99 else f"{rssi}/31"
        return {
            "mode": "modem",
            "ok": registered,
            "detail": (f"{self.port} @ {self.baud}: network {reg_state.get(reg.group(1), '?') if reg else '?'}"
                       f"{' (' + op.group(1) + ')' if op else ''}, signal {signal}."),
        }


def detect_modem(ports=CANDIDATE_PORTS, bauds=CANDIDATE_BAUDS, opener=None, exists=os.path.exists) -> dict:
    """Find the modem: try each serial port and speed until it answers "OK".

    Returns {"port", "baud", "model"}; raises SmsError with advice if nothing answers."""
    opener = opener or open_port
    seen, tried, errors = set(), [], []
    with MODEM_LOCK:
        for port in ports:
            if not exists(port):
                continue
            real = os.path.realpath(port)
            if real in seen:
                continue
            seen.add(real)
            for baud in bauds:
                try:
                    ser = opener(port, baud)
                except SmsError as exc:
                    errors.append(str(exc))
                    break
                try:
                    sender = ModemSender(port, baud, opener=lambda *_, s=ser: s)
                    for _ in range(3):
                        try:
                            sender._cmd(ser, "AT", timeout=1.0)
                            info = sender._cmd(ser, "ATI").replace("OK", "").strip()
                            return {"port": port, "baud": baud, "model": info or "GSM modem"}
                        except SmsError:
                            continue
                finally:
                    ser.close()
                tried.append(f"{port}@{baud}")
    if errors and not tried:
        raise SmsError(errors[0])
    if not seen:
        raise SmsError("No serial port found. Enable it: sudo raspi-config → Interface Options → Serial Port "
                       "(login shell: No, hardware: Yes), then reboot.")
    raise SmsError("The modem didn't answer on any port or speed. Check: power LED on, GND shared with the Pi, "
                   "SIMT→Pi pin 10 and SIMR→Pi pin 8 (swap them if unsure), VCC_MCU→Pi 3.3 V.")


class SavedSmsSettings:
    """SMS settings chosen in the app (Tools tab), kept in ~/.config/aureoclean/sms.json.
    They take priority over AUREOCLEAN_SMS_* in the config file."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
            return data if data.get("mode") in ("log", "modem") else None
        except (OSError, ValueError):
            return None

    def save(self, mode: str, port: str = "", baud: int = 0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"mode": mode, "port": port, "baud": baud}, indent=2))


def build_sender(mode: str, port: str, baud: int, outbox: Path) -> SmsSender:
    if mode == "modem":
        return ModemSender(port, baud)
    return LogSender(outbox)
