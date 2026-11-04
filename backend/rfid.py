"""RFID card reader (RC522 on the Pi's SPI pins): tap an ID card to sign in.

Wiring (RC522 → Raspberry Pi 4 header; the module is 3.3 V only):

    SDA  → pin 24 (GPIO8, CE0)     SCK  → pin 23 (GPIO11)
    MOSI → pin 19 (GPIO10)         MISO → pin 21 (GPIO9)
    GND  → pin 6                   3.3V → pin 1 or 17 (NOT 5 V)
    RST  → pin 17 (3.3 V) as well  IRQ  → not connected

Turn SPI on once: sudo raspi-config → Interface Options → SPI → Yes, then reboot.
Only the `spidev` package is needed (RST is held high, so no GPIO library).

The reader reads the card's UID (4 or 7 bytes) — 13.56 MHz MIFARE / NFC cards, like most school IDs
and the white sample cards. 125 kHz cards (EM4100 "proximity" cards) can't be read by an RC522.

The UID is never stored: AureoClean keeps an HMAC of it (a keyed hash), see service.card_hash().
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("aureoclean.rfid")

# RC522 registers / commands
COMMAND, COM_IEN, COM_IRQ, DIV_IRQ, ERROR, FIFO_DATA, FIFO_LEVEL, CONTROL, BIT_FRAMING = 0x01, 0x02, 0x04, 0x05, 0x06, 0x09, 0x0A, 0x0C, 0x0D
MODE, TX_CONTROL, TX_ASK, CRC_H, CRC_L, T_MODE, T_PRESCALER, T_RELOAD_H, T_RELOAD_L, VERSION = 0x11, 0x14, 0x15, 0x21, 0x22, 0x2A, 0x2B, 0x2C, 0x2D, 0x37
IDLE, CALC_CRC, TRANSCEIVE, SOFT_RESET = 0x00, 0x03, 0x0C, 0x0F
WUPA, ANTICOLL_CL1, ANTICOLL_CL2, HALT = 0x52, 0x93, 0x95, 0x50
KNOWN_VERSIONS = {0x88: "clone", 0x90: "v0.0", 0x91: "v1.0", 0x92: "v2.0", 0x12: "clone", 0xB2: "clone"}

POLL_SECONDS = 0.15
SAME_CARD_SECONDS = 2.5     # a card held on the reader counts once; lifted this long → counts again
RETRY_SECONDS = 30


class ReaderError(Exception):
    pass


class RC522:
    """Minimal MFRC522 driver over spidev: wake a card and read its UID."""

    def __init__(self, bus: int = 0, device: int = 0, spi=None):
        if spi is None:
            try:
                import spidev  # type: ignore
            except ImportError as exc:
                raise ReaderError("The spidev package is missing. Run setup_pi.sh again (or: .venv/bin/pip install spidev).") from exc
            if not os.path.exists(f"/dev/spidev{bus}.{device}"):
                raise ReaderError("SPI is off. Turn it on: sudo raspi-config → Interface Options → SPI → Yes, then reboot.")
            spi = spidev.SpiDev()
            try:
                spi.open(bus, device)
            except (OSError, PermissionError) as exc:
                raise ReaderError(f"Can't open /dev/spidev{bus}.{device}: {exc}. Add the user to the spi group: "
                                  "sudo usermod -aG spi $USER, then reboot.") from exc
            spi.max_speed_hz = 1_000_000
            spi.mode = 0
        self.spi = spi
        self.version = 0
        self.init()

    # ---------------------------------------------------------------- registers
    def w(self, reg: int, val: int) -> None:
        self.spi.xfer2([(reg << 1) & 0x7E, val & 0xFF])

    def r(self, reg: int) -> int:
        return self.spi.xfer2([((reg << 1) & 0x7E) | 0x80, 0])[1]

    def set_bits(self, reg: int, mask: int) -> None:
        self.w(reg, self.r(reg) | mask)

    def clear_bits(self, reg: int, mask: int) -> None:
        self.w(reg, self.r(reg) & ~mask)

    def init(self) -> None:
        self.w(COMMAND, SOFT_RESET)
        time.sleep(0.05)
        self.version = self.r(VERSION)
        if self.version in (0x00, 0xFF):
            raise ReaderError("No RC522 answered on SPI. Check the wiring (SDA→pin 24, SCK→23, MOSI→19, MISO→21, "
                              "3.3V and RST→3.3 V, GND).")
        self.w(T_MODE, 0x8D)
        self.w(T_PRESCALER, 0x3E)
        self.w(T_RELOAD_L, 30)
        self.w(T_RELOAD_H, 0)
        self.w(TX_ASK, 0x40)
        self.w(MODE, 0x3D)
        if (self.r(TX_CONTROL) & 0x03) != 0x03:
            self.set_bits(TX_CONTROL, 0x03)          # antenna on

    # ---------------------------------------------------------------- talking to the card
    def transceive(self, data: list[int], bits: int = 0) -> tuple[list[int], int]:
        """Send bytes to the card; returns (answer bytes, answer length in bits). Raises on no card / error."""
        self.w(COM_IEN, 0x77 | 0x80)
        self.clear_bits(COM_IRQ, 0x80)
        self.set_bits(FIFO_LEVEL, 0x80)              # flush the FIFO
        self.w(COMMAND, IDLE)
        for b in data:
            self.w(FIFO_DATA, b)
        self.w(BIT_FRAMING, bits & 0x07)
        self.w(COMMAND, TRANSCEIVE)
        self.set_bits(BIT_FRAMING, 0x80)             # start sending
        irq = 0
        for _ in range(250):
            irq = self.r(COM_IRQ)
            if irq & 0x30 or irq & 0x01:             # RxIRq/IdleIRq, or the timer ran out
                break
        self.clear_bits(BIT_FRAMING, 0x80)
        if not irq & 0x30:
            raise ReaderError("no card")
        if self.r(ERROR) & 0x1B:                     # buffer overflow, collision, parity, protocol
            raise ReaderError("card read error")
        n = self.r(FIFO_LEVEL)
        last = self.r(CONTROL) & 0x07
        out = [self.r(FIFO_DATA) for _ in range(min(n, 16))]
        return out, ((n - 1) * 8 + last) if last else n * 8

    def crc(self, data: list[int]) -> list[int]:
        self.w(COMMAND, IDLE)
        self.clear_bits(DIV_IRQ, 0x04)
        self.set_bits(FIFO_LEVEL, 0x80)
        for b in data:
            self.w(FIFO_DATA, b)
        self.w(COMMAND, CALC_CRC)
        for _ in range(255):
            if self.r(DIV_IRQ) & 0x04:
                break
        self.w(COMMAND, IDLE)
        return [self.r(CRC_L), self.r(CRC_H)]

    def _anticoll(self, level: int) -> list[int]:
        out, _ = self.transceive([level, 0x20])
        if len(out) != 5 or (out[0] ^ out[1] ^ out[2] ^ out[3]) != out[4]:
            raise ReaderError("card read error")
        return out

    def _select(self, level: int, part: list[int]) -> int:
        frame = [level, 0x70] + part
        out, _ = self.transceive(frame + self.crc(frame))
        if not out:
            raise ReaderError("card read error")
        return out[0]                                # SAK

    def read_uid(self) -> Optional[str]:
        """The UID of a card on the reader (hex, e.g. "04A1B2C3"), or None when there is none."""
        try:
            atqa, bits = self.transceive([WUPA], bits=7)
            if bits != 16:
                return None
            cl1 = self._anticoll(ANTICOLL_CL1)
            if cl1[0] == 0x88:                       # cascade tag: a 7-byte UID
                self._select(ANTICOLL_CL1, cl1)
                cl2 = self._anticoll(ANTICOLL_CL2)
                self._select(ANTICOLL_CL2, cl2)
                uid = cl1[1:4] + cl2[0:4]
            else:
                self._select(ANTICOLL_CL1, cl1)
                uid = cl1[0:4]
            self.halt()
            return "".join(f"{b:02X}" for b in uid)
        except ReaderError:
            return None

    def halt(self) -> None:
        frame = [HALT, 0x00]
        try:
            self.transceive(frame + self.crc(frame))
        except ReaderError:
            pass                                     # a halted card doesn't answer: that's normal


class CardReader:
    """Background thread: reads cards and calls on_card(uid) once per tap."""

    def __init__(self, on_card: Callable[[str], None], mode: str = "auto", bus: int = 0, device: int = 0,
                 make: Optional[Callable[[], object]] = None, clock: Callable[[], float] = time.monotonic):
        self.on_card = on_card
        self.mode = (mode or "auto").lower()
        self.bus, self.device = bus, device
        self.make = make or (lambda: RC522(self.bus, self.device))
        self.clock = clock
        self.available = False
        self.ever_found = False
        self.detail = "Starting…"
        self.version = ""
        self.last_scan: Optional[float] = None
        self.scans = 0
        self._last_uid: Optional[str] = None
        self._last_seen = 0.0
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            if not self.enabled:
                self.detail = "Turned off (AUREOCLEAN_RFID=off)."
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="rfid")
        self._thread.start()

    def seen(self, uid: Optional[str]) -> bool:
        """Debounce: True when this read is a new tap."""
        now = self.clock()
        if not uid:
            return False
        new = uid != self._last_uid or now - self._last_seen > SAME_CARD_SECONDS
        self._last_uid, self._last_seen = uid, now
        return new

    def _run(self) -> None:
        while True:
            try:
                dev = self.make()
            except ReaderError as exc:
                self.available, self.detail = False, str(exc)
                time.sleep(RETRY_SECONDS)
                continue
            except Exception as exc:  # noqa: BLE001
                self.available, self.detail = False, f"{type(exc).__name__}: {exc}"[:200]
                time.sleep(RETRY_SECONDS)
                continue
            self.available, self.ever_found = True, True
            self.version = KNOWN_VERSIONS.get(getattr(dev, "version", 0), hex(getattr(dev, "version", 0)))
            self.detail = f"RC522 ready on SPI {self.bus}.{self.device} (chip {self.version}). Tap a card to test."
            log.info("RFID: %s", self.detail)
            errors = 0
            while True:
                try:
                    uid = dev.read_uid()
                    errors = 0
                except Exception as exc:  # noqa: BLE001 - SPI glitch / unplugged: reopen
                    errors += 1
                    if errors >= 5:
                        self.available, self.detail = False, f"The card reader stopped answering ({exc}). Retrying…"
                        break
                    uid = None
                if self.seen(uid):
                    self.last_scan = time.time()
                    self.scans += 1
                    try:
                        self.on_card(uid)
                    except Exception:  # noqa: BLE001
                        log.warning("Handling a card tap failed", exc_info=True)
                time.sleep(POLL_SECONDS)
            time.sleep(2)

    def status(self) -> dict:
        return {"enabled": self.enabled, "available": self.available, "everFound": self.ever_found,
                "detail": self.detail, "lastScan": self.last_scan, "scans": self.scans}
