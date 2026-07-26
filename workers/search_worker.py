from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from core.library import LibraryManager


class SearchWorker(QObject):
    request = pyqtSignal(str, int)
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str, int)

    def __init__(self):
        super().__init__()
        self._lib: LibraryManager | None = None
        self._abort = False
        self._busy = False
        self._latest = None
        self.request.connect(self.search)

    @pyqtSlot()
    def abort(self):
        self._abort = True

    @pyqtSlot(str, int)
    def search(self, query: str, generation: int):
        if self._abort:
            self.finished.emit([], generation)
            return
        self._latest = (query, generation)
        if not self._busy:
            self._run_next()

    def _run_next(self):
        if self._abort or not self._latest:
            return
        self._busy = True
        query, generation = self._latest
        self._latest = None
        try:
            if self._lib is None:
                self._lib = LibraryManager()
                try:
                    self._lib.load_library()
                except Exception:
                    pass
            results = self._lib.fast_search(query.strip()) if query.strip() else []
            self.finished.emit(results, generation)
        except Exception as e:
            self.error.emit(str(e), generation)
        finally:
            self._busy = False
            if self._latest and not self._abort:
                self._run_next()
