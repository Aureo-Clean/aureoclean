"""AureoClean desktop app (PySide6).

Run from the project folder:  python -m desktop.app [--fullscreen]
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

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QLineEdit, QMainWindow, QScrollArea, QStackedWidget

from .api import ApiClient, ApiError
from .pages.admin import AdminDashboard
from .pages.dashboard import DashboardPanel
from .pages.auth import ActivatePage, AdminRegisterPage, JanitorRequestPage, LoginPage, StatusPage
from .pages.janitor import JanitorDashboard
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
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.show_login()
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
        name = "admin" if user["role"] == "admin" else "janitor"
        self.stack.setCurrentWidget(self.pages[name])
        self.pages[name].enter(user)

        def show(links):
            url = next((link["url"] for link in links if link.get("key") == "dashboard"), "")
            self.dashboard.set_url(url)

        run_async(self.api.links, show, lambda e: self.dashboard.set_url(""))

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


def main() -> int:
    # Needed by the embedded dashboard (Qt WebEngine); must be set before the app starts.
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("AureoClean")
    app.setDesktopFileName("aureoclean")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "icon.svg")))
    app.setStyleSheet(STYLESHEET)
    app.focusChanged.connect(keep_focus_visible)
    window = MainWindow()
    if "--fullscreen" in sys.argv:
        window.showFullScreen()
    elif "--maximized" in sys.argv:
        window.showMaximized()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
