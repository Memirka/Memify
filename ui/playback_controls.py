from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer, QPoint
from PyQt6.QtCore import QPropertyAnimation, QSequentialAnimationGroup, QPauseAnimation
from PyQt6.QtGui import QFont, QCursor, QPixmap, QPainter, QPainterPath, QColor, QBrush, QFontMetrics

import ui.styles as styles_module
from utils.format_utils import format_duration, clean_artist_name, clean_title
from utils.image_utils import make_rounded_pixmap


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._update_style()

    def _update_style(self):
        self.setStyleSheet(
            "QLabel { color: #B3B3B3; padding: 0; margin: 0; border: none; }"
            "QLabel:hover { color: #FFFFFF; text-decoration: underline; }"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MarqueeLabel(QWidget):
    """Sliding marquee label for long text."""
    clicked = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._speed = 50.0
        self._start_pause = 600
        self._edge_pause = 700
        self._threshold = 6

        self._label = ClickableLabel(text, self)
        self._label.clicked.connect(self.clicked.emit)
        self._label.setWordWrap(False)
        self._label.move(0, 0)
        self._anim = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._label.text()

    def setText(self, text: str):
        self._label.setText(text)
        self._label.setMinimumSize(0, 0)
        self._label.setMaximumSize(16777215, 16777215)
        self._label.adjustSize()
        self._refresh()
        self.updateGeometry()

    def setFont(self, font: QFont):
        self._label.setFont(font)
        self._label.adjustSize()
        self._refresh()
        self.updateGeometry()

    def setStyleSheet(self, style: str):
        self._label.setStyleSheet(style)
        self._label.adjustSize()
        self._refresh()
        self.updateGeometry()

    def sizeHint(self):
        h = self._label.sizeHint()
        return QSize(min(h.width(), 200), h.height())

    def minimumSizeHint(self):
        h = self._label.sizeHint()
        return QSize(min(40, h.width()), h.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _stop(self):
        if self._anim:
            try:
                self._anim.stop()
            except Exception:
                pass
            try:
                self._anim.deleteLater()
            except Exception:
                pass
            self._anim = None
        self._label.move(0, 0)

    def _refresh(self):
        try:
            vw = max(0, self.width())
            if vw <= 0:
                self._stop()
                if self.isVisible():
                    QTimer.singleShot(60, self._refresh)
                return

            self._label.setMinimumSize(0, 0)
            self._label.setMaximumSize(16777215, 16777215)
            self._label.adjustSize()
            hint = self._label.sizeHint()
            tw, th = hint.width(), hint.height()
            self._label.setFixedSize(tw, th)
            self.setMinimumHeight(th)
            self.setMaximumHeight(th)
            self.updateGeometry()

            delta = tw - vw
            if delta <= self._threshold:
                self._stop()
                return

            dur_ms = max(900, int(delta / max(1.0, self._speed) * 1000))

            if self._anim:
                try:
                    self._anim.stop()
                except Exception:
                    pass

            group = QSequentialAnimationGroup(self)
            group.setLoopCount(-1)
            group.addAnimation(QPauseAnimation(self._start_pause, group))

            fwd = QPropertyAnimation(self._label, b"pos", group)
            fwd.setStartValue(QPoint(0, 0))
            fwd.setEndValue(QPoint(-delta, 0))
            fwd.setDuration(dur_ms)
            group.addAnimation(fwd)

            group.addAnimation(QPauseAnimation(self._edge_pause, group))

            bwd = QPropertyAnimation(self._label, b"pos", group)
            bwd.setStartValue(QPoint(-delta, 0))
            bwd.setEndValue(QPoint(0, 0))
            bwd.setDuration(dur_ms)
            group.addAnimation(bwd)

            group.addAnimation(QPauseAnimation(self._edge_pause, group))

            self._anim = group
            self._anim.start()
        except Exception:
            self._stop()


class MarqueeContainer(QWidget):
    """Like MarqueeLabel, but slides an arbitrary pre-built child widget
    (e.g. a row of several clickable artist chips) instead of owning a
    single QLabel. Call refresh() whenever the child's contents change."""

    def __init__(self, child: QWidget, parent=None):
        super().__init__(parent)
        self._speed = 50.0
        self._start_pause = 600
        self._edge_pause = 700
        self._threshold = 6

        self._child = child
        self._child.setParent(self)
        self._child.move(0, 0)
        self._child.show()  # setParent() hides the widget as a side effect
        self._anim = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        h = self._child.sizeHint()
        return QSize(min(h.width(), 200), h.height())

    def minimumSizeHint(self):
        h = self._child.sizeHint()
        return QSize(min(40, h.width()), h.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def _stop(self):
        if self._anim:
            try:
                self._anim.stop()
            except Exception:
                pass
            try:
                self._anim.deleteLater()
            except Exception:
                pass
            self._anim = None
        self._child.move(0, 0)

    def refresh(self):
        try:
            vw = max(0, self.width())
            if vw <= 0:
                self._stop()
                if self.isVisible():
                    QTimer.singleShot(60, self.refresh)
                return

            # Undo any previous setFixedSize() lock below before re-measuring —
            # otherwise a moment with an empty/tiny child (e.g. no artists yet)
            # permanently pins the child at that size and it can never grow again.
            self._child.setMinimumSize(0, 0)
            self._child.setMaximumSize(16777215, 16777215)
            self._child.adjustSize()
            hint = self._child.sizeHint()
            tw, th = hint.width(), hint.height()
            self._child.setFixedSize(tw, th)
            self.setMinimumHeight(th)
            self.setMaximumHeight(th)
            self.updateGeometry()

            delta = tw - vw
            if delta <= self._threshold:
                self._stop()
                return

            dur_ms = max(900, int(delta / max(1.0, self._speed) * 1000))

            if self._anim:
                try:
                    self._anim.stop()
                except Exception:
                    pass

            group = QSequentialAnimationGroup(self)
            group.setLoopCount(-1)
            group.addAnimation(QPauseAnimation(self._start_pause, group))

            fwd = QPropertyAnimation(self._child, b"pos", group)
            fwd.setStartValue(QPoint(0, 0))
            fwd.setEndValue(QPoint(-delta, 0))
            fwd.setDuration(dur_ms)
            group.addAnimation(fwd)

            group.addAnimation(QPauseAnimation(self._edge_pause, group))

            bwd = QPropertyAnimation(self._child, b"pos", group)
            bwd.setStartValue(QPoint(-delta, 0))
            bwd.setEndValue(QPoint(0, 0))
            bwd.setDuration(dur_ms)
            group.addAnimation(bwd)

            group.addAnimation(QPauseAnimation(self._edge_pause, group))

            self._anim = group
            self._anim.start()
        except Exception:
            self._stop()


class ClickableSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                self.setProperty("_user_seek", True)
            except Exception:
                pass
            x = event.position().x()
            w = self.width()
            if w > 0:
                ratio = max(0.0, min(1.0, x / w))
                self.setValue(self.minimum() + int((self.maximum() - self.minimum()) * ratio))
            event.accept()
        super().mousePressEvent(event)


class _CircleButton(QPushButton):
    """Round play/pause button with pixel-perfect centered text via paintEvent."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent; border: none;")

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        c = styles_module.COLORS
        bg = QColor(c["TEXT_SECONDARY"] if self._hovered else c["TEXT_PRIMARY"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawEllipse(r)
        p.setPen(QColor("#000000"))
        f = self.font()
        f.setPointSize(14)
        f.setBold(True)
        p.setFont(f)
        text = self.text()
        fm = QFontMetrics(f)
        tb = fm.tightBoundingRect(text)
        cx = r.x() + r.width() / 2
        cy = r.y() + r.height() / 2
        x = int(cx - tb.x() - tb.width() / 2)
        y = int(cy - tb.y() - tb.height() / 2)
        p.drawText(x, y, text)
        p.end()


class PlaybackControls(QWidget):
    artist_clicked = pyqtSignal(str)
    album_clicked = pyqtSignal(str, str)
    like_clicked = pyqtSignal()
    cover_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_artist_name = ""
        self.current_album_title = ""
        self._slider_down = False
        self._cover_original: QPixmap | None = None
        self._is_playing = False
        self._like_state = False
        self._like_enabled = True
        self._repeat_mode = "off"

        self.setup_ui()
        self.setup_connections()
        self._apply_style()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(6)
        main_layout.setContentsMargins(8, 6, 8, 6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)
        bottom_row.setContentsMargins(0, 4, 0, 0)
        bottom_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # ── Left: track info ──────────────────────────────────────────────────
        info_w = self._build_track_info()
        bottom_row.addWidget(info_w, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        bottom_row.addStretch(1)

        # ── Center: controls ──────────────────────────────────────────────────
        center_w = self._build_controls()
        bottom_row.addWidget(center_w, 0, Qt.AlignmentFlag.AlignHCenter)
        bottom_row.addStretch(1)

        # ── Right: volume ─────────────────────────────────────────────────────
        vol_w = self._build_volume()
        bottom_row.addWidget(vol_w, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Keep side columns same width so center stays truly centered
        side_w = max(info_w.sizeHint().width(), vol_w.sizeHint().width(), 260)
        info_w.setFixedWidth(side_w)
        vol_w.setFixedWidth(side_w)

        main_layout.addLayout(bottom_row)
        self.clear_info()

    def _build_track_info(self) -> QWidget:
        layout = QHBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(56, 56)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(
            f"background-color: {styles_module.COLORS['COVER_BG']}; border-radius: 6px;"
        )
        self.cover_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover_label.mousePressEvent = lambda e: (
            self.cover_clicked.emit() if e.button() == Qt.MouseButton.LeftButton and self._cover_original else None
        )
        layout.addWidget(self.cover_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(4)
        text_layout.setContentsMargins(0, 0, 0, 0)

        self.track_title = MarqueeLabel("Нет трека")
        self.track_title.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.track_title.clicked.connect(self._on_album_clicked)
        text_layout.addWidget(self.track_title)

        self.artist_row = QWidget()
        artist_row_layout = QHBoxLayout(self.artist_row)
        artist_row_layout.setContentsMargins(0, 0, 0, 0)
        artist_row_layout.setSpacing(0)
        artist_row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._artist_row_marquee = MarqueeContainer(self.artist_row)
        text_layout.addWidget(self._artist_row_marquee)

        self.album_label = MarqueeLabel("")
        self.album_label.setFont(QFont("Segoe UI", 9))
        self.album_label.clicked.connect(self._on_album_clicked)
        text_layout.addWidget(self.album_label)

        layout.addLayout(text_layout, 1)

        self.like_btn = QPushButton("♡")
        self.like_btn.setFixedSize(28, 28)
        self.like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.like_btn.setToolTip("Нравится")
        self.like_btn.setEnabled(False)
        self.like_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {styles_module.COLORS['TEXT_SECONDARY']}; font-size: 16px; }}"
            f"QPushButton:hover {{ color: {styles_module.COLORS['TEXT_PRIMARY']}; }}"
        )
        self.like_btn.clicked.connect(self.like_clicked.emit)
        layout.addWidget(self.like_btn)

        w = QWidget()
        w.setLayout(layout)
        w.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        return w

    def _build_controls(self) -> QWidget:
        col = QVBoxLayout()
        col.setSpacing(4)
        col.setContentsMargins(0, 0, 0, 0)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(14)
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_row.setContentsMargins(0, 0, 0, 0)

        c = styles_module.COLORS
        btn_style = (
            f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; font-size: 18px; }}"
            f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
            f"QPushButton:checked {{ color: {c['PRIMARY']}; }}"
        )
        self.shuffle_btn = QPushButton("⇄")
        self.shuffle_btn.setFixedSize(32, 32)
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.shuffle_btn.setStyleSheet(btn_style)
        self.shuffle_btn.setToolTip("Перемешать")
        btn_row.addWidget(self.shuffle_btn)

        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setFixedSize(32, 32)
        self.prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.prev_btn.setStyleSheet(btn_style)
        self.prev_btn.setToolTip("Назад")
        btn_row.addWidget(self.prev_btn)

        self.play_btn = _CircleButton("▶")
        self.play_btn.setFixedSize(40, 40)
        self.play_btn.setToolTip("Пауза / Воспроизведение")
        btn_row.addWidget(self.play_btn)

        self.next_btn = QPushButton("⏭")
        self.next_btn.setFixedSize(32, 32)
        self.next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.next_btn.setStyleSheet(btn_style)
        self.next_btn.setToolTip("Далее")
        btn_row.addWidget(self.next_btn)

        self.repeat_btn = QPushButton("↻")
        self.repeat_btn.setFixedSize(32, 32)
        self.repeat_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.repeat_btn.setStyleSheet(btn_style)
        self.repeat_btn.setToolTip("Повтор")
        btn_row.addWidget(self.repeat_btn)

        col.addLayout(btn_row)

        # Progress row
        prog_row = QHBoxLayout()
        prog_row.setSpacing(8)
        prog_row.setContentsMargins(0, 0, 0, 0)
        prog_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.time_label = QLabel("0:00")
        self.time_label.setFixedWidth(38)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setStyleSheet(f"color: {styles_module.COLORS['TEXT_SECONDARY']}; font: 8pt 'Segoe UI';")
        prog_row.addWidget(self.time_label)

        self.progress_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setMinimum(0)
        self.progress_slider.setMaximum(1000)
        self.progress_slider.setValue(0)
        self.progress_slider.setMinimumWidth(340)
        self.progress_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        prog_row.addWidget(self.progress_slider, 1)

        self.duration_label = QLabel("0:00")
        self.duration_label.setFixedWidth(38)
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.duration_label.setStyleSheet(f"color: {styles_module.COLORS['TEXT_SECONDARY']}; font: 8pt 'Segoe UI';")
        prog_row.addWidget(self.duration_label)

        col.addLayout(prog_row)

        w = QWidget()
        w.setLayout(col)
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return w

    def _build_volume(self) -> QWidget:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        row.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(self.volume_slider)

        w = QWidget()
        w.setLayout(row)
        w.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        return w

    def setup_connections(self):
        self.progress_slider.sliderPressed.connect(self._on_slider_pressed)
        self.progress_slider.sliderReleased.connect(self._on_slider_released)

    def _apply_style(self):
        c = styles_module.COLORS
        slider_style = (
            f"QSlider::groove:horizontal {{"
            f"  height: 4px; background: {c['BORDER']}; border-radius: 2px;"
            f"}}"
            f"QSlider::handle:horizontal {{"
            f"  width: 12px; height: 12px;"
            f"  background: {c['TEXT_PRIMARY']};"
            f"  border-radius: 6px; margin: -4px 0;"
            f"}}"
            f"QSlider::sub-page:horizontal {{"
            f"  background: {c['PRIMARY']}; border-radius: 2px;"
            f"}}"
        )
        self.progress_slider.setStyleSheet(slider_style)
        self.volume_slider.setStyleSheet(slider_style)
        self.setStyleSheet(
            f"PlaybackControls {{ background-color: {c['SURFACE']}; border-top: 1px solid {c['BORDER']}; }}"
        )
        # Refresh shuffle checked state style
        self.shuffle_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; font-size: 18px; }}"
            f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
            f"QPushButton:checked {{ color: {c['PRIMARY']}; }}"
        )

    def apply_accent(self):
        """Re-apply styles after accent color change."""
        self._apply_style()
        self.set_repeat(self._repeat_mode)
        self.set_like_state(self._like_state, self._like_enabled)
        self.play_btn.update()

    # ── Public API ────────────────────────────────────────────────────────────

    def update_track_info(self, track: dict, artist: dict, album: dict, display_artist_names: list | None = None):
        title = clean_title(track.get("title", "")) or "Неизвестно"
        artist_name = track.get("artist_name") or (artist or {}).get("artist", "") or ""
        # When playing from a virtual album (e.g. liked tracks), use the real album title embedded in the track.
        album_title = track.get("_real_album_title") or (album or {}).get("title", "") or ""

        # current_artist_name stays the single real artist this track is playing under.
        self.current_artist_name = artist_name
        self.current_album_title = album_title

        self.track_title.setText(title)
        names = display_artist_names if display_artist_names else (
            [clean_artist_name(artist_name)] if artist_name else []
        )
        self._set_artist_names(names)
        self.album_label.setText(clean_title(album_title))

    def _set_artist_names(self, names: list):
        """Render one separately-clickable label per artist, comma-separated."""
        layout = self.artist_row.layout()
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        for i, name in enumerate(names):
            chip = ClickableLabel(name)
            chip.setFont(QFont("Segoe UI", 9))
            chip.clicked.connect(lambda n=name: self.artist_clicked.emit(n))
            layout.addWidget(chip)
            chip.show()  # newly added layout items aren't visible until shown/polished
            if i < len(names) - 1:
                sep = QLabel(", ")
                sep.setFont(QFont("Segoe UI", 9))
                sep.setStyleSheet("QLabel { color: #B3B3B3; padding: 0; margin: 0; border: none; }")
                layout.addWidget(sep)
                sep.show()

        self.artist_row.adjustSize()
        self._artist_row_marquee.refresh()

    def set_cover(self, pixmap: QPixmap | None):
        if pixmap and not pixmap.isNull():
            self._cover_original = pixmap
            pm = make_rounded_pixmap(pixmap, 56, 6)
            self.cover_label.setPixmap(pm)
        else:
            self._cover_original = None
            self.cover_label.setPixmap(QPixmap())

    def set_playing(self, is_playing: bool):
        self._is_playing = is_playing
        self.play_btn.setText("⏸" if is_playing else "▶")

    def update_position(self, position_ms: int, duration_ms: int):
        if self._slider_down:
            return
        self.time_label.setText(format_duration(position_ms))
        if duration_ms > 0:
            self.duration_label.setText(format_duration(duration_ms))
            try:
                self.progress_slider.blockSignals(True)
                self.progress_slider.setValue(int(position_ms / duration_ms * 1000))
            finally:
                self.progress_slider.blockSignals(False)

    def set_shuffle(self, enabled: bool):
        self.shuffle_btn.setChecked(enabled)

    def set_repeat(self, mode: str):
        self._repeat_mode = mode
        icons = {"off": "↻", "album": "↻", "track": "↺"}
        self.repeat_btn.setText(icons.get(mode, "↻"))
        c = styles_module.COLORS
        color = c["PRIMARY"] if mode != "off" else c["TEXT_SECONDARY"]
        self.repeat_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {color}; font-size: 16px; }}"
            f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
        )

    def set_like_state(self, liked: bool, enabled: bool = True):
        self._like_state = liked
        self._like_enabled = enabled
        self.like_btn.setEnabled(enabled)
        c = styles_module.COLORS
        if liked:
            self.like_btn.setText("♥")
            self.like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['PRIMARY']}; font-size: 16px; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self.like_btn.setText("♡")
            self.like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; font-size: 16px; }}"
                f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
            )

    def set_volume(self, value: int):
        try:
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(max(0, min(100, value)))
        finally:
            self.volume_slider.blockSignals(False)

    def clear_info(self):
        self.track_title.setText("Нет трека")
        self._set_artist_names([])
        self.album_label.setText("")
        self.cover_label.setPixmap(QPixmap())
        self.time_label.setText("0:00")
        self.duration_label.setText("0:00")
        self.progress_slider.setValue(0)
        self.set_like_state(False, enabled=False)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_slider_pressed(self):
        self._slider_down = True

    def _on_slider_released(self):
        self._slider_down = False

    def _on_album_clicked(self):
        if self.current_album_title:
            self.album_clicked.emit(self.current_album_title, self.current_artist_name)
