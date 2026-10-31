"""Builds the service with the provider selected by the configuration."""
from __future__ import annotations

from .config import Settings, load_settings
from .service import AureoService
from .sms import SavedSmsSettings, build_sender


def build_service(settings: Settings | None = None) -> AureoService:
    settings = settings or load_settings()
    from .localcrypt import load_or_create_key
    from .providers.local import LocalBackend

    # The Pi's own copy of the data, encrypted at rest (key next to the other private settings).
    key = load_or_create_key(settings.sms_settings_file.with_name("local.key"))
    local = LocalBackend(settings.data_dir / "aureoclean.db", key=key)
    if settings.mode == "firebase":
        import os

        # Use the Pi's own DNS lookups for Firebase's connection (gRPC's built-in resolver
        # can stall on some home/school networks, which shows up as "deadline exceeded").
        os.environ.setdefault("GRPC_DNS_RESOLVER", "native")
        from .providers.synced import SyncedBackend

        def remote():
            from .providers.firebase import FirebaseBackend
            return FirebaseBackend(settings.firebase_project_id, settings.firebase_web_api_key, settings.credentials_path,
                                   settings.firebase_database_url)

        # Firebase always, with the local copy for speed and for when the Wi-Fi is down.
        backend = SyncedBackend(local, remote, prefix=settings.collection_prefix)
        backend.check_online()
    else:
        backend = local
    # A modem chosen in the app (Tools → Find & use modem) wins over the config file.
    saved = SavedSmsSettings(settings.sms_settings_file).load() or {}
    sms = build_sender(saved.get("mode", settings.sms_mode), saved.get("port") or settings.sms_port,
                       saved.get("baud") or settings.sms_baud, settings.log_dir / "sms_outbox.log")
    return AureoService(settings, store=backend, identity=backend, sms=sms)
