"""Check the RC522 card reader: run with AureoClean stopped, then tap cards.

    .venv/bin/python tools/rfid_test.py          (Ctrl+C to stop)

Prints the reader's chip version and each card's UID (the app only stores a keyed hash of it).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.rfid import RC522, KNOWN_VERSIONS, ReaderError  # noqa: E402


def main() -> int:
    spi = (os.environ.get("AUREOCLEAN_RFID_SPI", "0.0") or "0.0").split(".")
    try:
        r = RC522(int(spi[0]), int(spi[-1]))
    except ReaderError as exc:
        print(f"Card reader not working: {exc}")
        return 1
    print(f"RC522 found (chip {KNOWN_VERSIONS.get(r.version, hex(r.version))}). Tap a card… (Ctrl+C to stop)")
    last, seen = None, 0.0
    try:
        while True:
            uid = r.read_uid()
            if uid and (uid != last or time.monotonic() - seen > 2.5):
                print(f"  card {uid}  ({len(uid) // 2}-byte UID)")
                last = uid
            if uid:
                seen = time.monotonic()
            time.sleep(0.15)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
