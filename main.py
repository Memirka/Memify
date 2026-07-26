"""Memify Player — entry point with authentication."""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication, QWidget, QStackedLayout
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPalette, QColor

from config import APP_ICON, DATA_DIR
from core.account import AccountManager
from ui.auth_widget import AuthWidget
from ui.splash_screen import SplashScreen
import ui.styles as styles_module


def _apply_dark_palette(app: QApplication):
    c = styles_module.COLORS
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c["BACKGROUND"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(c["SURFACE"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c["SURFACE"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["SURFACE_LIGHT"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(c["SURFACE"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c["PRIMARY"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    app.setPalette(palette)


class AppWindow(QWidget):
    def __init__(self, account_manager: AccountManager):
        super().__init__()
        self.setWindowTitle("Memify")
        self.resize(1280, 800)

        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))

        self._account_manager = account_manager
        self._main_widget = None
        self._main_ready = False
        self._splash_done = False
        self._auth_pending = False
        self._splash = None

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._auth_widget = AuthWidget(self._account_manager, parent=self)
        self._auth_widget.authenticated.connect(self._on_authenticated)
        self._stack.addWidget(self._auth_widget)

        self._show_splash()

        if self._account_manager.activate_saved_login():
            # Has saved credentials → build main right away
            self._build_main_widget()
        else:
            self._auth_pending = True

    # ── Splash ────────────────────────────────────────────────────────────────

    def _show_splash(self):
        if self._splash:
            try:
                self._stack.removeWidget(self._splash)
                self._splash.deleteLater()
            except Exception:
                pass

        self._splash_done = False
        self._splash = SplashScreen(
            icon_path=APP_ICON,
            duration_ms=700,
            hold_after_ms=300,
            as_window=False,
            parent=self,
        )
        self._stack.addWidget(self._splash)
        self._stack.setCurrentWidget(self._splash)
        self._splash.finished.connect(self._on_splash_finished)
        self._splash.start()

    def _on_splash_finished(self):
        self._splash_done = True
        if self._auth_pending and not self._main_ready:
            self._stack.setCurrentWidget(self._auth_widget)
            return
        self._maybe_show_main()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _on_authenticated(self, _data: dict):
        self._auth_pending = False
        self._build_main_widget()
        self._show_splash()

    # ── Main widget ───────────────────────────────────────────────────────────

    def _build_main_widget(self):
        if self._main_ready:
            return

        # Lazy import to keep startup fast
        from ui.main_window import MusicApp

        self._main_widget = MusicApp(account_manager=self._account_manager)
        self._main_widget.logout_requested.connect(self._on_logout)
        self._main_ready = True
        self._stack.addWidget(self._main_widget)
        self._maybe_show_main()

    def _maybe_show_main(self):
        if self._main_ready and self._splash_done and self._main_widget:
            self._stack.setCurrentWidget(self._main_widget)
            if self._splash:
                try:
                    self._splash.deleteLater()
                except Exception:
                    pass
                self._splash = None

    # ── Logout ────────────────────────────────────────────────────────────────

    def _on_logout(self):
        self._main_ready = False
        self._auth_pending = True

        if self._main_widget:
            try:
                # close() (not just deleteLater()) so MusicApp.closeEvent runs
                # and stops its background image-loading QThreads first —
                # otherwise deleteLater() can destroy a QThread while it's
                # still running and Qt aborts the process.
                self._main_widget.close()
            except Exception:
                pass
            try:
                self._stack.removeWidget(self._main_widget)
                self._main_widget.deleteLater()
            except Exception:
                pass
            self._main_widget = None

        try:
            self._auth_widget.login_input.clear()
            self._auth_widget.password_input.clear()
        except Exception:
            pass

        self._stack.setCurrentWidget(self._auth_widget)

    def closeEvent(self, event):
        if self._main_widget:
            try:
                self._main_widget.close()
            except Exception:
                pass
        super().closeEvent(event)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("Memify")
    app.setStyle("Fusion")

    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))

    _apply_dark_palette(app)

    account_manager = AccountManager()
    window = AppWindow(account_manager)
    window.show()

    exit_code = app.exec()
    # Force a deterministic cleanup pass now, while the Qt event loop and
    # C++ objects are still in a known-good state — left to CPython's own
    # (less predictable) interpreter-shutdown finalization order, a background
    # QThread that only just finished can still occasionally get torn down
    # a beat late and abort the process ("QThread: Destroyed while running").
    import gc
    gc.collect()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
