from PyQt6.QtCore import QEasingCurve, QVariantAnimation, QAbstractAnimation, Qt, QRectF
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QGradient
from PyQt6.QtWidgets import QLabel, QWidget


class _ShimmerMixin:
    """Shared shimmer animation helpers."""

    def _init_shimmer(self, base_color="#2A2A2A", highlight_color="#3A3A3A", radius=10, duration=1300):
        self._shimmer_base = QColor(base_color)
        self._shimmer_highlight = QColor(highlight_color)
        self._shimmer_radius = radius
        self._shimmer_progress = 0.0
        self._shimmer_enabled = True

        self._shimmer_anim = QVariantAnimation(self)
        self._shimmer_anim.setStartValue(0.0)
        self._shimmer_anim.setEndValue(1.0)
        self._shimmer_anim.setDuration(duration)
        self._shimmer_anim.setLoopCount(-1)
        self._shimmer_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.InOutSine))
        self._shimmer_anim.valueChanged.connect(self._on_shimmer_value)
        self._start_shimmer()

    def _on_shimmer_value(self, value):
        self._shimmer_progress = float(value or 0.0)
        try:
            self.update()
        except Exception:
            pass

    def _paint_shimmer(self, painter: QPainter, rect):
        gradient = QLinearGradient(0, 0, 1, 0)
        gradient.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)

        mid = -0.35 + (self._shimmer_progress * 1.7)
        spread = 0.25
        left = mid - spread
        right = mid + spread

        gradient.setColorAt(0.0, self._shimmer_base)
        if left > 0:
            gradient.setColorAt(min(left, 1.0), self._shimmer_base)
        gradient.setColorAt(max(0.0, min(1.0, mid)), self._shimmer_highlight)
        if right < 1:
            gradient.setColorAt(max(0.0, min(1.0, right)), self._shimmer_base)
        gradient.setColorAt(1.0, self._shimmer_base)

        path = QPainterPath()
        radius = max(0.0, float(self._shimmer_radius))
        rectf = QRectF(rect)
        rectf = rectf.adjusted(0.5, 0.5, -0.5, -0.5)
        path.addRoundedRect(rectf, radius, radius)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillPath(path, gradient)

    def _start_shimmer(self):
        if self._shimmer_enabled and self._shimmer_anim.state() != QAbstractAnimation.State.Running:
            self._shimmer_anim.start()

    def _stop_shimmer(self):
        if self._shimmer_anim.state() != QAbstractAnimation.State.Stopped:
            self._shimmer_anim.stop()


class ShimmerPlaceholder(QWidget, _ShimmerMixin):
    """Simple shimmering block for loading placeholders."""

    def __init__(self, *args, base_color="#2A2A2A", highlight_color="#3A3A3A", radius=10, duration=1300, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._init_shimmer(base_color, highlight_color, radius, duration)

    def paintEvent(self, event):
        painter = QPainter(self)
        self._paint_shimmer(painter, self.rect())
        painter.end()

    def showEvent(self, event):
        self._start_shimmer()
        super().showEvent(event)

    def hideEvent(self, event):
        self._stop_shimmer()
        super().hideEvent(event)


class ShimmerLabel(QLabel, _ShimmerMixin):
    """QLabel that shows a shimmering placeholder until content appears."""

    def __init__(self, *args, base_color="#2A2A2A", highlight_color="#3A3A3A", radius=10, duration=1300, **kwargs):
        super().__init__(*args, **kwargs)
        self._placeholder_visible = True
        self._init_shimmer(base_color, highlight_color, radius, duration)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

    def set_placeholder_visible(self, visible: bool):
        self._placeholder_visible = bool(visible)
        if self._placeholder_visible:
            self._start_shimmer()
        else:
            self._stop_shimmer()
        self.update()

    def setPixmap(self, pixmap):
        super().setPixmap(pixmap)
        has_pixmap = pixmap is not None and hasattr(pixmap, "isNull") and not pixmap.isNull()
        self.set_placeholder_visible(not has_pixmap)

    def clear(self):
        super().clear()
        self.set_placeholder_visible(True)

    def paintEvent(self, event):
        show_placeholder = self._placeholder_visible and (self.pixmap() is None or self.pixmap().isNull())
        if show_placeholder:
            painter = QPainter(self)
            self._paint_shimmer(painter, self.rect())
            painter.end()
        super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()

    def showEvent(self, event):
        if self._placeholder_visible:
            self._start_shimmer()
        super().showEvent(event)

    def hideEvent(self, event):
        self._stop_shimmer()
        super().hideEvent(event)
