import os
import hashlib
import threading

from PyQt6.QtGui import QPixmap

from config import (
    IMAGE_CACHE_SIZE,
    COVER_CACHE_DIR,
    COVER_CACHE_MAX_SIZE_MB,
    COVER_CACHE_MAX_FILES,
)


def cache_key(url: str, size: int, radius: int) -> str:
    """
    Стабильный ключ для кэша: URL плюс нормализованные размер и радиус,
    чтобы разные масштабы обложек не путались между собой.
    """
    try:
        radius_norm = max(0, 2 * round(int(radius) / 2))
    except Exception:
        radius_norm = int(radius) if radius else 0

    # Размер не включаем в ключ, чтобы одна и та же обложка не дублировалась в кэше.
    try:
        size_norm = max(0, 8 * round(int(size or 0) / 8))
    except Exception:
        size_norm = int(size) if size else 0

    return f"{str(url).strip()}|r{radius_norm}|s{size_norm}"


class CoverCache:
    """
    Кэш обложек:
    - в памяти: ограниченное число QPixmap
    - на диске: сжатые jpg в COVER_CACHE_DIR
    """

    def __init__(self, max_items: int = IMAGE_CACHE_SIZE):
        self._max_items = max(10, int(max_items or 50))
        self._lock = threading.RLock()
        self._data: dict[str, QPixmap] = {}
        self._order: list[str] = []

        os.makedirs(COVER_CACHE_DIR, exist_ok=True)

    # ===================== Вспомогательные =====================

    def _key_to_path(self, key: str) -> str:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(COVER_CACHE_DIR, f"{h}.png")

    def _touch_file(self, path: str) -> None:
        """Обновляем время доступа, чтобы LRU по mtimes работал нормально."""
        try:
            os.utime(path, None)
        except Exception:
            pass

    def _load_from_disk(self, key: str) -> QPixmap | None:
        """Пробуем достать обложку с диска по ключу."""
        path = self._key_to_path(key)
        if not os.path.exists(path):
            return None

        try:
            pm = QPixmap()
            if pm.load(path, "PNG"):
                self._touch_file(path)
                return pm
        except Exception:
            return None

        return None

    def _save_to_disk(self, key: str, pixmap: QPixmap, allow_replace: bool = False) -> None:
        """Сохраняем обложку на диск и подчищаем старые при переполнении."""
        if pixmap is None or pixmap.isNull():
            return

        path = self._key_to_path(key)

        if os.path.exists(path):
            if not allow_replace:
                self._touch_file(path)
                return
            try:
                existing = self._load_from_disk(key)
                if existing and existing.width() >= pixmap.width() and existing.height() >= pixmap.height():
                    # Уже лежит не меньшая версия — оставляем.
                    return
            except Exception:
                pass

        try:
            tmp = QPixmap(pixmap)
            tmp.save(path, "PNG")
        except Exception:
            return

        # После записи следим за размером кэша
        self._enforce_disk_limits()

    def _enforce_disk_limits(self) -> None:
        """Ограничиваем размер и количество кэшированных файлов."""
        try:
            max_bytes = int(COVER_CACHE_MAX_SIZE_MB) * 1024 * 1024
        except Exception:
            max_bytes = 128 * 1024 * 1024

        try:
            max_files = int(COVER_CACHE_MAX_FILES)
        except Exception:
            max_files = 2000

        try:
            entries = []
            total_size = 0

            for name in os.listdir(COVER_CACHE_DIR):
                path = os.path.join(COVER_CACHE_DIR, name)
                if not os.path.isfile(path):
                    continue
                try:
                    st = os.stat(path)
                    size = st.st_size
                    mtime = st.st_mtime
                except OSError:
                    continue

                entries.append((path, size, mtime))
                total_size += size

            if total_size <= max_bytes and len(entries) <= max_files:
                return

            # Сортируем по времени последнего изменения: старые первыми на удаление
            entries.sort(key=lambda t: t[2])  # по mtime

            for path, size, _ in entries:
                if total_size <= max_bytes and len(entries) <= max_files:
                    break
                try:
                    os.remove(path)
                    total_size -= size
                except OSError:
                    pass

        except Exception:
            # В худшем случае просто оставим кэш как есть
            pass

    def _remember_in_memory(self, key: str, pixmap: QPixmap) -> None:
        """Записываем обложку в RAM с LRU."""
        if pixmap is None or pixmap.isNull():
            return

        if key in self._order:
            try:
                self._order.remove(key)
            except ValueError:
                pass

        self._order.append(key)
        self._data[key] = pixmap

        while len(self._order) > self._max_items:
            old_key = self._order.pop(0)
            self._data.pop(old_key, None)

    # ===================== Публичный API =====================

    def get(self, key: str) -> QPixmap | None:
        """Получить обложку из кэша."""
        if not key:
            return None

        with self._lock:
            pm = self._data.get(key)
            if pm is not None and not pm.isNull():
                # обновляем LRU
                try:
                    self._order.remove(key)
                except ValueError:
                    pass
                self._order.append(key)
                return pm

        # Не нашли в памяти - пробуем с диска
        pm = self._load_from_disk(key)
        if pm is None or pm.isNull():
            return None

        # Кладём то, что прочитали с диска, в RAM
        with self._lock:
            self._remember_in_memory(key, pm)

        return pm

    def set(self, key: str, pixmap: QPixmap) -> None:
        """Положить обложку в кэш (RAM + диск)."""
        if not key or pixmap is None or pixmap.isNull():
            return

        try:
            with self._lock:
                existing = self._data.get(key)
                if existing and existing.width() >= pixmap.width() and existing.height() >= pixmap.height():
                    # Уже есть более крупная версия в памяти.
                    self._remember_in_memory(key, existing)
                else:
                    try:
                        disk_pm = self._load_from_disk(key)
                    except Exception:
                        disk_pm = None

                    if disk_pm and disk_pm.width() >= pixmap.width() and disk_pm.height() >= pixmap.height():
                        self._remember_in_memory(key, disk_pm)
                    else:
                        self._remember_in_memory(key, pixmap)

            # Дисковую часть делаем без долгой блокировки
            self._save_to_disk(key, pixmap, allow_replace=False)
        except Exception:
            pass

    def clear(self) -> None:
        """Очистить только кэш в памяти (диск не трогаем)."""
        with self._lock:
            self._data.clear()
            self._order.clear()

    def clear_disk_cache(self) -> tuple[int, int]:
        """
        Удаляет файлы кэша обложек с диска.
        Возвращает (количество удалённых файлов, суммарный размер в байтах).
        """
        removed = 0
        freed = 0
        try:
            for name in os.listdir(COVER_CACHE_DIR):
                path = os.path.join(COVER_CACHE_DIR, name)
                if not os.path.isfile(path):
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                try:
                    os.remove(path)
                    removed += 1
                    freed += size
                except OSError:
                    continue
        except FileNotFoundError:
            pass

        self.clear()
        return removed, freed

cover_cache = CoverCache()
