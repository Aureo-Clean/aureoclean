"""Firebase implementation: Firebase Authentication + Cloud Firestore.

Uses the Admin SDK (service-account credentials) for privileged operations and
the Firebase Auth REST API for password sign-in. The service-account JSON must
stay outside the app folder; its path comes from GOOGLE_APPLICATION_CREDENTIALS.
"""
from __future__ import annotations

from typing import Callable, Optional

import requests

from .base import AccountDisabled, AccountExists, AlreadyExists, ProviderError

SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"


def _friendly(exc: Exception) -> ProviderError:
    """Turn Google Cloud errors into a message a person can act on."""
    text = str(exc)
    if "does not exist for project" in text:
        return ProviderError(
            "Cloud Firestore isn't set up for this Firebase project yet. In the Firebase Console open "
            "Build > Firestore Database > Create database (choose a Singapore region), then try again."
        )
    if "PERMISSION_DENIED" in text or "403" in text[:10]:
        return ProviderError("The service-account key doesn't have permission for Firestore. Download a new key "
                             "from Firebase Console > Project settings > Service accounts.")
    if any(t in text for t in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "503", "Failed to connect", "failed to connect")):
        return ProviderError("Cannot reach Firebase. Check the Pi's internet connection.")
    return ProviderError(f"Firebase error: {text.splitlines()[0][:200]}")


def _guard_firestore(cls):
    """Wrap the Firestore methods so Google errors become readable ProviderErrors."""
    from functools import wraps

    def wrap(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (AlreadyExists, ProviderError):
                raise
            except Exception as exc:
                module = type(exc).__module__
                if module.startswith(("google.", "grpc")):
                    raise _friendly(exc) from exc
                raise

        return inner

    for name in ("get", "create", "set", "delete", "query", "latest", "transact"):
        setattr(cls, name, wrap(getattr(cls, name)))
    return cls


@_guard_firestore
class FirebaseBackend:
    def __init__(self, project_id: str, web_api_key: str, credentials_path: str):
        try:
            import firebase_admin
            from firebase_admin import auth, credentials, firestore
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError("firebase-admin is not installed. Run setup_pi.sh.") from exc

        if not project_id:
            raise ProviderError("AUREOCLEAN_FIREBASE_PROJECT_ID is not set.")
        if not web_api_key:
            raise ProviderError("AUREOCLEAN_FIREBASE_WEB_API_KEY is not set.")
        if not credentials_path:
            raise ProviderError("GOOGLE_APPLICATION_CREDENTIALS is not set.")

        cred = credentials.Certificate(credentials_path)
        try:
            app = firebase_admin.get_app("aureoclean")
        except ValueError:
            app = firebase_admin.initialize_app(cred, {"projectId": project_id}, name="aureoclean")

        self._auth = auth
        self._app = app
        self._firestore = firestore
        self._db = firestore.client(app=app)
        self._api_key = web_api_key
        self._http = requests.Session()

    # ------------------------------------------------------------ DocStore
    def _ref(self, collection: str, doc_id: str):
        return self._db.collection(collection).document(doc_id)

    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        snap = self._ref(collection, doc_id).get()
        return snap.to_dict() if snap.exists else None

    def create(self, collection: str, doc_id: str, data: dict) -> None:
        from google.api_core.exceptions import AlreadyExists as GAlreadyExists, Conflict

        try:
            self._ref(collection, doc_id).create(data)
        except (GAlreadyExists, Conflict) as exc:
            raise AlreadyExists(f"{collection}/{doc_id}") from exc

    def set(self, collection: str, doc_id: str, data: dict) -> None:
        self._ref(collection, doc_id).set(data)

    def delete(self, collection: str, doc_id: str) -> None:
        self._ref(collection, doc_id).delete()

    def query(self, collection: str, **equals) -> list[tuple[str, dict]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self._db.collection(collection)
        for field, value in equals.items():
            q = q.where(filter=FieldFilter(field, "==", value))
        return [(snap.id, snap.to_dict()) for snap in q.stream()]

    def latest(self, collection: str, field: str, limit: int) -> list[tuple[str, dict]]:
        q = (
            self._db.collection(collection)
            .order_by(field, direction=self._firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [(snap.id, snap.to_dict()) for snap in q.stream()]

    def transact(self, collection: str, doc_id: str, fn: Callable[[Optional[dict]], Optional[dict]]):
        ref = self._ref(collection, doc_id)

        @self._firestore.transactional
        def run(transaction):
            snap = ref.get(transaction=transaction)
            result = fn(snap.to_dict() if snap.exists else None)
            if result is not None:
                transaction.set(ref, result)
            return result

        return run(self._db.transaction())

    # ------------------------------------------------------------ Identity
    def create_account(self, email: str, password: str, display_name: str) -> str:
        try:
            user = self._auth.create_user(
                email=email, password=password, display_name=display_name, app=self._app
            )
        except self._auth.EmailAlreadyExistsError as exc:
            raise AccountExists(email) from exc
        return user.uid

    def verify_password(self, email: str, password: str) -> Optional[str]:
        try:
            resp = self._http.post(
                SIGN_IN_URL,
                params={"key": self._api_key},
                json={"email": email, "password": password, "returnSecureToken": True},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError("Cannot reach Firebase. Check the internet connection.") from exc

        if resp.ok:
            return resp.json().get("localId")

        message = ""
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        if message.startswith("USER_DISABLED"):
            raise AccountDisabled(email)
        if message.startswith(("EMAIL_NOT_FOUND", "INVALID_PASSWORD", "INVALID_LOGIN_CREDENTIALS")):
            return None
        if message.startswith("TOO_MANY_ATTEMPTS"):
            raise ProviderError("Firebase temporarily blocked sign-in attempts. Try again later.")
        if "API key not valid" in message or message.startswith("API_KEY"):
            raise ProviderError("The Firebase Web API key is invalid. Check the configuration.")
        raise ProviderError(f"Firebase sign-in failed ({message or resp.status_code}).")

    def find_uid(self, email: str) -> Optional[str]:
        try:
            return self._auth.get_user_by_email(email, app=self._app).uid
        except self._auth.UserNotFoundError:
            return None

    def set_password(self, uid: str, password: str) -> None:
        self._auth.update_user(uid, password=password, app=self._app)

    def set_disabled(self, uid: str, disabled: bool) -> None:
        self._auth.update_user(uid, disabled=disabled, app=self._app)

    def delete_account(self, uid: str) -> None:
        self._auth.delete_user(uid, app=self._app)
