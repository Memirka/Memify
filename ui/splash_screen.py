import os

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap, QImage
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget


class SplashScreen(QWidget):
    finished = pyqtSignal()

    def __init__(self, icon_path: str, duration_ms: int = 1400, hold_after_ms: int = 1200, as_window: bool = False, parent=None):
        super().__init__(parent)
        self._icon_path = icon_path
        self._duration_ms = duration_ms
        self._hold_after_ms = hold_after_ms
        self._as_window = as_window
        self._setup_ui()
        self._setup_animation()

    def _setup_ui(self):
        self.setWindowTitle("Memify")
        if self._as_window:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
            self.setFixedSize(520, 340)
        self.setStyleSheet("background-color: #2f2f2f;")

        if self._as_window and self._icon_path and os.path.exists(self._icon_path):
            self.setWindowIcon(QIcon(self._icon_path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon_label = QLabel(self)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setStyleSheet("background: transparent;")

        pixmap = QPixmap(self._icon_path) if self._icon_path and os.path.exists(self._icon_path) else QPixmap()
        if not pixmap.isNull():
            pixmap = self._remove_flat_background(pixmap)
            pixmap = pixmap.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._icon_label.setPixmap(pixmap)
        else:
            self._icon_label.setText("Memify")
            self._icon_label.setStyleSheet("color: white; font-size: 24px; font-weight: 600;")

        self._opacity_effect = QGraphicsOpacityEffect(self._icon_label)
        self._icon_label.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(0.0)

        layout.addWidget(self._icon_label)

    def _setup_animation(self):
        self._fade_in = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_in.setDuration(self._duration_ms)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_in.finished.connect(self._on_fade_in_finished)

        self._fade_out = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_out.setDuration(self._duration_ms)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_out.finished.connect(self.finished.emit)

    def start(self):
        if self._as_window:
            self._center_on_screen()
            self.show()
        self._fade_in.start()

    def _center_on_screen(self):
        if not self._as_window:
            return
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geometry = screen.availableGeometry()
        target_x = geometry.center().x() - self.width() // 2
        target_y = geometry.center().y() - self.height() // 2
        self.move(target_x, target_y)

    def _on_fade_in_finished(self):
        QTimer.singleShot(self._hold_after_ms, self._start_fade_out)

    def _start_fade_out(self):
        self._fade_out.start()

    def _remove_flat_background(self, pixmap: QPixmap) -> QPixmap:
        """Make single-color backgrounds transparent to avoid visible squares on splash."""
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        if image.isNull():
            return pixmap

        # If any corner already has transparency, assume the asset is fine.
        corner_alpha = [
            image.pixelColor(0, 0).alpha(),
            image.pixelColor(image.width() - 1, 0).alpha(),
            image.pixelColor(0, image.height() - 1).alpha(),
            image.pixelColor(image.width() - 1, image.height() - 1).alpha(),
        ]
        if any(a < 255 for a in corner_alpha):
            return pixmap

        bg_color = image.pixelColor(0, 0)
        tolerance = 12

        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if (
                    abs(color.red() - bg_color.red()) <= tolerance
                    and abs(color.green() - bg_color.green()) <= tolerance
                    and abs(color.blue() - bg_color.blue()) <= tolerance
                ):
                    color.setAlpha(0)
                    image.setPixelColor(x, y, color)

        return QPixmap.fromImage(image)
