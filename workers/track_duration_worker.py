from collections import deque

from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput


class TrackDurationWorker(QObject):
    """Sequential track-duration prober.

    Uses a muted QMediaPlayer rather than shelling out to the `ffprobe` CLI:
    Qt's multimedia backend links FFmpeg as shared libraries used internally
    by QMediaPlayer, not as an invokable command, so a standalone `ffprobe`
    binary is not part of a PyInstaller build of this app and generally
    isn't present on a user's machine either — every duration probe was
    silently failing (caught by a broad except, returning 0), leaving every
    track's duration column blank.

    Must be constructed and used on the main/GUI thread — QMediaPlayer needs
    a live Qt event loop, so despite the name this is NOT a QThread worker.
    It processes its queue asynchronously via signals/timers so it never
    blocks the UI, the same way PlayerController.warm_up()'s throwaway
    player does.
    """

    duration_ready = pyqtSignal(str, int)
    finished = pyqtSignal()

    TIMEOUT_MS = 8000

    def __init__(self, urls: list[str], parent=None):
        super().__init__(parent)
        self._queue = deque(urls or [])
        self._stopped = False
        self._current_url: str | None = None

        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._output.setMuted(True)
        self._output.setVolume(0.0)
        self._player.setAudioOutput(self._output)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.errorOccurred.connect(self._on_error)

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

    def start(self):
        self._advance()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._timeout_timer.stop()
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except Exception:
            pass

    def _advance(self):
        if self._stopped:
            return
        if not self._queue:
            self.finished.emit()
            return
        self._current_url = self._queue.popleft()
        try:
            self._player.setSource(QUrl(self._current_url))
            self._player.play()
            self._timeout_timer.start(self.TIMEOUT_MS)
        except Exception:
            self._current_url = None
            QTimer.singleShot(0, self._advance)

    def _finish_current(self):
        self._timeout_timer.stop()
        self._current_url = None
        try:
            self._player.stop()
        except Exception:
            pass
        QTimer.singleShot(0, self._advance)

    def _on_duration_changed(self, ms: int):
        if self._stopped or not self._current_url:
            return
        if ms > 0:
            url = self._current_url
            self.duration_ready.emit(url, ms)
            self._finish_current()

    def _on_error(self, *_args):
        if self._stopped or not self._current_url:
            return
        self._finish_current()

    def _on_timeout(self):
        if self._stopped or not self._current_url:
            return
        self._finish_current()
