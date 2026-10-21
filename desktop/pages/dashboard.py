"""Arduino IoT Cloud dashboard shown automatically after sign-in.

Two ways to show it, chosen automatically:

1. Inside the AureoClean window (Qt WebEngine), when it is installed.
2. Otherwise in its own Chromium "app" window (no address bar or tabs),
   opened automatically at sign-in and closed again at sign-out.

Both use a separate browser profile kept on the Pi, so whoever signs in to
Arduino there once stays signed in, and it is kept apart from the admin's
normal Chromium.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QProcess, Qt, QTimer, QUrl
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from ..widgets import button, card, label
from .common import BROWSERS, _make_room, open_in_browser

PROFILE_ROOT = Path(os.environ.get("AUREOCLEAN_DATA_DIR", "~/.local/share/aureoclean")).expanduser() / "browser"

# Pages the embedded view may visit: Arduino Cloud and its sign-in pages.
ALLOWED_HOSTS = ("arduino.cc", "arduino.tips", "google.com", "gstatic.com", "apple.com", "github.com")

# How long a failed in-app load may last before switching to a Chromium window.
# (Arduino Cloud redirects to its sign-in page straight away, and Qt reports
# that interrupted first load as a "failure" even though the next one works.)
FALLBACK_AFTER_MS = 6000

try:  # Qt WebEngine is optional; not every Raspberry Pi install has it.
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    HAS_WEBENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    HAS_WEBENGINE = False


def embedding_enabled() -> bool:
    return HAS_WEBENGINE and os.environ.get("AUREOCLEAN_DASHBOARD_EMBED", "1").strip() != "0"


def _host_allowed(url: QUrl) -> bool:
    host = (url.host() or "").lower()
    if url.scheme() in ("about", "data", "blob"):
        return True
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


if HAS_WEBENGINE:

    class _LockedPage(QWebEnginePage):
        """Stays on Arduino Cloud: other sites are not opened inside the app."""

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802 (Qt name)
            if is_main_frame and not _host_allowed(url):
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

        def createWindow(self, _type):  # noqa: N802 - pop-ups open in the same view
            return self

    _PROFILES: dict[str, "QWebEngineProfile"] = {}

    def _profile(name: str) -> "QWebEngineProfile":
        if name not in _PROFILES:
            from PySide6.QtWidgets import QApplication

            profile = QWebEngineProfile(f"aureoclean-{name}", QApplication.instance())
            path = PROFILE_ROOT / f"embedded-{name}"
            path.mkdir(parents=True, exist_ok=True)
            profile.setPersistentStoragePath(str(path))
            profile.setCachePath(str(path / "cache"))
            profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
            _PROFILES[name] = profile
        return _PROFILES[name]


class DashboardPanel(QWidget):
    """Shows the monitoring dashboard. Call ``show_url`` after sign-in and ``leave`` at sign-out."""

    def __init__(self, profile_name: str):
        super().__init__()
        self.profile_name = profile_name
        self.url = ""
        self.window_proc: QProcess | None = None
        self.fell_back = False  # embedded view gave up this session -> use the Chromium window
        self._fail_timer = QTimer(self)
        self._fail_timer.setSingleShot(True)
        self._fail_timer.timeout.connect(self._embedded_failed)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.addWidget(label("Live restroom monitoring", "Section", wrap=False))
        self.status = label("", "Muted", wrap=False)
        bar.addWidget(self.status)
        bar.addStretch(1)
        self.reload_btn = button("Reload", on_click=self.reload)
        self.window_btn = button("Open in separate window", on_click=self.open_window)
        bar.addWidget(self.reload_btn)
        bar.addWidget(self.window_btn)
        root.addLayout(bar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Page 0: message card (not configured / opened in its own window / error)
        msg_holder = QWidget()
        ml = QVBoxLayout(msg_holder)
        ml.setContentsMargins(0, 0, 0, 0)
        self.msg_frame, mb = card()
        self.msg_title = label("", "Section")
        self.msg_body = label("", "Muted")
        self.msg_action = button("Show dashboard", "Primary", self.open_window)
        mb.addWidget(self.msg_title)
        mb.addWidget(self.msg_body)
        mb.addWidget(self.msg_action, 0, Qt.AlignLeft)
        ml.addWidget(self.msg_frame)
        ml.addStretch(1)
        self.stack.addWidget(msg_holder)

        # Page 1: embedded web view
        self.view = None
        if embedding_enabled():
            self.view = QWebEngineView()
            self.view.setPage(_LockedPage(_profile(profile_name), self.view))
            self.view.loadStarted.connect(lambda: self.status.setText("Loading…"))
            self.view.loadFinished.connect(self._load_finished)
            self.stack.addWidget(self.view)

    # ------------------------------------------------------------------ public
    def show_url(self, url: str) -> None:
        """Called after sign-in with the configured dashboard link (may be empty)."""
        url = (url or "").strip()
        self.url = url
        has_url = bool(url) and urlparse(url).scheme in ("http", "https")
        self.reload_btn.setVisible(has_url and self.view is not None)
        self.window_btn.setVisible(has_url)

        if not has_url:
            self._message(
                "Dashboard not set up yet",
                "The Arduino IoT Cloud dashboard link hasn't been added. An administrator puts it in "
                "~/.config/aureoclean/aureoclean.env as AUREOCLEAN_DASHBOARD_URL=… and restarts the app.",
                action=False,
            )
            return

        if self.view is not None and not self.fell_back:
            self.stack.setCurrentWidget(self.view)
            if self.view.url().toString() != url:
                self.view.load(QUrl(url))
            return

        # No embedded browser (or it didn't work): open the dashboard in its own window right away.
        self._show_in_window()

    def _show_in_window(self) -> None:
        self.reload_btn.setVisible(False)
        if self.open_window():
            self._message(
                "Dashboard opened in its own window",
                "The live dashboard is in a separate window. If you closed it or it's hidden "
                "behind this one, press the button below.",
            )

    def leave(self) -> None:
        """Called at sign-out: stop showing the dashboard."""
        self._fail_timer.stop()
        if self.view is not None:
            self.view.stop()
            self.view.setUrl(QUrl("about:blank"))
        if self.window_proc is not None and self.window_proc.state() != QProcess.NotRunning:
            self.window_proc.terminate()
            if not self.window_proc.waitForFinished(2000):
                self.window_proc.kill()
        self.window_proc = None
        self.status.setText("")

    # ------------------------------------------------------------------ internals
    def reload(self) -> None:
        if self.view is not None and self.url:
            self.view.load(QUrl(self.url))

    def _load_finished(self, ok: bool) -> None:
        if ok:
            self._fail_timer.stop()
            self.status.setText("")
        elif not self._fail_timer.isActive():
            # Often just a redirect (e.g. to Arduino's sign-in page). Give the next load a chance.
            self._fail_timer.start(FALLBACK_AFTER_MS)

    def _embedded_failed(self) -> None:
        """The in-app view really didn't load: show the dashboard in a Chromium window instead."""
        if not self.url or self.fell_back:
            return
        self.fell_back = True
        if self.view is not None:
            self.view.stop()
        self.status.setText("")
        self._show_in_window()

    def _message(self, title: str, body: str, action: bool = True) -> None:
        self.msg_title.setText(title)
        self.msg_body.setText(body)
        self.msg_action.setVisible(action)
        self.stack.setCurrentIndex(0)

    def open_window(self) -> bool:
        """Open the dashboard in a Chromium app window (its own profile). Returns True on success."""
        if not self.url:
            return False
        # If it's already open, launching again with the same profile just brings it to the front.
        chromium = next((shutil.which(b) for b in BROWSERS[:2] if shutil.which(b)), None)
        if chromium is None:
            # No Chromium: fall back to whatever browser exists.
            open_in_browser(self.url, self)
            return True
        profile_dir = PROFILE_ROOT / f"window-{self.profile_name}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        proc = QProcess(self)
        proc.setProgram(chromium)
        proc.setArguments([
            f"--app={self.url}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Vulkan",
            "--start-maximized",
        ])
        proc.start()
        if not proc.waitForStarted(5000):
            self._message("Couldn't open the dashboard",
                          f"Chromium didn't start. Open Chromium and go to:\n{self.url}")
            return False
        self.window_proc = proc
        if self.window().isFullScreen():
            _make_room(self)  # a full-screen AureoClean window would cover the dashboard
        return True
