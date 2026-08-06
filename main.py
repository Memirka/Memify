"""Memify Player — entry point with authentication."""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))

from config import APP_ICON, DATA_DIR, APP_SETTINGS_FILE, SERVER_URL, APP_VERSION


def _load_saved_ui_scale() -> float:
    """Read the saved UI scale directly from disk, before QApplication exists —
    Qt only picks up QT_SCALE_FACTOR if it's set before the app is created."""
    try:
        if os.path.exists(APP_SETTINGS_FILE):
            with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            scale = float(data.get("ui_scale", 1.0))
            if 0.5 <= scale <= 3.0:
                return scale
    except Exception:
        pass
    return 1.0


_saved_scale = _load_saved_ui_scale()
if _saved_scale != 1.0:
    os.environ["QT_SCALE_FACTOR"] = str(_saved_scale)

from utils.desktop_entry import install_desktop_entry

install_desktop_entry()

from PyQt6.QtWidgets import QApplication, QWidget, QStackedLayout, QProgressDialog, QMessageBox
from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtGui import QIcon, QGuiApplication

# Qt6's default high-DPI rounding policy snaps a manual QT_SCALE_FACTOR like
# 1.25/1.5 to the nearest "clean" factor, which is exactly why non-100% presets
# can look subtly off (uneven padding, slightly wrong sizes) while 100% looks
# fine. PassThrough uses the requested factor exactly, with no rounding.
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)

from core.account import AccountManager
from ui.auth_widget import AuthWidget
from ui.splash_screen import SplashScreen
import ui.styles as styles_module


def _apply_dark_palette(app: QApplication):
    styles_module.apply_palette(app)


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
        self._update_check_thread = None
        self._update_check_worker = None
        self._update_download_thread = None
        self._update_download_worker = None

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._auth_widget = AuthWidget(self._account_manager, parent=self)
        self._auth_widget.authenticated.connect(self._on_authenticated)
        self._stack.addWidget(self._auth_widget)

        self._show_splash()

        if self._account_manager.activate_saved_login():
            # Has saved credentials → build main right away
            self._build_main_widget()
        elif self._account_manager.last_error == "network_error":
            # Saved credentials exist but verifying them against the server
            # failed with a connection error (not e.g. a wrong password) —
            # no point showing a login screen the user can't do anything
            # useful with; launch straight into offline mode instead.
            self._build_main_widget(offline=True)
        elif self._account_manager.is_server_reachable():
            self._auth_pending = True
        else:
            # No saved credentials at all, so activate_saved_login() never
            # even attempted a network request — a dedicated reachability
            # check is the only way to tell "brand new install, offline"
            # apart from "brand new install, just needs to sign in".
            self._build_main_widget(offline=True)

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
        self._start_update_check()

    # ── Self-update ──────────────────────────────────────────────────────────

    def _start_update_check(self):
        from core.updater import is_frozen, find_pending_update, log as update_log

        if not is_frozen():
            self._after_update_check()
            return

        pending = find_pending_update()
        if pending:
            # A previous update was fully downloaded but never got swapped
            # in (e.g. the Windows helper script failed to run) — finish it
            # now instead of downloading the same build all over again.
            update_log(f"_start_update_check: retrying pending update: {pending}")
            self._apply_downloaded_update(pending)
            return

        from workers.update_check_worker import UpdateCheckWorker

        worker = UpdateCheckWorker(SERVER_URL, APP_VERSION)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)

        def on_done(info):
            thread.quit()
            if info:
                self._begin_update(info)
            else:
                self._after_update_check()

        worker.finished.connect(on_done)
        thread.started.connect(worker.run)
        thread.start()
        # Keep both alive — the worker isn't parented to anything, so without
        # a live Python reference it can get garbage-collected right after
        # this method returns, before the thread ever delivers `started`.
        self._update_check_thread = thread
        self._update_check_worker = worker

    def _begin_update(self, info: dict):
        from workers.download_worker import DownloadWorker
        from core.updater import download_dest_path, log as update_log

        download_url = (info or {}).get("download_url")
        if not download_url:
            self._after_update_check()
            return

        tmp_path = download_dest_path()
        update_log(f"_begin_update: downloading {download_url} -> {tmp_path}")

        progress = QProgressDialog("Загрузка обновления...", None, 0, 100, self)
        progress.setWindowTitle("Memify")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()

        tasks = [{"url": download_url, "path": tmp_path, "label": "Memify"}]
        worker = DownloadWorker(tasks)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)

        download_error = {"msg": ""}

        def on_progress(file_num, total_files, downloaded, total_bytes, label):
            if total_bytes > 0:
                progress.setValue(min(int(downloaded / total_bytes * 100), 99))

        def on_file_failed(path, error):
            download_error["msg"] = error
            update_log(f"_begin_update: download failed: {error}")

        def on_finished(success, failed, cancelled):
            progress.setValue(100)
            progress.close()
            thread.quit()
            if success and not failed:
                self._apply_downloaded_update(tmp_path)
            else:
                if not cancelled:
                    reason = download_error["msg"] or "неизвестная ошибка"
                    self._show_update_error(f"Не удалось скачать обновление: {reason}")
                self._after_update_check()

        worker.progress.connect(on_progress)
        worker.file_failed.connect(on_file_failed)
        worker.finished.connect(on_finished)
        thread.started.connect(worker.run)
        thread.start()
        self._update_download_thread = thread
        self._update_download_worker = worker

    def _apply_downloaded_update(self, file_path: str):
        from core.updater import apply_update_and_exit, _log_path

        ok, err = apply_update_and_exit(file_path)
        if ok:
            QApplication.instance().quit()
        else:
            self._show_update_error(f"Не удалось применить обновление: {err}\n\nЛог: {_log_path()}")
            self._after_update_check()

    def _after_update_check(self):
        if self._auth_pending and not self._main_ready:
            self._stack.setCurrentWidget(self._auth_widget)
            return
        self._maybe_show_main()

    def _show_update_error(self, text: str):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Memify")
        box.setText("Автообновление не удалось")
        box.setInformativeText(text)
        box.exec()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _on_authenticated(self, _data: dict):
        self._auth_pending = False
        self._build_main_widget()
        self._show_splash()

    # ── Main widget ───────────────────────────────────────────────────────────

    def _build_main_widget(self, offline: bool = False):
        if self._main_ready:
            return

        # Lazy import to keep startup fast
        try:
            from ui.main_window import MusicApp
        except (ImportError, OSError) as e:
            # ui.main_window imports core.player_vlc, which imports the vlc
            # module — that raises exactly these two error types when
            # libVLC's shared library can't be found (missing python-vlc
            # package -> ImportError; python-vlc installed but the actual
            # libvlc.dll/.so isn't -> OSError from ctypes.CDLL). Both are
            # effectively the same problem for the user: no working audio
            # backend. A raw traceback here would be a dead end for them —
            # this at least says what to do about it.
            QMessageBox.critical(
                self,
                "Ошибка запуска",
                "Не удалось загрузить компонент воспроизведения аудио (VLC).\n\n"
                "Попробуйте установить VLC media player (videolan.org) и "
                "запустить приложение снова.\n\n"
                f"Подробности: {e}",
            )
            sys.exit(1)

        self._main_widget = MusicApp(account_manager=self._account_manager, offline=offline)
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
