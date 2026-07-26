import json
import subprocess
from collections import deque

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot


class TrackDurationWorker(QObject):
    """Sequential loader that fetches track durations via ffprobe subprocess.

    Runs in a background QThread — does not touch the Qt multimedia stack at all,
    so the UI stays fully responsive during loading.
    """

    duration_ready = pyqtSignal(str, int)
    finished = pyqtSignal()

    def __init__(self, urls: list[str], timeout_ms: int = 8000):
        super().__init__()
        self._queue = deque(urls or [])
        self._timeout_s = max(3, timeout_ms / 1000)
        self._stopped = False
        self._current_proc: subprocess.Popen | None = None

    @pyqtSlot()
    def start(self):
        try:
            while self._queue and not self._stopped:
                url = self._queue.popleft()
                duration = self._load_duration(url)
                if not self._stopped:
                    self.duration_ready.emit(url, duration)
        finally:
            self.finished.emit()

    @pyqtSlot()
    def stop(self):
        self._stopped = True
        self._queue.clear()
        proc = self._current_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _load_duration(self, url: str) -> int:
        if not url or self._stopped:
            return 0
        try:
            proc = subprocess.Popen(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    "-analyzeduration", "500000",
                    "-probesize", "200000",
                    url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._current_proc = proc
            try:
                stdout, _ = proc.communicate(timeout=self._timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return 0
            finally:
                self._current_proc = None

            if proc.returncode == 0 and stdout:
                data = json.loads(stdout)
                dur = data.get("format", {}).get("duration")
                if dur:
                    return int(float(dur) * 1000)
        except Exception:
            pass
        return 0
