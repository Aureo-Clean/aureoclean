"""Small HTTP client for the local AureoClean API (standard library only)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

BASE_URL = f"http://127.0.0.1:{os.environ.get('AUREOCLEAN_PORT', '8765')}"


# Talking to the GSM modem (finding it, sending a text) can take up to a minute.
SMS_TIMEOUT = 90


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


class ApiClient:
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url
        self.token: Optional[str] = None
        # Never route loopback traffic through a system proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: Optional[dict] = None,
              timeout: float = 30) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            message = f"Request failed ({exc.code})."
            try:
                detail = json.loads(exc.read() or b"{}").get("detail")
                if isinstance(detail, str):
                    message = detail
                elif isinstance(detail, list) and detail:  # pydantic validation errors
                    message = "Please check the form: " + "; ".join(
                        str(d.get("loc", [""])[-1]) + " " + d.get("msg", "") for d in detail
                    )
            except (ValueError, AttributeError):
                pass
            raise ApiError(message, exc.code) from exc
        except urllib.error.URLError as exc:
            raise ApiError("Cannot reach the AureoClean service. Is the backend running?") from exc
        except TimeoutError as exc:
            raise ApiError("The AureoClean service took too long to respond.") from exc

    # ---------------------------------------------------------------- public
    def health(self, recheck: bool = False):
        return self._call("GET", "/health", query={"recheck": "true" if recheck else ""})

    def login(self, identifier: str, password: str) -> dict:
        result = self._call("POST", "/auth/login", {"identifier": identifier, "password": password})
        self.token = result["token"]
        return result

    def resend_verification(self, identifier: str, password: str) -> dict:
        return self._call("POST", "/auth/resend-verification", {"identifier": identifier, "password": password})

    def logout(self) -> None:
        try:
            if self.token:
                self._call("POST", "/auth/logout")
        finally:
            self.token = None

    def request_janitor(self, full_name, username, email, phone):
        return self._call("POST", "/register/janitor", {"fullName": full_name, "username": username, "email": email, "phone": phone})

    def request_status(self, username, email):
        return self._call("POST", "/register/status", {"username": username, "email": email})

    def activate(self, username, code, password):
        return self._call("POST", "/register/activate", {"username": username, "code": code, "password": password})

    def register_admin(self, invite_code, full_name, username, email, password):
        return self._call(
            "POST", "/register/admin",
            {"inviteCode": invite_code, "fullName": full_name, "username": username, "email": email, "password": password},
        )

    # ---------------------------------------------------------------- signed in
    def me(self):
        return self._call("GET", "/auth/me")

    def change_password(self, current, new):
        return self._call("POST", "/auth/change-password", {"currentPassword": current, "newPassword": new})

    def links(self):
        return self._call("GET", "/links")

    def phone_start(self, phone):
        return self._call("POST", "/me/phone/start", {"phone": phone}, timeout=SMS_TIMEOUT)

    def phone_confirm(self, code):
        return self._call("POST", "/me/phone/confirm", {"code": code})

    def phone_remove(self):
        return self._call("POST", "/me/phone/remove")

    def set_sms_alerts(self, enabled: bool):
        return self._call("POST", "/me/sms-alerts", {"enabled": enabled})

    def delete_user(self, uid, confirm_username: str):
        return self._call("POST", f"/admin/users/{urllib.parse.quote(uid)}/delete",
                          {"confirmUsername": confirm_username})

    def test_sms(self, uid):
        return self._call("POST", f"/admin/users/{urllib.parse.quote(uid)}/test-sms", timeout=SMS_TIMEOUT)

    def set_lock_pin(self, password: str, pin: str):
        return self._call("POST", "/auth/lock-pin", {"password": password, "pin": pin})

    def unlock(self, secret: str):
        return self._call("POST", "/auth/unlock", {"secret": secret})

    def sim_promo(self):
        return self._call("GET", "/admin/sms/promo")

    def set_sim_promo(self, name: str, days: int, registered: str = ""):
        return self._call("POST", "/admin/sms/promo", {"name": name, "days": days, "registered": registered})

    def clear_sim_promo(self):
        return self._call("POST", "/admin/sms/promo/clear", {})

    def system_health(self, check_balance: bool = False):
        return self._call("GET", "/admin/health", query={"balance": "true" if check_balance else ""},
                          timeout=SMS_TIMEOUT)

    def sms_settings(self):
        return self._call("GET", "/admin/sms/settings")

    def sms_detect(self):
        return self._call("POST", "/admin/sms/detect", timeout=SMS_TIMEOUT)

    def sms_test_mode(self):
        return self._call("POST", "/admin/sms/test-mode")

    def sms_simulate_reply(self, phone: str, text: str):
        return self._call("POST", "/admin/sms/simulate-reply", {"phone": phone, "text": text}, timeout=SMS_TIMEOUT)

    def sms_test_to(self, phone: str):
        return self._call("POST", "/admin/sms/test", {"phone": phone}, timeout=SMS_TIMEOUT)

    def sms_status(self):
        return self._call("GET", "/admin/sms/status", timeout=SMS_TIMEOUT)

    def admin_overview(self):
        return self._call("GET", "/admin/overview")

    def list_requests(self, status: str = ""):
        return self._call("GET", "/admin/requests", query={"status": status})

    def approve(self, request_id):  # may text a welcome message
        return self._call("POST", f"/admin/requests/{urllib.parse.quote(request_id)}/approve", timeout=SMS_TIMEOUT)

    def reissue_code(self, request_id):
        return self._call("POST", f"/admin/requests/{urllib.parse.quote(request_id)}/reissue-code")

    def reject(self, request_id, reason):
        return self._call("POST", f"/admin/requests/{urllib.parse.quote(request_id)}/reject", {"reason": reason})

    def list_users(self):
        return self._call("GET", "/admin/users")

    def set_user_active(self, uid, active: bool):
        return self._call("POST", f"/admin/users/{urllib.parse.quote(uid)}/status", {"active": active})

    def reports_summary(self, days: int = 30):
        return self._call("GET", "/admin/reports/summary", query={"days": days})

    def list_reports(self, status: str = "", category: str = ""):
        return self._call("GET", "/admin/reports", query={"status": status, "category": category})

    def create_report(self, **report):
        return self._call("POST", "/admin/reports", report)

    def update_report(self, report_id: str, status: str = "", note: str = ""):
        return self._call("POST", f"/admin/reports/{urllib.parse.quote(report_id)}/update",
                          {"status": status, "note": note})

    def list_invites(self):
        return self._call("GET", "/admin/invites")

    def create_invite(self, email: str, hours: int):
        return self._call("POST", "/admin/invites", {"email": email, "hours": hours})

    def revoke_invite(self, invite_id):
        return self._call("POST", f"/admin/invites/{urllib.parse.quote(invite_id)}/revoke")

    def audit(self, limit: int = 200):
        return self._call("GET", "/admin/audit", query={"limit": limit})

    def janitor_overview(self):
        return self._call("GET", "/janitor/overview")
