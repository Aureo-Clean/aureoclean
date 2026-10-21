"""Interfaces shared by the local (SQLite) and Firebase providers.

The service layer only talks to these two interfaces, so the exact same
business rules run in both modes.
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol


class AlreadyExists(Exception):
    """A document or account with that key already exists."""


class AccountExists(Exception):
    """An authentication account already exists for this email."""


class AccountDisabled(Exception):
    """The authentication account is disabled."""


class ProviderError(Exception):
    """The storage or auth provider failed (network, quota, misconfiguration)."""


class DocStore(Protocol):
    """A tiny document store modelled on Firestore."""

    def get(self, collection: str, doc_id: str) -> Optional[dict]: ...

    def create(self, collection: str, doc_id: str, data: dict) -> None:
        """Create a document; raise AlreadyExists if the ID is taken (atomic)."""

    def set(self, collection: str, doc_id: str, data: dict) -> None: ...

    def delete(self, collection: str, doc_id: str) -> None: ...

    def query(self, collection: str, **equals) -> list[tuple[str, dict]]:
        """All documents whose fields equal the given values (no ordering)."""

    def transact(
        self, collection: str, doc_id: str, fn: Callable[[Optional[dict]], Optional[dict]]
    ) -> Optional[dict]:
        """Atomically read a document, call ``fn(current)`` and write its result.

        ``fn`` may raise to abort without writing. Returning None skips the write.
        Returns whatever ``fn`` returned."""

    def latest(self, collection: str, field: str, limit: int) -> list[tuple[str, dict]]:
        """The ``limit`` documents with the largest value of ``field``, newest first."""


class Identity(Protocol):
    """Password accounts (Firebase Authentication, or a local equivalent)."""

    def create_account(self, email: str, password: str, display_name: str) -> str:
        """Create an account and return its UID. Raise AccountExists if taken."""

    def verify_password(self, email: str, password: str) -> Optional[str]:
        """Return the UID if the password is right, else None.
        Raise AccountDisabled if the account is disabled."""

    def find_uid(self, email: str) -> Optional[str]: ...

    def set_password(self, uid: str, password: str) -> None: ...

    def set_disabled(self, uid: str, disabled: bool) -> None: ...

    def delete_account(self, uid: str) -> None:
        """Only used to roll back an account created moments earlier."""
