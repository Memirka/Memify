from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QSizePolicy
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap, QFontMetrics

from utils.format_utils import clean_title
from utils.image_utils import make_rounded_pixmap
from .shimmer_placeholder import ShimmerLabel
from .styles import COLORS, get_album_widget_style


class AlbumWidget(QWidget):
    clicked = pyqtSignal(dict)

    def __init__(self, album: dict, cover_url: str = "",
                 widget_size: int = 180, cover_size: int = 160,
                 cover_radius: int = 14, parent=None):
        super().__init__(parent)
        self.album = album or {}
        self.cover_url = cover_url or ""

        self._widget_size = int(widget_size)
        self._cover_size = int(cover_size)
        self._cover_radius = int(cover_radius)
        pad = max(6, (widget_size - cover_size) // 2)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("albumCard")
        self.setStyleSheet(get_album_widget_style())
        self.setFixedWidth(self._widget_size)

        root = QVBoxLayout(self)
        root.setContentsMargins(pad, 10, pad, 10)
        root.setSpacing(8)

        self.cover_label = ShimmerLabel(
            objectName="cover",
            radius=self._cover_radius,
            base_color=COLORS["SURFACE_LIGHT"],
            highlight_color=COLORS["SURFACE_HOVER"],
        )
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setFixedSize(self._cover_size, self._cover_size)
        self.cover_label.setStyleSheet(
            f"border-radius: {self._cover_radius}px; background-color: transparent;"
        )
        root.addWidget(self.cover_label, 0, Qt.AlignmentFlag.AlignHCenter)

        title_text = clean_title(self.album.get("title", "")) or "Неизвестно"
        self.title_label = QLabel(title_text)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.title_label.setWordWrap(True)
        font = QFont("Segoe UI", 9, QFont.Weight.Medium)
        self.title_label.setFont(font)
        self.title_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; background: transparent;")
        fm = QFontMetrics(font)
        self.title_label.setMaximumHeight(fm.height() * 2 + 4)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root.addWidget(self.title_label)

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_cover(self, pixmap: QPixmap):
        if pixmap and not pixmap.isNull():
            rounded = make_rounded_pixmap(pixmap, self._cover_size, self._cover_radius)
            self.cover_label.setPixmap(rounded)
        else:
            self.cover_label.setPixmap(QPixmap())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.album)
        super().mousePressEvent(event)
