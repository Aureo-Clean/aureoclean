"""Builds the service with the provider selected by the configuration."""
from __future__ import annotations

from .config import Settings, load_settings
from .service import AureoService
from .sms import SavedSmsSettings, build_sender


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
    # A modem chosen in the app (Tools → Find & use modem) wins over the config file.
    saved = SavedSmsSettings(settings.sms_settings_file).load() or {}
    sms = build_sender(saved.get("mode", settings.sms_mode), saved.get("port") or settings.sms_port,
                       saved.get("baud") or settings.sms_baud, settings.log_dir / "sms_outbox.log")
    return AureoService(settings, store=backend, identity=backend, sms=sms)
