from PyQt6.QtCore import QObject, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPainter, QPainterPath
import requests
import time


class ImageLoaderWorker(QObject):
    image_loaded = pyqtSignal(str, object, int, int)
    finished = pyqtSignal()
    stop_signal = pyqtSignal()

    def __init__(self, urls, size, radius, timeout: float | tuple = 8.0, max_attempts: int = 3):
        super().__init__()
        self.urls = urls
        self.size = int(size)
        self.radius = int(radius)
        self._stop = False
        self._max_attempts = max(1, int(max_attempts or 1))
        if isinstance(timeout, (list, tuple)):
            self._timeout = tuple(timeout)
        else:
            try:
                self._timeout = float(timeout)
            except Exception:
                self._timeout = 8.0
        self.stop_signal.connect(self.stop)

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Memify/1.0"})

    def stop(self):
        """Останавливает загрузчик изображений."""
        print(f"Останавливаем ImageLoaderWorker для {len(self.urls)} URL")
        self._stop = True

    def run(self):
        """Загружает изображения из очереди."""
        try:
            print(f"Старт загрузки {len(self.urls)} изображений")

            for i, url in enumerate(self.urls):
                if self._stop:
                    print(f"Остановка загрузчика изображений на {i}/{len(self.urls)}")
                    break

                if not url:
                    continue

                try:
                    try:
                        req_url = requests.utils.requote_uri(url)
                    except Exception:
                        req_url = url
                    resp = None
                    for attempt in range(self._max_attempts):
                        if self._stop:
                            break
                        resp = self._session.get(req_url, timeout=self._timeout)
                        if resp.ok and resp.content:
                            break
                        if attempt + 1 < self._max_attempts:
                            time.sleep(0.12 * (attempt + 1))
                    if not resp or not resp.ok or not resp.content:
                        continue

                    if self._stop:
                        break

                    img = QImage()
                    if not img.loadFromData(resp.content):
                        continue

                    # Подгоняем размер прямо здесь, чтобы разгрузить UI‑поток.
                    if self.size:
                        img = img.scaled(
                            self.size,
                            self.size,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        if img.width() != self.size or img.height() != self.size:
                            # Center-crop to a square so rounded avatars stay circular.
                            x = max(0, (img.width() - self.size) // 2)
                            y = max(0, (img.height() - self.size) // 2)
                            img = img.copy(x, y, self.size, self.size)

                    if self.radius and self.radius > 0 and not img.isNull():
                        try:
                            r = float(self.radius)
                            out = QImage(img.size(), QImage.Format.Format_ARGB32_Premultiplied)
                            out.fill(Qt.GlobalColor.transparent)
                            painter = QPainter(out)
                            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                            path = QPainterPath()
                            path.addRoundedRect(0, 0, img.width(), img.height(), r, r)
                            painter.setClipPath(path)
                            painter.drawImage(0, 0, img)
                            painter.end()
                            img = out
                        except Exception:
                            pass

                    if self._stop:
                        break

                    self.image_loaded.emit(url, img, self.size, self.radius)

                    if not self._stop:
                        time.sleep(0.01)
                except Exception as e:
                    print(f"Ошибка загрузки изображения {url}: {e}")
                    continue

            print(f"Остановка загрузки изображений завершена. stop={self._stop}")

        except Exception as e:
            print(f"Ошибка завершения работы ImageLoaderWorker: {e}")
        finally:
            try:
                self._session.close()
            except Exception:
                pass
            self.finished.emit()
            print("ImageLoaderWorker завершил загрузку изображений")
