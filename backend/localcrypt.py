"""Encryption at rest for the Pi's local copy of the data.

Every document in the local database is encrypted with AES-256-GCM. The key is a random
32-byte file, ~/.config/aureoclean/local.key, readable only by the AureoClean user (0600,
in a 0700 folder). Someone who copies the database file alone can't read it; the database
and the key together are protected by the Pi's own user account — keep the SD card safe.
"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Optional

PREFIX = "enc1:"


def load_or_create_key(path: Path) -> bytes:
    path = Path(path).expanduser()
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError(f"{path} is not a valid AureoClean key (expected 32 bytes).")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    key = secrets.token_bytes(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class Cipher:
    def __init__(self, key: Optional[bytes]):
        self.enabled = key is not None
        if key is not None:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            self._aes = AESGCM(key)

    def seal(self, text: str, context: str) -> str:
        if not self.enabled:
            return text
        nonce = secrets.token_bytes(12)
        ct = self._aes.encrypt(nonce, text.encode(), context.encode())
        return PREFIX + base64.b64encode(nonce + ct).decode()

    def open(self, stored: str, context: str) -> str:
        if not stored.startswith(PREFIX):
            return stored                       # written before encryption was switched on
        if not self.enabled:
            raise ValueError("This data is encrypted, but no key was given.")
        raw = base64.b64decode(stored[len(PREFIX):])
        return self._aes.decrypt(raw[:12], raw[12:], context.encode()).decode()
