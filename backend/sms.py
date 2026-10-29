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
from typing import Optional, Protocol

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
    replacements = {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "·": "-", "ñ": "n", "Ñ": "N", "°": "", "Ω": " ohm", "₂": "2"}
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
CANDIDATE_BAUDS = (9600, 115200, 57600, 38400, 19200, 4800)


def open_port(port: str, baud: int):
    try:
        import serial  # pyserial
    except ImportError as exc:
        raise SmsError("pyserial is not installed. Run setup_pi.sh again.") from exc
    try:
        return serial.Serial(port, baud, timeout=0.3, write_timeout=5)
    except (serial.SerialException, OSError) as exc:
        if "permission denied" in str(exc).lower() or getattr(exc, "errno", None) == 13:
            raise SmsError(f"No permission to use {port}. Run: sudo usermod -aG dialout $USER — then reboot the Pi.") from exc
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
                                   "and baud rate (System → SMS → Find & use modem).") from exc
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

    def ussd(self, code: str, timeout: float = 20) -> str:
        """Dial a USSD code (e.g. a load-balance code) and return the network's answer."""
        if not re.fullmatch(r"[*#0-9]{2,20}", code or ""):
            raise SmsError("The balance code should look like *143# (digits, * and # only).")
        with MODEM_LOCK:
            ser = self._open()
            try:
                self._prepare(ser)
                ser.reset_input_buffer()
                ser.write(f'AT+CUSD=1,"{code}",15\r'.encode())
                reply = self._read_until(ser, ("+CUSD:",), timeout)
                if "+CUSD:" in reply:  # let the rest of a long answer arrive
                    reply += self._read_until(ser, ('",',), 3)
                try:
                    self._cmd(ser, "AT+CUSD=2", timeout=2)  # close the USSD session
                except SmsError:
                    pass
            finally:
                ser.close()
        m = re.search(r'\+CUSD:\s*\d+\s*,\s*"(.*?)"', reply, re.S)
        if not m:
            raise SmsError("The network didn't answer the balance code. Check the code and the signal.")
        return _decode_text(m.group(1).strip())

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


def serial_console_on(files=("/boot/firmware/cmdline.txt", "/boot/cmdline.txt")) -> Optional[str]:
    """The Pi's serial login console still uses the UART (it swallows the modem's answers)? → the file that says so."""
    for f in files:
        try:
            if re.search(r"console=(serial0|ttyS0|ttyAMA0)\b", Path(f).read_text()):
                return f
        except OSError:
            continue
    return None


def _wake(ser) -> bytes:
    """A SIM900A that was just powered, or set to auto-baud, needs a few "AT"s before it answers."""
    heard = b""
    for _ in range(3):
        ser.write(b"AT\r")
        time.sleep(0.25)
        heard += ser.read(ser.in_waiting or 0) or b""
    return heard


def detect_modem(ports=CANDIDATE_PORTS, bauds=CANDIDATE_BAUDS, opener=None, exists=os.path.exists,
                 console=serial_console_on) -> dict:
    """Find the modem: try each serial port and speed until it answers "OK".

    Returns {"port", "baud", "model"}; raises SmsError with advice for what went wrong:
    no port, the serial console still on, garbled bytes (wrong level/ground), or silence."""
    opener = opener or open_port
    seen, tried, errors = set(), [], []
    garbled: dict[str, int] = {}
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
                    noise = _wake(ser)
                    sender = ModemSender(port, baud, opener=lambda *_, s=ser: s)
                    for _ in range(3):
                        try:
                            sender._cmd(ser, "AT", timeout=1.0)
                            try:
                                info = sender._cmd(ser, "ATI").replace("OK", "").strip()
                            except SmsError:
                                info = ""
                            log.info("Modem found on %s at %d baud", port, baud)
                            return {"port": port, "baud": baud, "model": info or "GSM modem"}
                        except SmsError as exc:
                            got = str(exc).split("reply: ", 1)[-1].rstrip(").") if "reply: " in str(exc) else ""
                            if got and got != "nothing":
                                noise += got.encode(errors="replace")
                            continue
                    if noise.strip(b"\x00\r\n "):
                        garbled[port] = garbled.get(port, 0) + len(noise)
                finally:
                    ser.close()
                tried.append(f"{port}@{baud}")
    if errors and not tried:
        raise SmsError(errors[0])
    if not seen:
        raise SmsError("No serial port found. Enable it: sudo raspi-config → Interface Options → Serial Port "
                       "(login shell: No, hardware: Yes), then reboot.")
    cmdline = console() if console else None
    if cmdline:
        raise SmsError(f"The Pi's serial login console is still switched on ({cmdline}), so it takes the modem's "
                       "answers. Run sudo raspi-config → Interface Options → Serial Port → login shell: No, "
                       "serial hardware: Yes, then reboot and try again.")
    if garbled:
        port = max(garbled, key=garbled.get)
        raise SmsError(f"The modem is sending something on {port}, but it's unreadable at every speed. Usually: the "
                       "modem's GND isn't joined to a Pi GND pin, or its TX is a 5 V signal — use the 3VT pin, or "
                       "a 1 kΩ/2 kΩ voltage divider into Pi pin 10.")
    raise SmsError("The modem didn't answer on any port or speed — nothing came back at all. Check: the modem has its "
                   "own 5 V 2 A supply and is switched on (power LED on, NET LED blinking — hold its PWR key ~2 s if "
                   "not), its GND is joined to a Pi GND pin (e.g. pin 6), the modem's TX goes to Pi pin 10 and its RX "
                   "to Pi pin 8 (use the 3VT/3VR pins if the board has them; try swapping TX/RX). Never connect the "
                   "modem's power to the Pi.")


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


# ---------------------------------------------------------------- send tracking
class SendTracker:
    """Remembers how the last texts went, for the System health panel.
    (A promo that ran out, or one that doesn't cover the other network, shows up here.)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_ok_at: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.failures_in_a_row = 0

    def ok(self):
        from .security import iso, utcnow
        with self.lock:
            self.last_ok_at, self.failures_in_a_row = iso(utcnow()), 0

    def fail(self, to: str, error: str):
        from .security import iso, utcnow
        with self.lock:
            self.last_error, self.last_error_at = f"to {to}: {error}", iso(utcnow())
            self.failures_in_a_row += 1


class TrackedSender:
    """Wraps any sender; everything except send() passes straight through."""

    def __init__(self, inner, tracker: SendTracker):
        self.inner, self.tracker = inner, tracker

    def send(self, to: str, text: str) -> None:
        try:
            self.inner.send(to, text)
        except SmsError as exc:
            self.tracker.fail(to, str(exc))
            raise
        self.tracker.ok()

    def __getattr__(self, name):
        return getattr(self.inner, name)


# ---------------------------------------------------------------- SIM promo
class SimPromo:
    """The SIM's promo (e.g. unli texts for 15 days), entered in Tools so the app
    can warn before it runs out. Kept in ~/.config/aureoclean/sim_promo.json."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
