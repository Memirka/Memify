import os
import requests
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot


class DownloadCancelled(Exception):
    """Raised when a download task is cancelled."""


class DownloadWorker(QObject):
    """
    Downloads one or multiple files sequentially outside of the UI thread.
    Emits per-chunk progress updates as well as per-file completion/error events.
    """

    progress = pyqtSignal(int, int, int, int, str)
    file_finished = pyqtSignal(str, str)
    file_failed = pyqtSignal(str, str)
    finished = pyqtSignal(int, int, bool)
    error = pyqtSignal(str)

    def __init__(self, tasks: list[dict], *, default_timeout: float = 30.0, default_chunk: int = 8192):
        super().__init__()
        self.tasks = tasks or []
        self.default_timeout = default_timeout
        self.default_chunk = default_chunk
        self._cancelled = False

    @pyqtSlot()
    def cancel(self):
        self._cancelled = True

    @pyqtSlot()
    def run(self):
        if not self.tasks:
            self.finished.emit(0, 0, self._cancelled)
            return

        success = 0
        failed = 0
        total = len(self.tasks)

        try:
            for index, task in enumerate(self.tasks, start=1):
                if self._cancelled:
                    break

                try:
                    self._download_single(task, index, total)
                    success += 1
                    self.file_finished.emit(task.get("path", ""), task.get("label", task.get("path", "")))
                except DownloadCancelled:
                    break
                except Exception as exc:
                    failed += 1
                    path = task.get("path", "")
                    self.file_failed.emit(path, str(exc))
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self.finished.emit(success, failed, self._cancelled)

    def _download_single(self, task: dict, index: int, total_files: int):
        url = (task or {}).get("url")
        dest = (task or {}).get("path")
        if not url or not dest:
            raise ValueError("Неверная задача загрузки: отсутствует URL или путь сохранения")

        timeout = task.get("timeout", self.default_timeout)
        chunk_size = task.get("chunk_size", self.default_chunk)
        label = task.get("label") or os.path.basename(dest)

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                total_bytes = int(response.headers.get("content-length") or 0)
                downloaded = 0

                with open(dest, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if self._cancelled:
                            raise DownloadCancelled()
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(index, total_files, downloaded, total_bytes, label)
        except DownloadCancelled:
            self._cleanup_partial_file(dest)
            raise
        except Exception:
            self._cleanup_partial_file(dest)
            raise

    @staticmethod
    def _cleanup_partial_file(path: str):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
