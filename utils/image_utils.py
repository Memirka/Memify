# utils/image_utils.py

import requests
from functools import lru_cache
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt
from io import BytesIO

# Один общий сессионный клиент для всех загрузок
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Memify/1.0"
})

# ====== ЗАГРУЗКА С КЭШЕМ ======

@lru_cache(maxsize=256)
def _download_pixmap_cached(url: str) -> QPixmap | None:
    """
    Скачивает картинку по URL ОДИН раз и кладёт в кэш.
    Все следующие вызовы с тем же url берут QPixmap из памяти.
    """
    if not url:
        return None

    try:
        resp = _session.get(url, timeout=5)
        if not resp.ok:
            return None

        data = resp.content
        if not data:
            return None

        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return None

        return pixmap
    except Exception:
        return None


def load_pixmap_from_url(url: str) -> QPixmap | None:
    """
    Обёртка, которую зовёт весь остальной код.
    Ничего в интерфейсе не меняется, просто добавился кэш и один Session.
    """
    try:
        return _download_pixmap_cached(url)
    except Exception:
        return None


def make_rounded_pixmap(pixmap: QPixmap, size: int, radius: int) -> QPixmap:
    """
    Обрезка в скруглённый квадратик (как у тебя уже было).
    """
    if not pixmap or pixmap.isNull():
        return QPixmap()

    # Если уже нужного размера — не гоняем через scaled второй раз.
    if pixmap.width() == size and pixmap.height() == size:
        scaled = pixmap
    else:
        scaled = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)

    from PyQt6.QtGui import QPainter, QPainterPath

    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.setClipPath(path)

    # Center-crop the expanded pixmap so avatars don't drift to the corner.
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawPixmap(x, y, scaled)
    painter.end()

    return result
