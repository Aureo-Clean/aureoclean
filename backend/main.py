"""AureoClean local API (FastAPI) and web app.

Listens on 127.0.0.1 (the Pi's own screen). When "Website on Wi-Fi" is turned
on, a second listener lets phones and laptops on the same Wi-Fi in too
(private network addresses only; see backend/web.py).

Run with:  python -m backend.main
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .factory import build_service
from .providers.base import ProviderError
from .service import ROLE_ADMIN, ROLE_JANITOR, AureoService, ServiceError, ServiceUnavailable as ServiceUnavailableError, Session
from .web import SECURITY_HEADERS, TYPES, RateLimit, is_private, qr_svg, static_file

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


class LockPinBody(BaseModel):
    password: str = Field(default="", max_length=200)
    pin: str = Field(default="", max_length=12)


class UnlockBody(BaseModel):
    secret: str = Field(default="", max_length=200)


class SimPromoBody(BaseModel):
    name: str = Field(default="", max_length=60)
    days: int = Field(default=15, ge=1, le=400)
    registered: str = Field(default="", max_length=10)


class JobDoneBody(BaseModel):
    note: str = Field(default="", max_length=300)


class ProblemBody(BaseModel):
    location: str = Field(max_length=80)
    locationCode: str = Field(default="", max_length=12)
    issues: list[str] = Field(default_factory=list, max_length=20)
    details: str = Field(default="", max_length=500)


class WebToggleBody(BaseModel):
    enabled: bool


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=200)


class InviteBody(BaseModel):
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=24)
    hours: Optional[int] = Field(default=None, ge=1, le=168)


class MailBody(BaseModel):
    user: str = Field(max_length=254)
    password: str = Field(max_length=64)


class UserStatusBody(BaseModel):
    active: bool


class SimulateSmsBody(BaseModel):
    phone: str = Field(max_length=24)
    text: str = Field(max_length=160)


class DeleteUserBody(BaseModel):
    confirmUsername: str = Field(max_length=40)


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

    # Sign-in and sign-up attempts from the Wi-Fi website, per phone/laptop.
    wifi_auth_limit = RateLimit(20, 600)

    def via_wifi(request: Request) -> bool:
        web = getattr(app.state, "web", None)
        server = request.scope.get("server") or ("", 0)
        return web is not None and server[1] == web.port

    @app.middleware("http")
    async def allowed_clients_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        if via_wifi(request):
            # The Wi-Fi website: same-network devices only, and gentle limits on sign-in attempts.
            if not is_private(client):
                return JSONResponse({"detail": "Only devices on the same Wi-Fi can open AureoClean."}, status_code=403)
            path = request.url.path
            if request.method == "POST" and (path.startswith("/register/") or path in (
                    "/auth/login", "/auth/verify-admin", "/auth/resend-verification")):
                if not wifi_auth_limit.allow(client):
                    return JSONResponse({"detail": "Too many attempts from this device. Wait a few minutes."},
                                        status_code=429)
        elif client not in ("127.0.0.1", "::1", "testclient"):
            # Defence in depth: the main server is bound to 127.0.0.1; refuse anything else anyway.
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        started = time.monotonic()
        response = await call_next(request)
        took = time.monotonic() - started
        if took > 5:  # written to logs/backend.log so slow spots can be found
            log.warning("Slow request: %s %s took %.1f s", request.method, request.url.path, took)
        return response

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

    # ------------------------------------------------ the web app (screens)
    def page(rel: str):
        path = static_file(rel)
        if path is None:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(path, media_type=TYPES[path.suffix], headers=SECURITY_HEADERS)

    @app.get("/")
    def web_index():
        return page("index.html")

    @app.get("/assets/{path:path}")
    def web_asset(path: str):
        return page(f"assets/{path}")

    # ------------------------------------------------ public
    @app.get("/health")
    def health(recheck: bool = False):
        watch = getattr(app.state, "network", None)
        if watch is not None and recheck:
            # Check in the background; wait at most 3 s so the sign-in screen never hangs on it.
            t = threading.Thread(target=watch.check_now, daemon=True, name="net-recheck")
            t.start()
            t.join(3)
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

    @app.post("/auth/verify-admin")
    def verify_admin(body: LoginBody):
        return svc().verify_admin(body.identifier, body.password)

    @app.post("/auth/lock-pin")
    def lock_pin(body: LockPinBody, session: Session = Depends(any_user)):
        return svc().set_lock_pin(session, body.password, body.pin)

    @app.post("/auth/unlock")
    def unlock(body: UnlockBody, session: Session = Depends(any_user)):
        return svc().unlock(session, body.secret)

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
    def admin_overview(cached: bool = False, _: Session = Depends(admin)):
        return svc().admin_overview(cached)

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

    @app.post("/admin/users/{uid}/delete")
    def delete_user(uid: str, body: DeleteUserBody, session: Session = Depends(admin)):
        return svc().delete_user(session, uid, body.confirmUsername)

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

    @app.post("/admin/sms/simulate-reply")
    def sms_simulate(body: SimulateSmsBody, session: Session = Depends(admin)):
        return svc().simulate_sms_reply(session, body.phone, body.text)

    @app.post("/admin/sms/test")
    def sms_test_to(body: PhoneBody, session: Session = Depends(admin)):
        return svc().send_test_sms_to(session, body.phone)

    @app.get("/admin/reports/summary")
    def reports_summary(days: int = 30, _: Session = Depends(admin)):
        from .analysis import dataset, summarize
        records = dataset(svc(), max(1, min(days, 365)))
        return {"days": days, "summary": summarize(records), "records": records}

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

    @app.get("/admin/sms/promo")
    def sim_promo(_: Session = Depends(admin)):
        return svc().get_sim_promo()

    @app.post("/admin/sms/promo")
    def set_sim_promo(body: SimPromoBody, session: Session = Depends(admin)):
        return svc().set_sim_promo(session, body.name, body.days, body.registered)

    @app.post("/admin/sms/promo/clear")
    def clear_sim_promo(session: Session = Depends(admin)):
        return svc().clear_sim_promo(session)

    @app.get("/admin/health")
    def health_report(balance: bool = False, _: Session = Depends(admin)):
        return svc().health.report(force_balance=balance)

    @app.get("/admin/invites")
    def list_invites(_: Session = Depends(admin)):
        return svc().list_invites()

    @app.post("/admin/invites")
    def create_invite(body: InviteBody, session: Session = Depends(admin)):
        web = getattr(app.state, "web", None)
        status = web.status() if web and web.enabled else None
        web_url = status["urls"][0] if status else ""
        return svc().create_admin_invite(session, body.email, body.hours, body.phone, web_url)

    @app.get("/admin/mail")
    def mail_status(_: Session = Depends(admin)):
        return svc().mail_status()

    @app.post("/admin/mail")
    def mail_set(body: MailBody, session: Session = Depends(admin)):
        return svc().set_mail(session, body.user, body.password)

    @app.post("/admin/mail/clear")
    def mail_clear(session: Session = Depends(admin)):
        return svc().clear_mail(session)

    @app.post("/admin/mail/test")
    def mail_test(session: Session = Depends(admin)):
        return svc().send_test_email(session)

    @app.post("/admin/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: str, session: Session = Depends(admin)):
        svc().revoke_invite(session, invite_id)
        return {"ok": True}

    @app.get("/admin/audit")
    def audit(limit: int = 200, _: Session = Depends(admin)):
        return svc().list_audit(limit)

    @app.get("/admin/reports/export.xlsx")
    def export_xlsx(days: int = 30, _: Session = Depends(admin)):
        import io
        import tempfile
        from datetime import datetime

        from .analysis import dataset, summarize
        from .exporting import write_xlsx

        days = max(1, min(days, 365))
        records = dataset(svc(), days)
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
            write_xlsx(tmp.name, summarize(records), records, days)
            data = io.BytesIO(open(tmp.name, "rb").read()).getvalue()
        name = f"AureoClean-reports-{datetime.now():%Y%m%d}.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    # ------------------------------------------------ Wi-Fi website (admin)
    def web_access():
        web = getattr(app.state, "web", None)
        if web is None:
            raise ServiceUnavailableError("The Wi-Fi website isn't available in this mode.")
        return web

    @app.get("/admin/web")
    def web_status(_: Session = Depends(admin)):
        web = getattr(app.state, "web", None)
        return web.status() if web else {"enabled": False, "running": False, "urls": [], "error": None,
                                         "available": False}

    @app.post("/admin/web")
    def web_toggle(body: WebToggleBody, session: Session = Depends(admin)):
        status = web_access().set_enabled(body.enabled)
        svc().audit("wifi_website_" + ("on" if body.enabled else "off"), session.actor, None)
        return status

    @app.get("/admin/web/test")
    def web_selftest(_: Session = Depends(admin)):
        return web_access().self_test()

    @app.get("/admin/web/qr.svg")
    def web_qr(_: Session = Depends(admin)):
        urls = web_access().status()["urls"]
        svg = qr_svg(urls[0])  # the number address: works on every phone
        if svg is None:
            return JSONResponse({"detail": "QR codes need the segno package (run Update AureoClean)."}, status_code=404)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------ reporting a problem (any role)
    @app.get("/report-options")
    def report_options(_: Session = Depends(any_user)):
        return svc().report_options()

    @app.post("/reports/problem")
    def report_problem(body: ProblemBody, session: Session = Depends(any_user)):
        return svc().report_problem(session, body.location, body.issues, body.details, body.locationCode)

    # ------------------------------------------------ janitor only
    @app.get("/janitor/overview")
    def janitor_overview(session: Session = Depends(janitor)):
        return svc().janitor_overview(session)

    @app.get("/janitor/jobs")
    def janitor_jobs(session: Session = Depends(janitor)):
        return svc().janitor_jobs(session)

    @app.post("/janitor/jobs/{report_id}/take")
    def janitor_take(report_id: str, session: Session = Depends(janitor)):
        return svc().janitor_take(session, report_id)

    @app.post("/janitor/jobs/{report_id}/done")
    def janitor_done(report_id: str, body: JobDoneBody, session: Session = Depends(janitor)):
        return svc().janitor_done(session, report_id, body.note)

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
    from .web import WebAccess

    app.state.web = WebAccess(app, settings.sms_settings_file.with_name("web.json"))
    if app.state.web.enabled:  # the Wi-Fi website was left on: start it again
        threading.Timer(2, app.state.web.start).start()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def start_web_signup_import(service: AureoService, every: float = 20.0) -> None:
    """Collect janitor sign-ups and public reports from the website, every few seconds."""
    import threading
    import time

    state = {"tick": 0}

    def loop():
        while True:
            state["tick"] += 1
            # Replies to alerts (OK 12 / DONE 12): every 20 s. Escalations: every minute.
            try:
                service.run_job("sms_inbox", service.two_way.poll)
            except Exception:  # noqa: BLE001
                log.warning("Reading SMS replies failed; will retry", exc_info=True)
            if state["tick"] % 3 == 0:
                try:
                    service.two_way.check_escalations()
                except Exception:  # noqa: BLE001
                    log.warning("Escalation check failed; will retry", exc_info=True)
                try:
                    service.promo_reminder()
                except Exception:  # noqa: BLE001
                    log.warning("SIM promo reminder failed; will retry", exc_info=True)
            try:
                n_signups, n_reports = service.run_job(
                    "web", lambda: (service.import_web_signups(), service.import_public_reports()))
                if n_signups or n_reports:
                    log.info("Imported %d website sign-up(s), %d report(s)", n_signups, n_reports)
            except Exception:  # noqa: BLE001 - keep trying (e.g. internet down)
                log.warning("Website import failed; will retry", exc_info=True)
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="web-signup-import").start()


if __name__ == "__main__":
    run()
