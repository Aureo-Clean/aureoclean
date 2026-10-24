"""Arduino IoT Cloud dashboard, built into the app for admins and janitors.

There is ONE dashboard view for the whole app. The admin and janitor screens
both show it in their "Dashboard" tab (the widget is moved between them), so
the page stays loaded and the Arduino sign-in is shared.

Staying signed in: Arduino's sign-in cookie is a "session" cookie, which a
normal browser deletes when it closes. Here it is forced to be saved to disk
(ForcePersistentCookies), so you sign in to Arduino once and it survives
signing out of AureoClean and restarting the Pi.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QSettings, QTimer, QUrl
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from ..widgets import button, card, label

PROFILE_DIR = Path(os.environ.get("AUREOCLEAN_DATA_DIR", "~/.local/share/aureoclean")).expanduser() / "browser" / "dashboard"

# Pages the view may open: Arduino Cloud and the sign-in providers it uses.
ALLOWED_HOSTS = ("arduino.cc", "arduino.tips", "google.com", "gstatic.com", "googleusercontent.com",
                 "apple.com", "github.com")

# Qt reports the first load as failed when Arduino immediately redirects to
# its sign-in page; only complain if nothing has loaded after this long.
FAIL_AFTER_MS = 8000
ZOOM_STEPS = (0.5, 0.6, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25)

# Google refuses to sign in inside embedded web views ("This browser or app may
# not be secure"). Like the qutebrowser browser (also built on Qt WebEngine),
# present Firefox's identity on Google's sign-in pages only. Everything else
# sees the normal engine.
GOOGLE_SIGNIN_HOSTS = ("accounts.google.com", "accounts.youtube.com")
FIREFOX_UA = "Mozilla/5.0 (X11; Linux aarch64; rv:140.0) Gecko/20100101 Firefox/140.0"
_NAVIGATOR_QUIRK = """
(function () {
  if (!%(hosts)s.includes(location.hostname)) return;
  var ua = %(ua)s;
  var props = {userAgent: ua, appVersion: "5.0 (X11)", vendor: "", platform: "Linux aarch64", userAgentData: undefined};
  Object.keys(props).forEach(function (k) {
    try { Object.defineProperty(Navigator.prototype, k, {get: function () { return props[k]; }, configurable: true}); } catch (e) {}
  });
})();
"""

try:  # Qt WebEngine is installed by setup_pi.sh when the Pi supports it.
    from PySide6.QtWebEngineCore import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineScript,
        QWebEngineUrlRequestInterceptor,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView

    HAS_WEBENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    HAS_WEBENGINE = False


def _host_allowed(url: QUrl) -> bool:
    if url.scheme() in ("about", "data", "blob"):
        return True
    host = (url.host() or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


if HAS_WEBENGINE:

    class _DashboardPage(QWebEnginePage):
        """Stays on Arduino Cloud; pop-ups (e.g. sign-in) open in the same view."""

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802 (Qt name)
            if is_main_frame and not _host_allowed(url):
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

        def createWindow(self, _type):  # noqa: N802 (Qt name)
            return self

    class _GoogleSignInQuirk(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):  # noqa: N802 (Qt name)
            if info.requestUrl().host().lower() in GOOGLE_SIGNIN_HOSTS:
                info.setHttpHeader(b"User-Agent", FIREFOX_UA.encode())

    def _make_profile(parent) -> "QWebEngineProfile":
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        profile = QWebEngineProfile("aureoclean-dashboard", parent)
        profile.setPersistentStoragePath(str(PROFILE_DIR))
        profile.setCachePath(str(PROFILE_DIR / "cache"))
        # Keep Arduino's sign-in even though it is a session cookie.
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        # Look like a normal Chrome to Arduino Cloud...
        profile.setHttpUserAgent(re.sub(r"\s*QtWebEngine/\S+", "", profile.httpUserAgent()))
        # ...and like Firefox on Google's sign-in pages (header + JavaScript).
        profile._quirk = _GoogleSignInQuirk(profile)  # keep a reference
        profile.setUrlRequestInterceptor(profile._quirk)
        script = QWebEngineScript()
        script.setName("google-signin-quirk")
        script.setSourceCode(_NAVIGATOR_QUIRK % {"hosts": json.dumps(list(GOOGLE_SIGNIN_HOSTS)),
                                                  "ua": json.dumps(FIREFOX_UA)})
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        profile.scripts().insert(script)
        return profile


class DashboardPanel(QWidget):
    """The single, shared dashboard view. Create once; call ``set_url`` and ``attach_to``."""

    def __init__(self):
        super().__init__()
        self.url = ""
        self.settings = QSettings("AureoClean", "desktop")

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.status = label("", "Muted", wrap=False)
        bar.addWidget(self.status, 1)
        self.zoom_out = button("A−", "Small", lambda: self._zoom(-1))
        self.zoom_in = button("A+", "Small", lambda: self._zoom(+1))
        self.reload_btn = button("⟳ Reload", "Small", self.reload)
        for b in (self.zoom_out, self.zoom_in, self.reload_btn):
            bar.addWidget(b)
        root.addLayout(bar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Page 0: message (not set up / viewer missing)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        frame, body = card()
        self.msg_title = label("", "Section")
        self.msg_body = label("", "Muted")
        body.addWidget(self.msg_title)
        body.addWidget(self.msg_body)
        hl.addWidget(frame)
        hl.addStretch(1)
        self.stack.addWidget(holder)

        self.view = None
        self._fail_timer = QTimer(self)
        self._fail_timer.setSingleShot(True)
        self._fail_timer.timeout.connect(self._really_failed)
        # Safe mode: AUREOCLEAN_SAFE_MODE=1 ./run_local.sh starts without the embedded
        # browser, so the rest of the app works even if the dashboard viewer crashes.
        self.safe_mode = os.environ.get("AUREOCLEAN_SAFE_MODE") == "1"
        if HAS_WEBENGINE and not self.safe_mode:
            self.profile = _make_profile(self)
            self.view = QWebEngineView()
            page = _DashboardPage(self.profile, self.view)
            self.view.setPage(page)
            self.view.loadStarted.connect(lambda: self.status.setText("Loading…"))
            self.view.loadFinished.connect(self._load_finished)
            page.renderProcessTerminated.connect(lambda *_: QTimer.singleShot(1000, self.reload))
            self.view.setZoomFactor(self._saved_zoom())
            self.stack.addWidget(self.view)

    # ------------------------------------------------------------------ public
    def attach_to(self, container: QWidget) -> None:
        """Show the shared dashboard inside ``container`` (a tab of the admin or janitor screen)."""
        if self.parentWidget() is not container:
            old = self.parentWidget()
            if old is not None and old.layout() is not None:
                old.layout().removeWidget(self)
            container.layout().addWidget(self)
        self.show()

    def set_url(self, url: str) -> None:
        url = (url or "").strip()
        valid = bool(url) and urlparse(url).scheme in ("http", "https")
        for b in (self.zoom_out, self.zoom_in, self.reload_btn):
            b.setVisible(valid and self.view is not None)

        if not valid:
            self.url = ""
            self._message("Dashboard not set up yet",
                          "An administrator adds the Arduino IoT Cloud dashboard link to "
                          "~/.config/aureoclean/aureoclean.env as AUREOCLEAN_DASHBOARD_URL=… and restarts the app.")
            return
        if self.view is None and self.safe_mode:
            self.url = url
            self._message("Dashboard paused (safe mode)",
                          "AureoClean was started without the built-in dashboard (safe mode). Everything else "
                          "works. Start it normally to bring the dashboard back.")
            return
        if self.view is None:
            self.url = url
            self._message("Dashboard viewer not installed",
                          "The in-app web viewer (Qt WebEngine) is missing on this Pi. "
                          "Run ./setup_pi.sh once to install it, then restart AureoClean.")
            return

        self.stack.setCurrentWidget(self.view)
        if url != self.url or self.view.url().isEmpty():
            self.url = url
            self.view.load(QUrl(url))

    def reload(self) -> None:
        if self.view is not None and self.url:
            self.view.load(QUrl(self.url))

    # ------------------------------------------------------------------ internals
    def _message(self, title: str, body: str) -> None:
        self.msg_title.setText(title)
        self.msg_body.setText(body)
        self.status.setText("")
        self.stack.setCurrentIndex(0)

    def _load_finished(self, ok: bool) -> None:
        if ok:
            self._fail_timer.stop()
            self.status.setText("")
        elif not self._fail_timer.isActive():
            self._fail_timer.start(FAIL_AFTER_MS)

    def _really_failed(self) -> None:
        self.status.setText("Couldn't load the dashboard. Check the Wi-Fi, then press Reload.")

    def _saved_zoom(self) -> float:
        try:
            return float(self.settings.value("dashboardZoom", 0.8))
        except (TypeError, ValueError):
            return 0.8

    def _zoom(self, direction: int) -> None:
        if self.view is None:
            return
        current = self.view.zoomFactor()
        index = min(range(len(ZOOM_STEPS)), key=lambda i: abs(ZOOM_STEPS[i] - current))
        index = max(0, min(len(ZOOM_STEPS) - 1, index + direction))
        self.view.setZoomFactor(ZOOM_STEPS[index])
        self.settings.setValue("dashboardZoom", ZOOM_STEPS[index])
