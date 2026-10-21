"""Builds the service with the provider selected by the configuration."""
from __future__ import annotations

from .config import Settings, load_settings
from .service import AureoService
from .sms import build_sender


def build_service(settings: Settings | None = None) -> AureoService:
    settings = settings or load_settings()
    if settings.mode == "firebase":
        from .providers.firebase import FirebaseBackend

        backend = FirebaseBackend(
            settings.firebase_project_id, settings.firebase_web_api_key, settings.credentials_path
        )
    else:
        from .providers.local import LocalBackend

        backend = LocalBackend(settings.data_dir / "aureoclean.db")
    sms = build_sender(settings.sms_mode, settings.sms_port, settings.sms_baud, settings.log_dir / "sms_outbox.log")
    return AureoService(settings, store=backend, identity=backend, sms=sms)
