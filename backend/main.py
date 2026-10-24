"""AureoClean local API (FastAPI). Listens on 127.0.0.1 only.

Run with:  python -m backend.main
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .factory import build_service
from .providers.base import ProviderError
from .service import ROLE_ADMIN, ROLE_JANITOR, AureoService, ServiceError, Session

log = logging.getLogger("aureoclean")


# ---------------------------------------------------------------- request bodies
class LoginBody(BaseModel):
    identifier: str = Field(max_length=254)
    password: str = Field(max_length=128)


class JanitorRequestBody(BaseModel):
    fullName: str = Field(max_length=80)
    username: str = Field(max_length=24)
    email: str = Field(max_length=254)
    phone: str = Field(default="", max_length=20)


class StatusBody(BaseModel):
    username: str = Field(max_length=24)
    email: str = Field(max_length=254)


class ActivateBody(BaseModel):
    username: str = Field(max_length=24)
    code: str = Field(max_length=20)
    password: str = Field(max_length=128)


class AdminRegisterBody(BaseModel):
    inviteCode: str = Field(max_length=30)
    fullName: str = Field(max_length=80)
    username: str = Field(max_length=24)
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class ChangePasswordBody(BaseModel):
    currentPassword: str = Field(max_length=128)
    newPassword: str = Field(max_length=128)


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=200)


class InviteBody(BaseModel):
    email: str = Field(default="", max_length=254)
    hours: Optional[int] = Field(default=None, ge=1, le=168)


class UserStatusBody(BaseModel):
    active: bool


class ReportBody(BaseModel):
    category: str = Field(max_length=20)
    title: str = Field(max_length=120)
    location: str = Field(default="", max_length=80)
    details: str = Field(default="", max_length=1200)
    priority: str = Field(default="", max_length=10)
    notify: str = Field(default="none", max_length=10)


class ReportUpdateBody(BaseModel):
    status: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=600)


class PhoneBody(BaseModel):
    phone: str = Field(max_length=24)


class PhoneCodeBody(BaseModel):
    code: str = Field(max_length=10)


class SmsAlertsBody(BaseModel):
    enabled: bool


# ---------------------------------------------------------------- app
def create_app(service: Optional[AureoService] = None) -> FastAPI:
    app = FastAPI(title="AureoClean local API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    def svc() -> AureoService:
        if app.state.service is None:
            app.state.service = build_service()
        return app.state.service

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        # Defence in depth: the server is bound to 127.0.0.1, but refuse
        # anything that somehow arrives from another host anyway.
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "testclient"):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("Unexpected error")
        return JSONResponse({"detail": "Something went wrong on the AureoClean service. Check the backend log."}, status_code=500)

    def bearer(authorization: str = Header(default="")) -> str:
        return authorization[7:] if authorization.lower().startswith("bearer ") else ""

    def any_user(token: str = Depends(bearer)) -> Session:
        return svc().require_session(token)

    def admin(token: str = Depends(bearer)) -> Session:
        return svc().require_session(token, ROLE_ADMIN)

    def janitor(token: str = Depends(bearer)) -> Session:
        return svc().require_session(token, ROLE_JANITOR)

    # ------------------------------------------------ public
    @app.get("/health")
    def health(recheck: bool = False):
        watch = getattr(app.state, "network", None)
        if watch is not None and recheck:
            watch.check_now()
        net = watch.status() if watch is not None else {"internet": "unknown", "message": ""}
        return {"ok": True, "mode": svc().settings.mode, **net}

    @app.post("/auth/login")
    def login(body: LoginBody):
        return svc().login(body.identifier, body.password)

    @app.post("/auth/resend-verification")
    def resend_verification(body: LoginBody):
        return svc().resend_verification(body.identifier, body.password)

    @app.post("/register/janitor")
    def register_janitor(body: JanitorRequestBody):
        return svc().submit_janitor_request(body.fullName, body.username, body.email, body.phone)

    @app.post("/register/status")
    def request_status(body: StatusBody):
        return svc().check_request_status(body.username, body.email)

    @app.post("/register/activate")
    def activate(body: ActivateBody):
        return svc().activate_janitor(body.username, body.code, body.password)

    @app.post("/register/admin")
    def register_admin(body: AdminRegisterBody):
        return svc().register_admin(body.inviteCode, body.fullName, body.username, body.email, body.password)

    # ------------------------------------------------ any signed-in user
    @app.get("/auth/me")
    def me(session: Session = Depends(any_user)):
        return svc().me(session)

    @app.post("/auth/logout")
    def logout(session: Session = Depends(any_user)):
        svc().logout(session)
        return {"ok": True}

    @app.post("/auth/change-password")
    def change_password(body: ChangePasswordBody, session: Session = Depends(any_user)):
        svc().change_password(session, body.currentPassword, body.newPassword)
        return {"ok": True, "message": "Password changed."}

    @app.get("/links")
    def links(session: Session = Depends(any_user)):
        return svc().links(session)

    # ------------------------------------------------ my SMS settings (any role)
    @app.post("/me/phone/start")
    def phone_start(body: PhoneBody, session: Session = Depends(any_user)):
        return svc().start_phone_verification(session, body.phone)

    @app.post("/me/phone/confirm")
    def phone_confirm(body: PhoneCodeBody, session: Session = Depends(any_user)):
        return svc().confirm_phone(session, body.code)

    @app.post("/me/phone/remove")
    def phone_remove(session: Session = Depends(any_user)):
        return svc().remove_phone(session)

    @app.post("/me/sms-alerts")
    def sms_alerts(body: SmsAlertsBody, session: Session = Depends(any_user)):
        return svc().set_sms_alerts(session, body.enabled)

    # ------------------------------------------------ admin only
    @app.get("/admin/overview")
    def admin_overview(_: Session = Depends(admin)):
        return svc().admin_overview()

    @app.get("/admin/requests")
    def list_requests(status: Optional[str] = None, _: Session = Depends(admin)):
        return svc().list_requests(status or None)

    @app.post("/admin/requests/{request_id}/approve")
    def approve(request_id: str, session: Session = Depends(admin)):
        return svc().approve_request(session, request_id)

    @app.post("/admin/requests/{request_id}/reissue-code")
    def reissue(request_id: str, session: Session = Depends(admin)):
        return svc().reissue_code(session, request_id)

    @app.post("/admin/requests/{request_id}/reject")
    def reject(request_id: str, body: RejectBody, session: Session = Depends(admin)):
        return svc().reject_request(session, request_id, body.reason)

    @app.get("/admin/users")
    def list_users(_: Session = Depends(admin)):
        return svc().list_users()

    @app.post("/admin/users/{uid}/status")
    def user_status(uid: str, body: UserStatusBody, session: Session = Depends(admin)):
        return svc().set_user_status(session, uid, body.active)

    @app.post("/admin/users/{uid}/test-sms")
    def test_sms(uid: str, session: Session = Depends(admin)):
        return svc().send_test_sms(session, uid)

    @app.get("/admin/sms/status")
    def sms_status(_: Session = Depends(admin)):
        return {**svc().sms_status(), **{"settings": svc().sms_settings()}}

    @app.get("/admin/sms/settings")
    def sms_settings(_: Session = Depends(admin)):
        return svc().sms_settings()

    @app.post("/admin/sms/detect")
    def sms_detect(session: Session = Depends(admin)):
        return svc().use_detected_modem(session)

    @app.post("/admin/sms/test-mode")
    def sms_test_mode(session: Session = Depends(admin)):
        return svc().use_test_mode(session)

    @app.post("/admin/sms/test")
    def sms_test_to(body: PhoneBody, session: Session = Depends(admin)):
        return svc().send_test_sms_to(session, body.phone)

    @app.get("/admin/reports")
    def list_reports(status: str = "", category: str = "", _: Session = Depends(admin)):
        return svc().list_reports(status, category)

    @app.post("/admin/reports")
    def create_report(body: ReportBody, session: Session = Depends(admin)):
        return svc().create_report(session, body.category, body.title, body.location, body.details,
                                   body.priority, body.notify)

    @app.post("/admin/reports/{report_id}/update")
    def update_report(report_id: str, body: ReportUpdateBody, session: Session = Depends(admin)):
        return svc().update_report(session, report_id, body.status, body.note)

    @app.get("/admin/invites")
    def list_invites(_: Session = Depends(admin)):
        return svc().list_invites()

    @app.post("/admin/invites")
    def create_invite(body: InviteBody, session: Session = Depends(admin)):
        return svc().create_admin_invite(session, body.email, body.hours)

    @app.post("/admin/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: str, session: Session = Depends(admin)):
        svc().revoke_invite(session, invite_id)
        return {"ok": True}

    @app.get("/admin/audit")
    def audit(limit: int = 200, _: Session = Depends(admin)):
        return svc().list_audit(limit)

    # ------------------------------------------------ janitor only
    @app.get("/janitor/overview")
    def janitor_overview(session: Session = Depends(janitor)):
        return svc().janitor_overview(session)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    from .config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    service = build_service(settings)  # fail fast on bad configuration
    app.state.service = service
    from .network import NetworkWatch

    app.state.network = NetworkWatch()
    app.state.network.start()
    log.info("AureoClean API starting in %s mode on %s:%s", settings.mode, settings.host, settings.port)
    start_web_signup_import(service)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def start_web_signup_import(service: AureoService, every: float = 20.0) -> None:
    """Collect janitor sign-ups from the website into Requests, every few seconds."""
    import threading
    import time

    def loop():
        while True:
            try:
                n = service.import_web_signups()
                if n:
                    log.info("Imported %d website sign-up(s)", n)
            except Exception:  # noqa: BLE001 - keep trying (e.g. internet down)
                log.warning("Website sign-up import failed; will retry", exc_info=True)
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="web-signup-import").start()


if __name__ == "__main__":
    run()
