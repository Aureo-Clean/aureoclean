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

import logging
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


# ---------------------------------------------------------------- test mode
class LogSender:
    mode = "log"

    def __init__(self, outbox: Path):
        self.outbox = outbox

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
class ModemSender:
    mode = "modem"

    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self._lock = threading.Lock()  # one conversation with the modem at a time

    def _open(self):
        try:
            import serial  # pyserial
        except ImportError as exc:
            raise SmsError("pyserial is not installed. Run setup_pi.sh again.") from exc
        try:
            return serial.Serial(self.port, self.baud, timeout=0.3, write_timeout=5)
        except (serial.SerialException, OSError) as exc:
            raise SmsError(f"Cannot open the SMS modem on {self.port}. Is it connected and is the serial port enabled?") from exc

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
        try:
            self._cmd(ser, "AT")
        except SmsError as exc:
            raise SmsError("The modem is not answering. Check its power supply, wiring (TX/RX) and baud rate.") from exc
        self._cmd(ser, "ATE0")          # no echo
        self._cmd(ser, "AT+CMGF=1")     # text mode
        self._cmd(ser, 'AT+CSCS="GSM"')

    def send(self, to: str, text: str) -> None:
        text = gsm_text(text)
        with self._lock:
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

    def check(self) -> dict:
        with self._lock:
            ser = self._open()
            try:
                self._prepare(ser)
                creg = self._cmd(ser, "AT+CREG?")
                csq = self._cmd(ser, "AT+CSQ")
            finally:
                ser.close()
        reg = re.search(r"\+CREG:\s*\d+,(\d+)", creg)
        sig = re.search(r"\+CSQ:\s*(\d+),", csq)
        reg_state = {"0": "not registered", "1": "registered (home)", "2": "searching", "3": "denied", "5": "registered (roaming)"}
        registered = bool(reg and reg.group(1) in ("1", "5"))
        rssi = int(sig.group(1)) if sig else 99
        signal = "unknown" if rssi == 99 else f"{rssi}/31"
        return {
            "mode": "modem",
            "ok": registered,
            "detail": f"Port {self.port}: network {reg_state.get(reg.group(1), '?') if reg else '?'}, signal {signal}.",
        }


def build_sender(mode: str, port: str, baud: int, outbox: Path) -> SmsSender:
    if mode == "modem":
        return ModemSender(port, baud)
    return LogSender(outbox)
