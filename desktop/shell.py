"""AureoClean on the Pi screen: a window that shows the AureoClean web app.

The screens themselves are the web app in ``webapp/`` (the same pages the Wi-Fi
website serves), so the Pi and phones always look and work the same. This
window adds what only the Pi can do:

* Arduino's own dashboard, as its own full-screen page (native viewer, stays
  signed in to Arduino) — the app's Live sensors view shows the same pages without it;
* kiosk mode (full screen, can't be closed by accident; the idle lock is in the
  web app);
* saving Excel downloads and PDFs to ~/Documents;
* opening outside links in Chrome.

The web app talks to this window by writing ``AUREO:{json}`` to the console,
and the window answers through ``window.AureoShell.receive({...})``.

Run:  python -m desktop.shell [--maximized | --fullscreen]
      python -m desktop.shell --classic      (the previous Qt screens)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-vulkan --disable-features=Vulkan")

PORT = int(os.environ.get("AUREOCLEAN_PORT", "8765") or 8765)
UI_URL = f"http://127.0.0.1:{PORT}/?shell=pi"
DOCUMENTS = Path.home() / "Documents"


def _webengine_available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> int:
    if "--classic" in sys.argv or not _webengine_available():
        # The previous Qt screens: also used when this Pi has no web viewer installed.
        from .app import main as classic_main
        return classic_main()

    from .app import _log_crashes
    _log_crashes()

    from PySide6.QtCore import QCoreApplication, QLockFile, QMarginsF, Qt, QTimer, QUrl
    from PySide6.QtGui import QIcon, QPageLayout, QPageSize
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

    from .pages.common import open_in_browser
    from .pages.dashboard import DashboardPanel
    from .theme import STYLESHEET
    from .widgets import button

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])  # our own options never reach the built-in Chromium
    app.setApplicationName("AureoClean")
    app.setDesktopFileName("aureoclean")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "icon-256.png")))
    app.setStyleSheet(STYLESHEET)

    lock_path = Path.home() / ".config" / "aureoclean" / "desktop.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    instance = QLockFile(str(lock_path))
    instance.setStaleLockTime(0)
    if not instance.tryLock(2000):
        QMessageBox.information(None, "AureoClean", "AureoClean is already open on this Pi.")
        return 3

    class UiPage(QWebEnginePage):
        """The web app. Listens for AUREO: messages; keeps navigation on AureoClean."""

        def __init__(self, profile, parent, window):
            super().__init__(profile, parent)
            self.window_ = window

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 (Qt name)
            if message.startswith("AUREO:"):
                try:
                    self.window_.on_message(json.loads(message[6:]))
                except (ValueError, TypeError):
                    pass

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802 (Qt name)
            if url.host() in ("127.0.0.1", "localhost") or url.scheme() in ("blob", "data", "about"):
                return True
            if url.scheme() in ("http", "https"):
                self.window_.open_external(url.toString())
            return False

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("AureoClean")
            self.setMinimumSize(640, 400)
            self.resize(800, 480)
            self.kiosk = False
            self._retry = 0

            self.stack = QStackedWidget()
            self.setCentralWidget(self.stack)

            # Page 0: the AureoClean screens.
            # Nothing kept on disk: a sign-in lasts while the app runs. The profile belongs to
            # the application so it outlives the page when the window closes.
            self.profile = QWebEngineProfile(QApplication.instance())
            self.profile.downloadRequested.connect(self.on_download)
            self.view = QWebEngineView()
            self.page_ = UiPage(self.profile, self.view, self)
            self.view.setPage(self.page_)
            self.view.loadFinished.connect(self.on_loaded)
            self.page_.renderProcessTerminated.connect(lambda *_: QTimer.singleShot(1000, self.load_ui))
            self.page_.pdfPrintingFinished.connect(self.on_pdf)
            self.stack.addWidget(self.view)

            # Page 1: Arduino's own dashboard (the app's Live sensors view is the main one), with a way back.
            sensors = QWidget()
            col = QVBoxLayout(sensors)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            # A QFrame (not a plain QWidget) so the navy "TopBar" style paints its background;
            # otherwise the white back button and title disappear on a white page.
            bar = QFrame()
            bar.setObjectName("TopBar")
            bar.setAttribute(Qt.WA_StyledBackground, True)
            bar.setFixedHeight(46)
            bar.setStyleSheet(
                "QFrame#TopBar { background: #14336E; border: none; }"
                "QFrame#TopBar QLabel { color: white; font-size: 15px; font-weight: 700; background: transparent; }"
                "QFrame#TopBar QPushButton { color: white; background: rgba(255,255,255,0.14); font-weight: 600;"
                " border: 1px solid rgba(255,255,255,0.6); border-radius: 8px; padding: 4px 14px; }"
                "QFrame#TopBar QPushButton:pressed { background: rgba(255,255,255,0.28); }")
            row = QHBoxLayout(bar)
            row.setContentsMargins(10, 6, 12, 6)
            row.setSpacing(12)
            back = button("‹  Back to AureoClean", "TopButton", self.show_ui)
            back.setMinimumHeight(34)
            row.addWidget(back)
            title = QLabel("Arduino Cloud dashboard")
            title.setObjectName("TopTitle")
            row.addWidget(title)
            row.addStretch(1)
            col.addWidget(bar)
            self.dashboard = DashboardPanel()
            col.addWidget(self.dashboard, 1)
            self.stack.addWidget(sensors)

            # Shown while the AureoClean service is starting.
            self.waiting = QLabel("Starting AureoClean…")
            self.waiting.setAlignment(Qt.AlignCenter)
            self.waiting.setWordWrap(True)
            self.waiting.setStyleSheet("font-size:16px; color:#5B6B80; background:#F3F6FA; padding:24px;")
            self.stack.addWidget(self.waiting)
            self.stack.setCurrentWidget(self.waiting)
            error = os.environ.pop("AUREOCLEAN_STARTUP_ERROR", "").strip()
            if error:
                self.waiting.setText("The AureoClean service could not start. Details (also in logs/backend.log):\n\n"
                                     + error[-600:])
            self.load_ui()

        # ---------------------------------------------------------------- pages
        def load_ui(self):
            self.view.load(QUrl(UI_URL))

        def on_loaded(self, ok: bool):
            if ok:
                self._retry = 0
                self.stack.setCurrentWidget(self.view)
                return
            # The service isn't answering yet (still starting): try again shortly.
            self._retry += 1
            if not self.waiting.text().startswith("The AureoClean service could not start"):
                self.waiting.setText("Starting AureoClean…" if self._retry < 15 else
                                     "Waiting for the AureoClean service… If this stays, restart the Pi or run "
                                     "Update AureoClean.")
            self.stack.setCurrentWidget(self.waiting)
            QTimer.singleShot(2000, self.load_ui)

        def show_ui(self):
            self.stack.setCurrentWidget(self.view)

        def show_dashboard(self):
            from backend.config import load_settings
            try:
                url = load_settings().dashboard_url
            except Exception:  # noqa: BLE001
                url = ""
            self.dashboard.set_url(url)
            self.stack.setCurrentIndex(1)

        # ---------------------------------------------------------------- bridge
        def tell(self, msg: dict):
            self.page_.runJavaScript(f"window.AureoShell && window.AureoShell.receive({json.dumps(msg)});")

        def toast(self, text: str, kind: str = ""):
            self.tell({"event": "toast", "text": text, "kind": kind})

        def on_message(self, msg: dict):
            action = msg.get("action")
            if action == "ready":
                self.tell({"event": "kiosk", "on": self.kiosk})
            elif action == "kiosk":
                self.set_kiosk(bool(msg.get("on")))
            elif action == "dashboard":
                self.show_dashboard()
            elif action == "open":
                self.open_external(str(msg.get("url", "")))
            elif action == "pdf":
                name = Path(str(msg.get("name") or "AureoClean.pdf")).name
                DOCUMENTS.mkdir(parents=True, exist_ok=True)
                layout = QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                     QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
                self.page_.printToPdf(str(DOCUMENTS / name), layout)

        def open_external(self, url: str):
            if url.startswith(("http://", "https://")):
                if self.kiosk:
                    self.toast("Leave kiosk mode to open other websites.", "error")
                    return
                open_in_browser(url, self)

        def set_kiosk(self, on: bool):
            self.kiosk = on
            if on:
                self.showFullScreen()
            else:
                self.showNormal()
                self.showMaximized()
            self.tell({"event": "kiosk", "on": on})

        def closeEvent(self, event):  # noqa: N802 (Qt name)
            if self.kiosk:
                event.ignore()
                self.toast("Kiosk mode is on. Turn it off in System, or with \"Exit kiosk mode\" on the sign-in screen.")
            else:
                event.accept()

        # ---------------------------------------------------------------- files
        def on_download(self, download):
            DOCUMENTS.mkdir(parents=True, exist_ok=True)
            name = Path(download.downloadFileName() or f"AureoClean-{datetime.now():%Y%m%d-%H%M}").name
            target = DOCUMENTS / name
            if target.exists():
                target = DOCUMENTS / f"{target.stem}-{int(time.time()) % 100000}{target.suffix}"
            download.setDownloadDirectory(str(DOCUMENTS))
            download.setDownloadFileName(target.name)

            def finished():
                if download.isFinished():
                    ok = download.state() == download.DownloadState.DownloadCompleted
                    self.toast(f"Saved to Documents/{target.name}" if ok else "Couldn't save the file.", "success" if ok else "error")
            download.isFinishedChanged.connect(finished)
            download.accept()

        def on_pdf(self, path: str, ok: bool):
            self.toast(f"Saved to Documents/{Path(path).name}" if ok else "Couldn't make the PDF.", "success" if ok else "error")

    window = Window()
    if "--fullscreen" in sys.argv:
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
