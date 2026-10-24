"""AureoClean desktop app (PySide6).

Run from the project folder:  python -m desktop.app [--fullscreen | --maximized | --kiosk]
The backend must already be running (run_local.sh starts both).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The Raspberry Pi 4's graphics driver has no Vulkan support. Tell Qt and the
# embedded dashboard browser to use OpenGL straight away, which avoids the
# harmless "vkDebug: ... VK_ERROR_INCOMPATIBLE_DRIVER" warning. Must be set
# before Qt is imported.
os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-vulkan --disable-features=Vulkan")

import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QLineEdit, QMainWindow, QScrollArea, QStackedWidget

from .api import ApiClient, ApiError
from .pages.admin import AdminDashboard
from .pages.dashboard import DashboardPanel
from .pages.auth import ActivatePage, AdminRegisterPage, JanitorRequestPage, LoginPage, StatusPage
from .pages.janitor import JanitorDashboard
from .pages.lock import LockPage
from .theme import STYLESHEET
from .widgets import run_async


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AureoClean")
        self.setMinimumSize(640, 400)
        self.resize(800, 480)  # the project's 7-inch display
        self.api = ApiClient()
        self.user = None
        # Kiosk mode (tools/kiosk.sh): the window can't be closed by accident; only
        # Tools → "Close AureoClean" quits, and anything else (a crash) is reopened.
        self.kiosk = "--kiosk" in sys.argv
        self.allow_close = not self.kiosk

        # One Arduino dashboard for the whole app, shown in both the admin and janitor screens.
        self.dashboard = DashboardPanel()
        self.stack = QStackedWidget()
        self.stack.setObjectName("Root")
        self.setCentralWidget(self.stack)
        self.pages = {
            "login": LoginPage(self),
            "janitor_request": JanitorRequestPage(self),
            "activate": ActivatePage(self),
            "admin_register": AdminRegisterPage(self),
            "status": StatusPage(self),
            "admin": AdminDashboard(self),
            "janitor": JanitorDashboard(self),
            "lock": LockPage(self),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.show_login()
        # Idle lock: any touch, click or key press counts as "in use".
        self.last_activity = time.monotonic()
        self._idle = QTimer(self)
        self._idle.timeout.connect(self._check_idle)
        self._idle.start(15_000)
        startup_error = os.environ.pop("AUREOCLEAN_STARTUP_ERROR", "").strip()
        if startup_error:
            self.pages["login"].message.error(
                "The AureoClean service could not start. Details (also in logs/backend.log):\n\n" + startup_error[-600:])

    # ------------------------------------------------------------ navigation
    def show_page(self, name: str):
        page = self.pages[name]
        if hasattr(page, "reset") and name != "login":
            page.reset()
        self.stack.setCurrentWidget(page)
        inputs = page.findChildren(QLineEdit)
        if inputs:
            inputs[0].setFocus()

    def show_login(self, identifier: str = "", note: str = ""):
        self.stack.setCurrentWidget(self.pages["login"])
        self.pages["login"].prefill(identifier, note)

    def signed_in(self, user: dict):
        self.user = user
        self.touched()
        name = "admin" if user["role"] == "admin" else "janitor"
        self.stack.setCurrentWidget(self.pages[name])
        self.pages[name].enter(user)

        def show(links):
            url = next((link["url"] for link in links if link.get("key") == "dashboard"), "")
            self.dashboard.set_url(url)

        run_async(self.api.links, show, lambda e: self.dashboard.set_url(""))

    def closeEvent(self, event):  # noqa: N802 (Qt name)
        if self.allow_close:
            event.accept()
        else:
            event.ignore()

    def quit_app(self):
        """Close on purpose (exit code 0 tells kiosk.sh not to reopen it)."""
        self.allow_close = True
        QApplication.instance().exit(0)

    # ------------------------------------------------------------ idle lock
    def touched(self):
        self.last_activity = time.monotonic()

    def _check_idle(self):
        minutes = int((self.user or {}).get("idleLockMinutes") or 0)
        if not (self.user and minutes and self.api.token):
            return
        if self.stack.currentWidget() is self.pages["lock"]:
            return
        if time.monotonic() - self.last_activity >= minutes * 60:
            self.lock_screen(minutes)

    def lock_screen(self, minutes: int = 0):
        if not self.user:
            return
        for _ in range(5):  # close any open pop-up so it isn't left above the lock screen
            modal = QApplication.activeModalWidget()
            if modal is None:
                break
            modal.close()
        back = self.stack.currentWidget()
        self.stack.setCurrentWidget(self.pages["lock"])
        self.pages["lock"].lock(back, self.user, minutes)

    def unlocked(self, back_to):
        self.touched()
        self.stack.setCurrentWidget(back_to or self.pages["admin" if self.user["role"] == "admin" else "janitor"])

    def logout(self):
        # The dashboard stays loaded (and signed in to Arduino) for the next person.
        def done(*_):
            self.user = None
            self.show_login()

        run_async(self.api.logout, done, done)

    def handle_session_error(self, error: ApiError) -> bool:
        """Send the user back to sign-in if their session ended. Returns True if handled."""
        if error.status != 401:
            return False
        self.api.token = None
        self.user = None
        self.show_login()
        self.pages["login"].message.error(error.message)
        return True


def keep_focus_visible(old, new):
    """When the on-screen keyboard or a small screen hides a field,
    scroll so the focused input stays visible."""
    if new is None:
        return
    parent = new.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea):
            QTimer.singleShot(50, lambda a=parent, w=new: a.ensureWidgetVisible(w, 20, 80))
            return
        if isinstance(parent, QAbstractScrollArea):
            return
        parent = parent.parentWidget()


class ActivityFilter(QObject):
    """Sees every input event in the app (including the embedded dashboard)."""

    KINDS = {QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.TouchBegin, QEvent.Wheel, QEvent.TabletPress}

    def __init__(self, window):
        super().__init__()
        self.window = window

    def eventFilter(self, obj, event):  # noqa: N802 (Qt name)
        if event.type() in self.KINDS:
            self.window.touched()
        return False


def _log_crashes() -> None:
    """Write any error, or a hard crash, to logs/desktop-crash.log (the icon has no terminal)."""
    import faulthandler
    import traceback
    from datetime import datetime

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        crash_file = open(log_dir / "desktop-crash.log", "a", buffering=1)  # noqa: SIM115 (kept open)
    except OSError:
        return
    crash_file.write(f"\n=== AureoClean started {datetime.now():%Y-%m-%d %H:%M:%S} (args: {' '.join(sys.argv[1:])})\n")
    faulthandler.enable(crash_file)  # a real crash (segfault/abort) still gets a stack trace

    def hook(kind, value, tb):
        crash_file.write(f"--- error at {datetime.now():%H:%M:%S}\n")
        traceback.print_exception(kind, value, tb, file=crash_file)
        sys.__excepthook__(kind, value, tb)

    sys.excepthook = hook


def main() -> int:
    _log_crashes()
    # Needed by the embedded dashboard (Qt WebEngine); must be set before the app starts.
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    # Our own options (--kiosk, --fullscreen, --maximized) stay ours: Qt hands its arguments
    # to the dashboard's built-in Chromium, which has a --kiosk mode of its own.
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AureoClean")
    app.setDesktopFileName("aureoclean")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "icon-256.png")))
    app.setStyleSheet(STYLESHEET)
    app.focusChanged.connect(keep_focus_visible)
    # Only one AureoClean window at a time: two copies fight over the dashboard's
    # browser profile, and the second one crashes when the dashboard loads.
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QMessageBox
    lock_path = Path.home() / ".config" / "aureoclean" / "desktop.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(lock_path))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(2000):
        QMessageBox.information(None, "AureoClean", "AureoClean is already open on this Pi.")
        return 3  # not 0: kiosk mode tries again later instead of stopping
    window = MainWindow()
    activity = ActivityFilter(window)
    app.installEventFilter(activity)
    if "--fullscreen" in sys.argv or window.kiosk:
        window.showFullScreen()
    elif "--maximized" in sys.argv:
        window.showMaximized()
    else:
        window.show()
    code = app.exec()
    print(f"AureoClean closed (exit code {code}).", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
