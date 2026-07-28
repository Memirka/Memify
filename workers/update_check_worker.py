from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.updater import check_update


class UpdateCheckWorker(QObject):
    """Runs the update-availability check off the UI thread so a slow/dead
    server can never freeze startup."""

    finished = pyqtSignal(object)  # dict (update available) or None

    def __init__(self, server_url: str, current_version: str):
        super().__init__()
        self._server_url = server_url
        self._current_version = current_version

    @pyqtSlot()
    def run(self):
        info = None
        try:
            info = check_update(self._server_url, self._current_version)
        except Exception:
            info = None
        self.finished.emit(info)
