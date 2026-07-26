"""
Main window for Memify Player.

Architecture: QStackedWidget holds pre-built pages that are populated lazily.
Switching pages = setCurrentWidget() with no layout rebuild.
"""

import os
import re
import sys
import json
import time
import threading
from functools import partial

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea, QGridLayout,
    QLineEdit, QApplication, QSizePolicy, QStackedWidget,
    QMenu, QFileDialog, QProgressDialog, QSpacerItem,
    QAbstractItemView, QFrame, QMessageBox, QGraphicsDropShadowEffect,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsBlurEffect,
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, QUrl, QSize, QRectF, pyqtSignal, pyqtSlot, QObject, QPoint, QPointF,
    QPropertyAnimation, pyqtProperty, QEasingCurve,
)
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QFontMetrics, QCursor, QPainter, QPainterPath, QPen, QBrush

from config import SERVER_URL, APP_ICON, ICONS_DIR, APP_SETTINGS_FILE, PLAYER_DATA_CACHE_DIR, LIBRARY_CACHE_FILE, WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT, DATA_DIR
from core.library import LibraryManager, SearchResult
from core.player import PlayerController
try:
    from core.account import AccountManager as _AccountManager
except ImportError:
    _AccountManager = None
from ui.playback_controls import PlaybackControls, ClickableLabel
from ui.album_widget import AlbumWidget
from ui.shimmer_placeholder import ShimmerLabel
import ui.styles as styles_module
from ui.styles import COLORS, get_scrollbar_style, set_accent_color, set_theme, get_theme
from utils.format_utils import clean_title, clean_artist_name, format_duration, normalize_track_url
from utils.image_utils import make_rounded_pixmap, load_pixmap_from_url
from utils.cover_cache import cover_cache, cache_key
from workers.image_loader import ImageLoaderWorker
from workers.search_worker import SearchWorker
from workers.download_worker import DownloadWorker
from workers.track_duration_worker import TrackDurationWorker

try:
    from utils.media_keys import MediaKeysHandler
    _MEDIA_KEYS_AVAILABLE = True
except ImportError:
    _MEDIA_KEYS_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Flow layout: wraps widgets like CSS flexbox row
# ──────────────────────────────────────────────────────────────────────────────

class _FlowLayout(QHBoxLayout):
    """A simple wrapping grid layout for album cards."""


def _start_image_loader(
    urls: list[str],
    size: int,
    radius: int,
    on_loaded_cb,
    runners_store: list,          # list of (thread, worker); caller keeps this alive
):
    """Spawn an ImageLoaderWorker thread and register it in runners_store.

    Both thread AND worker are stored so neither is garbage-collected while running.
    The entry is removed automatically when the thread finishes.
    """
    worker = ImageLoaderWorker(urls, size=size, radius=radius)
    # Parented to the app (not left parent-less) so Qt's own C++ ownership keeps
    # the QThread alive even if the widget that started it (and its runners_store
    # list) gets torn down before the thread finishes — otherwise Python can drop
    # the last reference to a still-running QThread and Qt aborts the process.
    thread = QThread(QApplication.instance())
    worker.moveToThread(thread)

    entry = [thread, worker]      # mutable so we can mutate before cleanup
    runners_store.append(entry)

    def _cleanup():
        worker.deleteLater()
        thread.deleteLater()
        try:
            runners_store.remove(entry)
        except ValueError:
            pass

    worker.image_loaded.connect(on_loaded_cb)
    worker.finished.connect(thread.quit)
    thread.finished.connect(_cleanup)
    thread.started.connect(worker.run)
    thread.start()
    return entry


_dying_threads: list = []  # Keep Python refs alive until threads actually finish


def _stop_runners(runners_store: list):
    """Signal all runners to stop — keeps Python refs alive until threads finish."""
    for entry in list(runners_store):
        thread, worker = entry[0], entry[1]
        try:
            worker._stop = True
        except Exception:
            pass
        try:
            thread.quit()
            _dying_threads.append(thread)
            thread.finished.connect(
                lambda t=thread: _dying_threads.remove(t) if t in _dying_threads else None
            )
        except Exception:
            pass
    runners_store.clear()


def _stop_runners_and_wait(runners_store: list, timeout_ms: int = 300):
    """Like _stop_runners, but also blocks briefly for each thread to actually
    finish. Used on app close — a thread already parented to QApplication
    would otherwise just keep running past window close (each still-in-flight
    HTTP request blocks up to its own timeout), and get torn down mid-flight
    when the process exits, aborting it. Real requests almost always wrap up
    within this window; it only fails to fully reap threads stuck on a
    genuinely hung connection, which is a pre-existing, separate risk."""
    _stop_runners(runners_store)
    for thread in list(_dying_threads):
        try:
            thread.wait(timeout_ms)
        except Exception:
            pass


def _blurred_backdrop(pixmap: QPixmap, radius: float = 28.0, tint_alpha: int = 130, downscale: int = 4) -> QPixmap:
    """Frosted-glass style backdrop: downscale (for speed + softer blur),
    blur via QGraphicsBlurEffect, scale back up, then darken with a translucent tint."""
    if pixmap.isNull():
        return pixmap
    try:
        small = pixmap.scaled(
            max(1, pixmap.width() // downscale), max(1, pixmap.height() // downscale),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(small)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(radius)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        blurred_small = QPixmap(small.size())
        blurred_small.fill(Qt.GlobalColor.transparent)
        painter = QPainter(blurred_small)
        scene.render(painter, QRectF(), QRectF(0, 0, small.width(), small.height()))
        painter.end()

        result = blurred_small.scaled(
            pixmap.width(), pixmap.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(result)
        painter.fillRect(result.rect(), QColor(10, 10, 12, tint_alpha))
        painter.end()
        return result
    except Exception:
        return pixmap


def _track_like_keys(track: dict, url: str = "") -> set:
    """Keys identifying a track for like/playing-state matching: its URL
    variants, plus — when the track's album is shared across several artists
    (has an album_id) — album_id + normalized title. That second key lets a
    like or "now playing" state on one artist's copy of a duplicated track
    also match the other artist's identical copy."""
    keys = set()
    u = url or (track or {}).get("url", "") or ""
    if u:
        keys.add(u)
        if u.startswith("http"):
            keys.add(re.sub(r'^https?://[^/]+', '', u))
        else:
            keys.add(SERVER_URL + u)
    album_id = str((track or {}).get("album_id") or "").strip()
    title = (track or {}).get("title", "") or (track or {}).get("track_title", "") or ""
    if album_id and title:
        keys.add(f"id:{album_id}::{clean_title(title).strip().lower()}")
    return keys


def _liked_entry_matches(entry, keys: set) -> bool:
    if isinstance(entry, dict):
        return bool(_track_like_keys(entry, entry.get("url", "")) & keys)
    if isinstance(entry, str):
        return bool(_track_like_keys({}, entry) & keys)
    return False


class AlbumGridWidget(QWidget):
    """Scrollable grid of album cards. Content can be replaced via load_albums()."""
    album_clicked = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: list[AlbumWidget] = []
        self._runners: list = []          # [(thread, worker), ...]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(get_scrollbar_style())

        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(16)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

    def load_albums(self, albums: list[dict], artist: dict):
        """Clear existing cards and populate with new album data."""
        self._stop_image_loaders()
        self._clear_grid()
        self._scroll.verticalScrollBar().setValue(0)

        if not albums:
            placeholder = QLabel("Нет альбомов")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font-size: 14px;")
            self._grid.addWidget(placeholder, 0, 0)
            return

        cols = max(1, self._compute_cols())
        cover_urls = []

        for i, album in enumerate(albums):
            cover_url = ""
            if album.get("cover"):
                cover_url = SERVER_URL + album["cover"] if not album["cover"].startswith("http") else album["cover"]

            card = AlbumWidget(album, cover_url, widget_size=170, cover_size=150)
            card.clicked.connect(self.album_clicked.emit)

            row, col = divmod(i, cols)
            self._grid.addWidget(card, row, col, Qt.AlignmentFlag.AlignTop)
            self._cards.append(card)
            cover_urls.append(cover_url)

        self._load_covers(cover_urls)

    def _compute_cols(self) -> int:
        w = self._scroll.width() or self.width() or 800
        card_w = 170 + 16
        return max(1, (w - 16) // card_w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-flow cards when width changes
        QTimer.singleShot(0, self._reflow)

    def _reflow(self):
        if not self._cards:
            return
        cols = self._compute_cols()
        for i, card in enumerate(self._cards):
            row, col = divmod(i, cols)
            self._grid.removeWidget(card)
            self._grid.addWidget(card, row, col, Qt.AlignmentFlag.AlignTop)

    def _load_covers(self, urls: list[str]):
        valid_urls = [(i, u) for i, u in enumerate(urls) if u]
        if not valid_urls:
            return

        to_load = []
        for i, url in valid_urls:
            key = cache_key(url, 150, 14)
            cached = cover_cache.get(key)
            if cached and not cached.isNull() and i < len(self._cards):
                self._cards[i].set_cover(cached)
            else:
                to_load.append((i, url))

        if not to_load:
            return

        indices = [i for i, _ in to_load]
        load_urls = [u for _, u in to_load]

        def on_loaded(url: str, img, size: int, radius: int):
            if img is None:
                return
            try:
                pm = QPixmap.fromImage(img)
                if pm.isNull():
                    return
                cover_cache.set(cache_key(url, size, radius), pm)
                try:
                    card_idx = indices[load_urls.index(url)]
                    if card_idx < len(self._cards):
                        self._cards[card_idx].set_cover(pm)
                except (ValueError, IndexError):
                    pass
            except Exception:
                pass

        _start_image_loader(load_urls, 150, 14, on_loaded, self._runners)

    def _stop_image_loaders(self):
        _stop_runners(self._runners)

    def _clear_grid(self):
        for card in self._cards:
            self._grid.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        # Remove any placeholder labels
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()


# ──────────────────────────────────────────────────────────────────────────────
# Artist page
# ──────────────────────────────────────────────────────────────────────────────

class ArtistPage(QWidget):
    """Pre-built page showing an artist's albums grid."""
    album_clicked = pyqtSignal(dict, dict)  # (album, artist)
    artist_like_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_artist: dict = {}
        self._runners: list = []
        self._is_liked: bool = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 0)
        layout.setSpacing(16)

        # Header
        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(20)

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(120, 120)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setStyleSheet(
            f"background: {COLORS['SURFACE_LIGHT']}; border-radius: 60px;"
        )
        header_row.addWidget(self._cover_label, 0, Qt.AlignmentFlag.AlignVCenter)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)
        info_col.setContentsMargins(0, 0, 0, 0)

        type_label = QLabel("Исполнитель")
        type_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        info_col.addWidget(type_label)

        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_row.setContentsMargins(0, 0, 0, 0)

        self._name_label = QLabel()
        self._name_label.setWordWrap(False)
        name_font = QFont("Segoe UI", 22, QFont.Weight.Bold)
        self._name_label.setFont(name_font)
        self._name_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        name_row.addWidget(self._name_label, 1)

        self._artist_like_btn = QPushButton("♡")
        self._artist_like_btn.setFixedSize(34, 34)
        self._artist_like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._artist_like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._artist_like_btn.setToolTip("Подписаться на исполнителя")
        self._artist_like_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {COLORS['TEXT_SECONDARY']}; font-size: 20px; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        self._artist_like_btn.clicked.connect(self.artist_like_clicked.emit)
        name_row.addWidget(self._artist_like_btn)
        info_col.addLayout(name_row)

        self._album_count_label = QLabel()
        self._album_count_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        info_col.addWidget(self._album_count_label)

        info_col.addStretch(1)
        header_row.addLayout(info_col, 1)
        layout.addWidget(header, 0)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {COLORS['BORDER']};")
        layout.addWidget(divider, 0)

        albums_label = QLabel("Альбомы")
        albums_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        albums_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        layout.addWidget(albums_label, 0)

        # Album grid
        self._album_grid = AlbumGridWidget(self)
        self._album_grid.album_clicked.connect(self._on_album_clicked)
        layout.addWidget(self._album_grid, 1)

    def load_artist(self, artist: dict):
        """Update this page for a different artist. Called on every navigation."""
        self._current_artist = artist
        name = clean_artist_name(artist.get("artist", "") or "")
        albums = artist.get("albums", []) or []

        self._name_label.setText(name)
        count = len(albums)
        self._album_count_label.setText(
            f"{count} альбом" if count == 1 else
            f"{count} альбома" if 2 <= count <= 4 else
            f"{count} альбомов"
        )

        # Load artist cover
        cover_rel = artist.get("cover", "")
        if cover_rel:
            cover_url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
            self._load_artist_cover(cover_url)
        else:
            self._cover_label.setPixmap(QPixmap())

        # Load albums grid (clear + refill)
        self._album_grid.load_albums(albums, artist)

    def _load_artist_cover(self, url: str):
        key = cache_key(url, 120, 60)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._cover_label.setPixmap(cached)
            return

        _stop_runners(self._runners)

        def on_loaded(loaded_url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, size, radius), pm)
                    self._cover_label.setPixmap(pm)
            except Exception:
                pass

        _start_image_loader([url], 120, 60, on_loaded, self._runners)

    def _on_album_clicked(self, album: dict):
        self.album_clicked.emit(album, self._current_artist)

    def set_liked(self, liked: bool):
        self._is_liked = liked
        c = COLORS
        if liked:
            self._artist_like_btn.setText("♥")
            self._artist_like_btn.setToolTip("Отписаться от исполнителя")
            self._artist_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['PRIMARY']}; font-size: 20px; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self._artist_like_btn.setText("♡")
            self._artist_like_btn.setToolTip("Подписаться на исполнителя")
            self._artist_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; font-size: 20px; }}"
                f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
            )

    def apply_accent(self):
        self.set_liked(self._is_liked)


# ──────────────────────────────────────────────────────────────────────────────
# Album / tracklist page
# ──────────────────────────────────────────────────────────────────────────────

class TrackRow(QWidget):
    """Single row in the tracklist."""
    play_requested = pyqtSignal(int)  # track index
    download_requested = pyqtSignal(int)  # track index
    like_clicked = pyqtSignal(int)  # track index

    def __init__(self, index: int, track: dict, parent=None, display_number: int | None = None):
        super().__init__(parent)
        self._index = index
        self._track = track
        self._display_number = display_number if display_number is not None else index + 1
        self._liked = False
        self._is_playing_state = False
        self.setObjectName("trackRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QWidget#trackRow { background: transparent; border-radius: 6px; }"
            f"QWidget#trackRow:hover {{ background-color: {COLORS['SURFACE_LIGHT']}; }}"
        )
        self._build_ui()

    def _build_ui(self):
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        self._num_label = QLabel(str(self._display_number))
        self._num_label.setFixedWidth(24)
        self._num_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._num_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';")
        row.addWidget(self._num_label)

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(36, 36)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 4px;")
        self._cover_label.setVisible(False)
        row.addWidget(self._cover_label)

        title = clean_title(self._track.get("title", "")) or "Неизвестно"
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        self._title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self._title_label, 1)

        artist_name = self._track.get("artist_name", "")
        if artist_name:
            self._artist_label = QLabel(clean_artist_name(artist_name))
            self._artist_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
            self._artist_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            row.addWidget(self._artist_label)

        self._like_btn = QPushButton("♡")
        self._like_btn.setFixedSize(28, 28)
        self._like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._like_btn.setToolTip("Нравится")
        self._like_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {COLORS['TEXT_SECONDARY']}; font-size: 14px; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        self._like_btn.clicked.connect(lambda: self.like_clicked.emit(self._index))
        row.addWidget(self._like_btn)

        duration_ms = self._track.get("duration", 0) or 0
        self._dur_label = QLabel(format_duration(duration_ms) if duration_ms else "")
        self._dur_label.setFixedWidth(40)
        self._dur_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._dur_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        row.addWidget(self._dur_label)

    def show_cover(self, show: bool):
        self._cover_label.setVisible(show)

    def set_show_artist(self, show: bool):
        if hasattr(self, "_artist_label"):
            self._artist_label.setVisible(show)

    def set_cover_pixmap(self, pm: QPixmap):
        if pm and not pm.isNull():
            self._cover_label.setPixmap(make_rounded_pixmap(pm, 36, 4))
        else:
            self._cover_label.setPixmap(QPixmap())

    def set_liked(self, liked: bool):
        self._liked = liked
        c = COLORS
        if liked:
            self._like_btn.setText("♥")
            self._like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['PRIMARY']}; font-size: 14px; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self._like_btn.setText("♡")
            self._like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; font-size: 14px; }}"
                f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
            )

    def track_url(self) -> str:
        url = self._track.get("url", "") or ""
        return url

    def track_identity_keys(self) -> set:
        return _track_like_keys(self._track)

    def set_playing(self, is_playing: bool):
        self._is_playing_state = is_playing
        accent = COLORS["PRIMARY"]
        self._num_label.setText("▶" if is_playing else str(self._display_number))
        self._num_label.setStyleSheet(
            f"color: {accent}; font: 10pt 'Segoe UI';" if is_playing
            else f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';"
        )
        self._title_label.setStyleSheet(
            f"color: {accent}; font: 10pt 'Segoe UI';" if is_playing
            else f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';"
        )

    def apply_accent(self):
        """Re-apply accent-dependent colors after the accent changes."""
        self.set_liked(self._liked)
        self.set_playing(self._is_playing_state)

    def update_duration(self, ms: int):
        self._dur_label.setText(format_duration(ms) if ms else "")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._index)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        dl_track = menu.addAction("↓  Скачать трек")
        dl_album = menu.addAction("↓  Скачать альбом")
        action = menu.exec(event.globalPos())
        if action == dl_track:
            self.download_requested.emit(self._index)
        elif action == dl_album:
            self.download_requested.emit(-1)


class AlbumPage(QWidget):
    """Pre-built page showing an album's tracklist."""
    track_play_requested = pyqtSignal(int, dict, dict)       # (track_idx, album, artist)
    artist_name_clicked = pyqtSignal(str)                     # artist name
    download_album_requested = pyqtSignal(dict, dict, str)   # (album, artist, folder)
    download_track_requested = pyqtSignal(dict, dict, int, str)  # (album, artist, track_idx, folder)
    track_like_clicked = pyqtSignal(dict)   # track dict
    album_like_clicked = pyqtSignal()
    cover_clicked = pyqtSignal(dict, dict)  # (album, artist)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_album: dict = {}
        self._current_artist: dict = {}
        self._track_rows: list[TrackRow] = []
        self._disc_headers: list[QWidget] = []
        self._album_liked: bool = False
        self._duration_thread: QThread | None = None
        self._duration_worker = None
        self._runners: list = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 0)
        layout.setSpacing(0)

        # Header: cover + album info
        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(24)

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(180, 180)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cover_label.setToolTip("Открыть обложку")
        self._cover_label.setStyleSheet(
            f"background: {COLORS['SURFACE_LIGHT']}; border-radius: 14px;"
        )
        self._cover_label.mousePressEvent = lambda e: (
            self.cover_clicked.emit(self._current_album, self._current_artist)
            if e.button() == Qt.MouseButton.LeftButton
            and self._current_album.get("cover")
            and not self._current_album.get("_is_liked_album")
            else None
        )
        header_row.addWidget(self._cover_label, 0, Qt.AlignmentFlag.AlignVCenter)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)
        info_col.setContentsMargins(0, 0, 0, 0)

        self._type_label = QLabel("Альбом")
        self._type_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        info_col.addWidget(self._type_label)

        self._album_name_label = QLabel()
        self._album_name_label.setWordWrap(True)
        self._album_name_label.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        self._album_name_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        info_col.addWidget(self._album_name_label)

        artist_row = QHBoxLayout()
        artist_row.setSpacing(8)
        artist_row.setContentsMargins(0, 0, 0, 0)
        self._artist_names_widget = QWidget()
        self._artist_names_layout = QHBoxLayout(self._artist_names_widget)
        self._artist_names_layout.setContentsMargins(0, 0, 0, 0)
        self._artist_names_layout.setSpacing(0)
        artist_row.addWidget(self._artist_names_widget)

        self._album_like_btn = QPushButton("♡")
        self._album_like_btn.setFixedSize(30, 30)
        self._album_like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._album_like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._album_like_btn.setToolTip("Сохранить альбом")
        self._album_like_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {COLORS['TEXT_SECONDARY']}; font-size: 18px; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        self._album_like_btn.clicked.connect(self.album_like_clicked.emit)
        artist_row.addWidget(self._album_like_btn)
        artist_row.addStretch(1)
        info_col.addLayout(artist_row)

        self._track_count_label = QLabel()
        self._track_count_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        info_col.addWidget(self._track_count_label)

        # Play all button
        self._play_all_btn = QPushButton("▶  Слушать")
        self._play_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_all_btn.setFixedHeight(36)
        self._play_all_btn.clicked.connect(lambda: self.track_play_requested.emit(0, self._current_album, self._current_artist))
        c = COLORS
        self._play_all_btn.setStyleSheet(
            f"QPushButton {{ background: {c['PRIMARY']}; border: none; border-radius: 18px; "
            f"color: #000; font: 10pt 'Segoe UI'; font-weight: 600; padding: 0 24px; }}"
            f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
        )
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 8, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addWidget(self._play_all_btn)

        dl_btn = QPushButton("↓ Скачать альбом")
        dl_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dl_btn.setFixedHeight(36)
        dl_btn.clicked.connect(self._on_download_album)
        dl_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {c['BORDER']}; border-radius: 18px; "
            f"color: {c['TEXT_PRIMARY']}; font: 10pt 'Segoe UI'; padding: 0 16px; }}"
            f"QPushButton:hover {{ border-color: {c['TEXT_PRIMARY']}; }}"
        )
        btn_row.addWidget(dl_btn)
        btn_row.addStretch(1)

        info_col.addLayout(btn_row)
        info_col.addStretch(1)
        header_row.addLayout(info_col, 1)
        layout.addWidget(header, 0)

        # Divider
        layout.addSpacing(12)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {COLORS['BORDER']};")
        layout.addWidget(divider, 0)

        # Column headers
        col_hdr = QHBoxLayout()
        col_hdr.setContentsMargins(8, 6, 8, 2)
        col_hdr.setSpacing(8)
        num_hdr = QLabel("#")
        num_hdr.setFixedWidth(24)
        num_hdr.setAlignment(Qt.AlignmentFlag.AlignRight)
        num_hdr.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        col_hdr.addWidget(num_hdr)
        title_hdr = QLabel("Название")
        title_hdr.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        col_hdr.addWidget(title_hdr, 1)
        like_hdr = QLabel()
        like_hdr.setFixedWidth(28)
        col_hdr.addWidget(like_hdr)
        dur_hdr = QLabel("Время")
        dur_hdr.setFixedWidth(40)
        dur_hdr.setAlignment(Qt.AlignmentFlag.AlignRight)
        dur_hdr.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        col_hdr.addWidget(dur_hdr)
        layout.addLayout(col_hdr)

        # Track list scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(get_scrollbar_style())
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._tracks_container = QWidget()
        self._tracks_layout = QVBoxLayout(self._tracks_container)
        self._tracks_layout.setContentsMargins(0, 4, 0, 16)
        self._tracks_layout.setSpacing(2)
        self._tracks_layout.addStretch(1)

        self._scroll.setWidget(self._tracks_container)
        layout.addWidget(self._scroll, 1)

    def _set_artist_names(self, names: list):
        """Render one separately-clickable button per artist, comma-separated."""
        layout = self._artist_names_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        c = COLORS
        for i, name in enumerate(names):
            btn = QPushButton(name)
            btn.setFlat(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; "
                f"font: 10pt 'Segoe UI'; text-align: left; padding: 0; }}"
                f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; text-decoration: underline; }}"
            )
            btn.clicked.connect(lambda checked=False, n=name: self.artist_name_clicked.emit(n))
            layout.addWidget(btn)
            if i < len(names) - 1:
                sep = QLabel(", ")
                sep.setStyleSheet(f"color: {c['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';")
                layout.addWidget(sep)
        self._artist_names_widget.setVisible(bool(names))

    def load_album(self, album: dict, artist: dict, playing_url: str = "", display_artist_names: list | None = None,
                    playing_track: dict | None = None):
        """Update this page for a different album."""
        self._current_album = album
        self._current_artist = artist
        self._stop_duration_loader()

        is_liked_album = bool(album.get("_is_liked_album"))

        album_name = clean_title(album.get("title", "")) or "Неизвестно"
        artist_name = clean_artist_name(artist.get("artist", "")) or ""
        tracks = album.get("tracks", []) or []

        self._album_name_label.setText(album_name)
        # The "liked tracks" virtual album isn't a real album and has no
        # real artist — showing the "Альбом" type label and an artist chip
        # that just reads "Неизвестно" (clean_artist_name's fallback for an
        # empty name) is pure noise here, so both are hidden for it.
        self._type_label.setVisible(not is_liked_album)
        names = [] if is_liked_album else (
            display_artist_names if display_artist_names else ([artist_name] if artist_name else [])
        )
        self._set_artist_names(names)

        self._cover_label.setCursor(
            Qt.CursorShape.ArrowCursor if is_liked_album else Qt.CursorShape.PointingHandCursor
        )
        self._cover_label.setToolTip("" if is_liked_album else "Открыть обложку")

        count = len(tracks)
        self._track_count_label.setText(
            f"{count} трек" if count == 1 else
            f"{count} трека" if 2 <= count <= 4 else
            f"{count} треков"
        )

        # Album cover
        cover_rel = album.get("cover", "")
        if cover_rel:
            if os.path.isabs(cover_rel) and os.path.exists(cover_rel):
                pm = QPixmap(cover_rel)
                if not pm.isNull():
                    self._cover_label.setPixmap(make_rounded_pixmap(pm, 180, 14))
                else:
                    self._cover_label.setPixmap(QPixmap())
            else:
                cover_url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
                self._load_album_cover(cover_url)
        else:
            self._cover_label.setPixmap(QPixmap())

        # Rebuild track list — batch all insertions with updates disabled
        self._clear_tracks()
        self._scroll.verticalScrollBar().setValue(0)

        discs = album.get("discs") or []
        show_disc_headers = len(discs) > 1
        disc_titles = {d.get("number"): d.get("title") for d in discs}
        last_disc_number = None
        urls_needing_duration = []
        self._tracks_container.setUpdatesEnabled(False)
        try:
            for i, track in enumerate(tracks):
                disc_number = track.get("disc_number")
                if show_disc_headers and disc_number != last_disc_number:
                    disc_title = disc_titles.get(disc_number) or f"Диск {disc_number}"
                    header = self._make_disc_header(disc_title)
                    self._tracks_layout.insertWidget(self._tracks_layout.count() - 1, header)
                    self._disc_headers.append(header)
                    last_disc_number = disc_number

                display_number = track.get("disc_track_number") if show_disc_headers else None
                if is_liked_album:
                    display_track = {k: v for k, v in track.items() if k != "artist_name"}
                    row = TrackRow(i, display_track, display_number=display_number)
                else:
                    row = TrackRow(i, track, display_number=display_number)
                row.play_requested.connect(lambda idx, al=album, ar=artist: self.track_play_requested.emit(idx, al, ar))
                row.download_requested.connect(self._on_track_download_requested)
                row.like_clicked.connect(lambda idx, t=track: self.track_like_clicked.emit(t))
                self._tracks_layout.insertWidget(self._tracks_layout.count() - 1, row)
                self._track_rows.append(row)

                if is_liked_album:
                    row.show_cover(True)
                    cover_rel = track.get("_real_album_cover", "")
                    if cover_rel:
                        self._load_track_cover(row, cover_rel)

                if not track.get("duration"):
                    url = SERVER_URL + track.get("url", "") if track.get("url") else ""
                    if url:
                        urls_needing_duration.append((i, url))
        finally:
            self._tracks_container.setUpdatesEnabled(True)

        if urls_needing_duration:
            self._start_duration_loader([(i, u) for i, u in urls_needing_duration])

        if playing_url or playing_track:
            self.mark_playing_url(playing_url, playing_track)

    def mark_playing(self, track_idx: int):
        for i, row in enumerate(self._track_rows):
            row.set_playing(i == track_idx)

    def mark_playing_url(self, url: str, track: dict | None = None):
        """Highlight the row whose track matches url — or, for albums shared
        across artists, the same album_id + normalized title; clear all others."""
        keys = _track_like_keys(track or {}, url)
        for row in self._track_rows:
            row.set_playing(bool(keys) and bool(row.track_identity_keys() & keys))

    def _load_album_cover(self, url: str):
        key = cache_key(url, 180, 14)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._cover_label.setPixmap(cached)
            return

        _stop_runners(self._runners)

        def on_loaded(loaded_url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, size, radius), pm)
                    self._cover_label.setPixmap(pm)
            except Exception:
                pass

        _start_image_loader([url], 180, 14, on_loaded, self._runners)

    def _load_track_cover(self, row: 'TrackRow', cover_rel: str):
        url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
        key = cache_key(url, 36, 4)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            row.set_cover_pixmap(cached)
            return

        def on_loaded(loaded_url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, size, radius), pm)
                    row.set_cover_pixmap(pm)
            except Exception:
                pass

        _start_image_loader([url], 36, 4, on_loaded, self._runners)

    def _clear_tracks(self):
        for row in self._track_rows:
            self._tracks_layout.removeWidget(row)
            row.deleteLater()
        self._track_rows.clear()
        for header in self._disc_headers:
            self._tracks_layout.removeWidget(header)
            header.deleteLater()
        self._disc_headers.clear()

    def _make_disc_header(self, title: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 14, 8, 6)
        lay.setSpacing(8)
        icon = QLabel("◎")
        icon.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font-size: 13px;")
        lay.addWidget(icon)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 600 10pt 'Segoe UI';")
        lay.addWidget(lbl)
        lay.addStretch(1)
        return row

    def _start_duration_loader(self, index_url_pairs: list):
        urls = [u for _, u in index_url_pairs]
        idx_map = {u: i for i, u in index_url_pairs}

        worker = TrackDurationWorker(urls)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)

        # Keep worker alive alongside thread
        self._duration_worker = worker
        self._duration_thread = thread

        def on_duration(url: str, ms: int):
            try:
                if ms <= 0:
                    return
                idx = idx_map.get(url)
                if idx is not None and idx < len(self._track_rows):
                    self._track_rows[idx].update_duration(ms)
                    tracks = self._current_album.get("tracks", [])
                    if 0 <= idx < len(tracks) and not tracks[idx].get("duration"):
                        tracks[idx]["duration"] = ms
            except Exception:
                pass

        worker.duration_ready.connect(on_duration)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.start)
        thread.start()

    def _stop_duration_loader(self):
        if self._duration_thread and self._duration_worker:
            old_thread = self._duration_thread
            old_worker = self._duration_worker
            self._duration_thread = None
            self._duration_worker = None
            try:
                old_worker.duration_ready.disconnect()
            except Exception:
                pass
            try:
                old_worker.stop()
                old_thread.quit()
                _dying_threads.append(old_thread)
                old_thread.finished.connect(
                    lambda t=old_thread: _dying_threads.remove(t) if t in _dying_threads else None
                )
            except Exception:
                pass

    def _on_download_album(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку для скачивания")
        if not folder:
            return
        self.download_album_requested.emit(self._current_album, self._current_artist, folder)

    def _on_track_download_requested(self, track_idx: int):
        if track_idx == -1:
            self._on_download_album()
            return
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку для скачивания")
        if not folder:
            return
        self.download_track_requested.emit(self._current_album, self._current_artist, track_idx, folder)

    def set_album_liked(self, liked: bool):
        self._album_liked = liked
        c = COLORS
        if liked:
            self._album_like_btn.setText("♥")
            self._album_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['PRIMARY']}; font-size: 18px; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self._album_like_btn.setText("♡")
            self._album_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {c['TEXT_SECONDARY']}; font-size: 18px; }}"
                f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; }}"
            )

    def set_track_liked(self, track: dict, liked: bool):
        keys = _track_like_keys(track)
        for row in self._track_rows:
            if row.track_identity_keys() & keys:
                row.set_liked(liked)
                break

    def refresh_track_likes(self, liked_keys: set):
        for row in self._track_rows:
            row.set_liked(bool(row.track_identity_keys() & liked_keys))

    def apply_accent(self):
        c = COLORS
        self._play_all_btn.setStyleSheet(
            f"QPushButton {{ background: {c['PRIMARY']}; border: none; border-radius: 18px; "
            f"color: #000; font: 10pt 'Segoe UI'; font-weight: 600; padding: 0 24px; }}"
            f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
        )
        self.set_album_liked(self._album_liked)
        for row in self._track_rows:
            row.apply_accent()


# ──────────────────────────────────────────────────────────────────────────────
# Full-window cover viewer overlay
# ──────────────────────────────────────────────────────────────────────────────

class CoverViewerOverlay(QWidget):
    """Full-window dark overlay showing an enlarged album cover, with a
    button to save it to disk. Parented directly to the main window so it
    covers everything (sidebar, player bar, etc), not just the album page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._album: dict = {}
        self._artist: dict = {}
        self._full_pixmap: QPixmap | None = None
        self._backdrop_pixmap: QPixmap | None = None
        self._runners: list = []
        self.hide()
        self._build_ui()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._backdrop_pixmap and not self._backdrop_pixmap.isNull():
            painter.drawPixmap(self.rect(), self._backdrop_pixmap)
        else:
            painter.fillRect(self.rect(), QColor(10, 10, 12, 210))
        painter.end()
        super().paintEvent(event)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 20, 24, 0)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(36, 36)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); border: none; border-radius: 18px; "
            "color: #FFFFFF; font-size: 15px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }"
        )
        self._close_btn.clicked.connect(self.hide_viewer)
        close_row.addWidget(self._close_btn)
        outer.addLayout(close_row)

        outer.addStretch(1)

        center = QVBoxLayout()
        center.setSpacing(18)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setFixedSize(420, 420)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 18)
        shadow.setColor(QColor(0, 0, 0, 180))
        self._image_label.setGraphicsEffect(shadow)
        center.addWidget(self._image_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._caption_label = QLabel()
        self._caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption_label.setStyleSheet("color: #FFFFFF; font: 600 12pt 'Segoe UI'; background: transparent;")
        center.addWidget(self._caption_label)

        self._download_btn = QPushButton("↓  Скачать обложку")
        self._download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_btn.setFixedHeight(38)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download_clicked)
        center.addWidget(self._download_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addLayout(center)
        outer.addStretch(2)
        self.apply_accent()

    def apply_accent(self):
        c = COLORS
        self._download_btn.setStyleSheet(
            f"QPushButton {{ background: {c['PRIMARY']}; border: none; border-radius: 19px; "
            f"color: #000; font: 600 10.5pt 'Segoe UI'; padding: 0 22px; }}"
            f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
            f"QPushButton:disabled {{ background: {c['SURFACE_LIGHT']}; color: {c['TEXT_SECONDARY']}; }}"
        )

    def mousePressEvent(self, event):
        # Clicks land here only when they miss every child widget — i.e. the
        # dim backdrop itself (the image/buttons/caption consume their own clicks).
        if event.button() == Qt.MouseButton.LeftButton:
            self.hide_viewer()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide_viewer()
            return
        super().keyPressEvent(event)

    def hide_viewer(self):
        _stop_runners(self._runners)
        self.hide()

    def show_for(self, album: dict, artist: dict):
        self._album = album or {}
        self._artist = artist or {}
        self._full_pixmap = None
        self._download_btn.setEnabled(False)

        album_title = clean_title(self._album.get("title", "")) or "Альбом"
        artist_name = clean_artist_name(self._artist.get("artist", "")) or ""
        self._caption_label.setText(f"{album_title} • {artist_name}" if artist_name else album_title)

        self._image_label.setPixmap(QPixmap())
        self._image_label.setText("Загрузка…")
        self._set_placeholder_style()

        if self.parent():
            self.setGeometry(self.parent().rect())
            # Grab the app's current look (while we're still hidden) and
            # frost it into a blurred, tinted backdrop for the overlay.
            snapshot = self.parent().grab()
            self._backdrop_pixmap = _blurred_backdrop(snapshot)
        self.raise_()
        self.show()
        self.setFocus()
        self.update()

        cover_rel = self._album.get("cover", "")
        if not cover_rel:
            self._image_label.setText("Нет обложки")
            return

        if os.path.isabs(cover_rel) and os.path.exists(cover_rel):
            self._apply_pixmap(QPixmap(cover_rel))
            return

        url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
        _stop_runners(self._runners)

        def on_loaded(_loaded_url, img, _size, _radius):
            self._apply_pixmap(QPixmap.fromImage(img) if img else QPixmap())

        # size=0, radius=0 → fetch the original full-resolution image, unscaled/uncropped.
        _start_image_loader([url], 0, 0, on_loaded, self._runners)

    def _set_placeholder_style(self):
        self._image_label.setStyleSheet(
            f"background: {COLORS['SURFACE']}; border-radius: 16px; "
            f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';"
        )

    def _apply_pixmap(self, pm: QPixmap):
        if not pm or pm.isNull():
            self._image_label.setText("Не удалось загрузить обложку")
            return
        self._full_pixmap = pm
        self._download_btn.setEnabled(True)
        self._image_label.setStyleSheet("background: transparent; border-radius: 16px;")
        self._resize_image()

    def _resize_image(self):
        if not self._full_pixmap:
            return
        w, h = self.width(), self.height()
        side = max(240, min(640, (min(w - 160, h - 260)) if w and h else 420))
        self._image_label.setFixedSize(side, side)
        scaled = self._full_pixmap.scaled(
            side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._image_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())
        self._resize_image()

    def _on_download_clicked(self):
        if not self._full_pixmap:
            return
        album_title = clean_title(self._album.get("title", "")) or "album"
        artist_name = clean_artist_name(self._artist.get("artist", "")) or "artist"
        suggested = f"{_safe_filename(artist_name)} - {_safe_filename(album_title)}.png"
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить обложку альбома", suggested,
            "Изображения (*.png *.jpg *.jpeg);;Все файлы (*)"
        )
        if file_path:
            self._full_pixmap.save(file_path)


# ──────────────────────────────────────────────────────────────────────────────
# Now-playing spinning disc overlay
# ──────────────────────────────────────────────────────────────────────────────

class _SpinningDisc(QWidget):
    """A circular, rotating cover image — spins while playing, holds its
    angle while paused (resumes from there, doesn't reset to 0)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._pixmap: QPixmap | None = None
        self._backdrop: QPixmap | None = None
        self._backdrop_offset = QPoint(0, 0)
        self._angle = 0.0
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(9000)
        self._anim.setLoopCount(-1)

    def _get_angle(self):
        return self._angle

    def _set_angle(self, value):
        self._angle = float(value)
        self.update()

    angle = pyqtProperty(float, fget=_get_angle, fset=_set_angle)

    def set_pixmap(self, pm: QPixmap | None):
        self._pixmap = pm
        self.update()

    def set_backdrop(self, pixmap: QPixmap | None, offset: QPoint):
        """Backdrop image (and this disc's position within it), used to make
        the spindle hole show what's behind the disc instead of a flat
        color."""
        self._backdrop = pixmap
        self._backdrop_offset = offset
        self.update()

    def start_spin(self):
        state = self._anim.state()
        if state == QPropertyAnimation.State.Paused:
            self._anim.resume()
        elif state == QPropertyAnimation.State.Stopped:
            self._anim.start()

    def stop_spin(self):
        if self._anim.state() == QPropertyAnimation.State.Running:
            self._anim.pause()

    def reset(self):
        self._anim.stop()
        self._angle = 0.0
        self.update()

    def paintEvent(self, event):
        if not self._pixmap or self._pixmap.isNull():
            return
        side = min(self.width(), self.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        painter.rotate(self._angle)
        painter.translate(-side / 2.0, -side / 2.0)
        path = QPainterPath()
        path.addEllipse(0, 0, side, side)
        painter.setClipPath(path)
        scaled = self._pixmap.scaled(
            side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
        )
        x = (side - scaled.width()) / 2.0
        y = (side - scaled.height()) / 2.0
        painter.drawPixmap(int(x), int(y), scaled)

        # Punch a spindle hole through the middle, like a real record/CD.
        # CompositionMode_Clear only produces real transparency on a surface
        # with an alpha channel — this widget is a plain (non-top-level)
        # child painting into its ancestor's opaque backing store, so
        # "clearing" here just wrote solid black instead of a see-through
        # hole. Since the disc spins around its own center, the hole always
        # lands at the widget's exact center regardless of rotation — reset
        # the transform and paint the matching crop of the overlay's
        # backdrop image there instead, so it looks like a real hole.
        painter.resetTransform()
        painter.setClipping(False)
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        hole_r = max(6.0, side * 0.07)
        hole_rect = QRectF(center.x() - hole_r, center.y() - hole_r, hole_r * 2, hole_r * 2)
        if self._backdrop and not self._backdrop.isNull():
            src_rect = hole_rect.translated(self._backdrop_offset.x(), self._backdrop_offset.y())
            hole_path = QPainterPath()
            hole_path.addEllipse(center, hole_r, hole_r)
            painter.setClipPath(hole_path)
            painter.drawPixmap(hole_rect, self._backdrop, src_rect)
            painter.setClipping(False)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(10, 10, 12, 220))
            painter.drawEllipse(center, hole_r, hole_r)

        # Thin rim around the hole for a bit of definition.
        painter.setPen(QColor(255, 255, 255, 60))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, hole_r, hole_r)
        painter.end()


class NowPlayingDiscOverlay(QWidget):
    """Minimal full-window overlay: nothing but a spinning cover disc on a
    blurred backdrop. No caption, no buttons, no download — click anywhere
    (or Escape) to close."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backdrop_pixmap: QPixmap | None = None
        self._runners: list = []
        self.hide()
        self._disc = _SpinningDisc(self)
        self._disc.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._backdrop_pixmap and not self._backdrop_pixmap.isNull():
            painter.drawPixmap(self.rect(), self._backdrop_pixmap)
        else:
            painter.fillRect(self.rect(), QColor(10, 10, 12, 220))
        painter.end()
        super().paintEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.hide_viewer()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide_viewer()
            return
        super().keyPressEvent(event)

    def hide_viewer(self):
        _stop_runners(self._runners)
        self._disc.stop_spin()
        self.hide()

    def _layout_disc(self):
        side = max(160, int(min(self.width(), self.height()) * 0.55))
        self._disc.setFixedSize(side, side)
        self._disc.move((self.width() - side) // 2, (self.height() - side) // 2)
        self._disc.set_backdrop(self._backdrop_pixmap, self._disc.pos())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())
        self._layout_disc()

    def show_for(self, cover_rel: str, is_playing: bool):
        self._disc.reset()
        self._disc.set_pixmap(None)

        if self.parent():
            self.setGeometry(self.parent().rect())
            snapshot = self.parent().grab()
            self._backdrop_pixmap = _blurred_backdrop(snapshot)
        self._layout_disc()
        self.raise_()
        self.show()
        self.setFocus()
        self.update()

        if not cover_rel:
            return

        if os.path.isabs(cover_rel) and os.path.exists(cover_rel):
            self._disc.set_pixmap(QPixmap(cover_rel))
            self.set_playing(is_playing)
            return

        url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
        _stop_runners(self._runners)

        def on_loaded(_url, img, _size, _radius):
            self._disc.set_pixmap(QPixmap.fromImage(img) if img else QPixmap())
            self.set_playing(is_playing)

        # size=0, radius=0 → fetch the original full-resolution image.
        _start_image_loader([url], 0, 0, on_loaded, self._runners)

    def set_playing(self, is_playing: bool):
        if not self.isVisible():
            return
        if is_playing:
            self._disc.start_spin()
        else:
            self._disc.stop_spin()


class _LoadingSpinner(QWidget):
    """Small rotating arc spinner for inline loading states."""

    def __init__(self, parent=None, diameter: int = 20, line_width: int = 3):
        super().__init__(parent)
        self._diameter = diameter
        self._line_width = line_width
        self.setFixedSize(diameter, diameter)
        self._angle = 0.0
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(900)
        self._anim.setLoopCount(-1)
        self.hide()

    def _get_angle(self):
        return self._angle

    def _set_angle(self, value):
        self._angle = float(value)
        self.update()

    angle = pyqtProperty(float, fget=_get_angle, fset=_set_angle)

    def start(self):
        self.show()
        if self._anim.state() != QPropertyAnimation.State.Running:
            self._anim.start()

    def stop(self):
        self._anim.stop()
        self.hide()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = styles_module.COLORS
        pen = QPen(QColor(c["PRIMARY"]))
        pen.setWidth(self._line_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        half = self._line_width / 2.0
        rect = QRectF(half, half, self._diameter - self._line_width, self._diameter - self._line_width)
        span = 100 * 16
        start = int(-self._angle * 16)
        p.drawArc(rect, start, span)
        p.end()


# ──────────────────────────────────────────────────────────────────────────────
# Search page
# ──────────────────────────────────────────────────────────────────────────────

class SearchPage(QWidget):
    """Pre-built search results page."""
    result_selected = pyqtSignal(object)  # SearchResult

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: list[SearchResult] = []
        self._runners: list = []
        self._track_title_labels: list[tuple] = []  # (title_lbl, url_rel)
        self._loading = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 0)
        layout.setSpacing(12)

        hdr = QLabel("Результаты поиска")
        hdr.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        layout.addWidget(hdr, 0)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self._spinner = _LoadingSpinner(self)
        status_row.addWidget(self._spinner)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        status_row.addWidget(self._count_label)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(get_scrollbar_style())
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(4)
        self._results_layout.addStretch(1)

        self._scroll.setWidget(self._results_container)
        layout.addWidget(self._scroll, 1)

    def set_loading(self, loading: bool):
        self._loading = loading
        if loading:
            self._spinner.start()
            self._count_label.setText("Поиск…")
        else:
            self._spinner.stop()

    def update_results(self, results: list[SearchResult]):
        self.set_loading(False)
        self._results = results
        self._rebuild_list()

    def _rebuild_list(self):
        _stop_runners(self._runners)
        self._track_title_labels.clear()
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        self._scroll.verticalScrollBar().setValue(0)

        if not self._results:
            self._count_label.setText("Ничего не найдено")
            return

        artists = [r for r in self._results if r.type == "artist"][:3]
        albums  = [r for r in self._results if r.type == "album"][:5]
        tracks  = [r for r in self._results if r.type == "track"][:10]
        total = len(artists) + len(albums) + len(tracks)
        self._count_label.setText(f"Найдено: {total}")

        insert_pos = 0

        def _insert(w):
            nonlocal insert_pos
            self._results_layout.insertWidget(insert_pos, w)
            insert_pos += 1

        if artists:
            _insert(self._make_section_header("Исполнители"))
            for r in artists:
                _insert(self._make_artist_row(r))

        if albums:
            _insert(self._make_section_header("Альбомы"))
            for r in albums:
                _insert(self._make_album_row(r))

        if tracks:
            _insert(self._make_section_header("Треки"))
            for r in tracks:
                _insert(self._make_track_row(r))

    def _make_section_header(self, text: str) -> QWidget:
        lbl = QLabel(text)
        lbl.setContentsMargins(8, 12, 8, 4)
        lbl.setStyleSheet(
            f"color: {COLORS['TEXT_SECONDARY']}; font: bold 9pt 'Segoe UI'; "
            f"letter-spacing: 1px; text-transform: uppercase;"
        )
        return lbl

    def _load_cover_into(self, cover_url: str, label: QLabel, size: int, radius: int):
        if not cover_url:
            return
        full = (SERVER_URL + cover_url) if not cover_url.startswith("http") else cover_url
        key = cache_key(full, size, radius)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            label.setPixmap(cached)
            return

        def _cb(loaded_url, img, sz, rad, lbl=label):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, sz, rad), pm)
                    lbl.setPixmap(pm)
            except Exception:
                pass

        _start_image_loader([full], size, radius, _cb, self._runners)

    def _make_artist_row(self, result: SearchResult) -> QWidget:
        row = QWidget()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setObjectName("srRow")
        row.setStyleSheet(
            "QWidget#srRow { background: transparent; border-radius: 8px; }"
            f"QWidget#srRow:hover {{ background-color: {COLORS['SURFACE_LIGHT']}; }}"
        )
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(14)

        avatar = QLabel()
        avatar.setFixedSize(48, 48)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 24px;")
        self._load_cover_into(result.cover_url or "", avatar, 48, 24)
        lay.addWidget(avatar)

        name_lbl = QLabel(clean_artist_name(result.artist_name or ""))
        name_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: bold 11pt 'Segoe UI';")
        lay.addWidget(name_lbl, 1)

        def on_click(event, r=result):
            if event.button() == Qt.MouseButton.LeftButton:
                self.result_selected.emit(r)
        row.mousePressEvent = on_click
        return row

    def _make_album_row(self, result: SearchResult) -> QWidget:
        row = QWidget()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setObjectName("srRow")
        row.setStyleSheet(
            "QWidget#srRow { background: transparent; border-radius: 8px; }"
            f"QWidget#srRow:hover {{ background-color: {COLORS['SURFACE_LIGHT']}; }}"
        )
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(14)

        cover = QLabel()
        cover.setFixedSize(48, 48)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 6px;")
        self._load_cover_into(result.cover_url or "", cover, 48, 6)
        lay.addWidget(cover)

        txt = QVBoxLayout()
        txt.setSpacing(2)
        txt.setContentsMargins(0, 0, 0, 0)
        title_lbl = QLabel(clean_title(result.album_title or ""))
        title_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        txt.addWidget(title_lbl)
        artist_lbl = QLabel(result.artists_display())
        artist_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        txt.addWidget(artist_lbl)
        lay.addLayout(txt, 1)

        def on_click(event, r=result):
            if event.button() == Qt.MouseButton.LeftButton:
                self.result_selected.emit(r)
        row.mousePressEvent = on_click
        return row

    def _make_track_row(self, result: SearchResult) -> QWidget:
        row = QWidget()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setObjectName("srRow")
        row.setStyleSheet(
            "QWidget#srRow { background: transparent; border-radius: 8px; }"
            f"QWidget#srRow:hover {{ background-color: {COLORS['SURFACE_LIGHT']}; }}"
        )
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(12)

        cover = QLabel()
        cover.setFixedSize(40, 40)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 4px;")
        self._load_cover_into(result.cover_url or "", cover, 40, 4)
        lay.addWidget(cover)

        txt = QVBoxLayout()
        txt.setSpacing(1)
        txt.setContentsMargins(0, 0, 0, 0)
        title_lbl = QLabel(clean_title(result.track_title or ""))
        title_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        txt.addWidget(title_lbl)
        sub_parts = [result.artists_display()]
        if result.album_title:
            sub_parts.append(clean_title(result.album_title))
        sub_lbl = QLabel(" • ".join(sub_parts))
        sub_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        txt.addWidget(sub_lbl)
        lay.addLayout(txt, 1)

        row_keys = _track_like_keys(result.track_obj or {})
        self._track_title_labels.append((title_lbl, row_keys))

        def on_click(event, r=result):
            if event.button() == Qt.MouseButton.LeftButton:
                self.result_selected.emit(r)
        row.mousePressEvent = on_click
        return row

    def _make_result_row(self, result: SearchResult) -> QWidget:
        if result.type == "artist":
            return self._make_artist_row(result)
        if result.type == "album":
            return self._make_album_row(result)
        return self._make_track_row(result)

    def refresh_playing(self, url: str, track: dict | None = None):
        """Highlight track rows that match the currently playing track (by URL,
        or — for albums shared across artists — by album_id + normalized title)."""
        accent = COLORS["PRIMARY"]
        keys = _track_like_keys(track or {}, url)
        for title_lbl, row_keys in self._track_title_labels:
            is_playing = bool(keys) and bool(row_keys & keys)
            title_lbl.setStyleSheet(
                f"color: {accent}; font: 10pt 'Segoe UI';" if is_playing
                else f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';"
            )


# ──────────────────────────────────────────────────────────────────────────────
# All-artists browse page
# ──────────────────────────────────────────────────────────────────────────────

_LATIN_LETTERS = [chr(c) for c in range(ord('A'), ord('Z') + 1)]
_CYRILLIC_LETTERS = list("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
_ALPHABET_LETTERS = _LATIN_LETTERS + _CYRILLIC_LETTERS


def _artist_group_letter(name: str) -> str:
    # clean_artist_name() falls back to the literal word "Неизвестно" for
    # empty input (same as clean_title()) rather than returning "" — check
    # for an empty name before cleaning, or a nameless artist would get
    # miscategorized under "Н" instead of the "#" catch-all bucket.
    raw = (name or "").strip()
    if not raw:
        return "#"
    cleaned = clean_artist_name(raw).strip() or raw
    ch = cleaned[0].upper()
    return ch if ch in _ALPHABET_LETTERS else "#"


class AllArtistsPage(QWidget):
    """Full server artist catalog (independent of the user's own library/
    subscriptions), grouped A-Z / А-Я with a clickable jump index."""

    artist_selected = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runners: list = []
        self._section_headers: dict[str, QWidget] = {}
        self._index_buttons: dict[str, QLabel] = {}
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 16, 8, 0)
        root.setSpacing(4)

        left_col = QVBoxLayout()
        left_col.setSpacing(10)
        left_col.setContentsMargins(0, 0, 0, 0)

        hdr = QLabel("Все исполнители")
        hdr.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        left_col.addWidget(hdr)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        left_col.addWidget(self._count_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(get_scrollbar_style())
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 4, 8, 16)
        self._container_layout.setSpacing(2)
        self._container_layout.addStretch(1)

        self._scroll.setWidget(self._container)
        left_col.addWidget(self._scroll, 1)
        root.addLayout(left_col, 1)

        # Right: compact clickable A-Z / А-Я jump index — letters with no
        # artists are dimmed and inert, so the index stays useful even
        # though it always shows the full alphabet.
        index_col = QVBoxLayout()
        index_col.setContentsMargins(0, 40, 4, 16)
        index_col.setSpacing(0)
        for letter in _ALPHABET_LETTERS:
            lbl = QLabel(letter)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFixedHeight(11)
            lbl.setFont(QFont("Segoe UI", 7))
            lbl.setStyleSheet(f"color: {COLORS['BORDER']};")
            index_col.addWidget(lbl)
            self._index_buttons[letter] = lbl
        index_widget = QWidget()
        index_widget.setFixedWidth(18)
        index_widget.setLayout(index_col)
        root.addWidget(index_widget, 0)

    def load_artists(self, artists: list[dict]):
        _stop_runners(self._runners)
        groups: dict[str, list[dict]] = {}
        for a in artists:
            if not isinstance(a, dict):
                continue
            letter = _artist_group_letter(a.get("artist", ""))
            groups.setdefault(letter, []).append(a)
        for letter, items in groups.items():
            items.sort(key=lambda a: clean_artist_name(a.get("artist", "")).lower())

        total = sum(len(v) for v in groups.values())
        self._count_label.setText(
            f"{total} исполнитель" if total % 10 == 1 and total % 100 != 11 else
            f"{total} исполнителя" if 2 <= total % 10 <= 4 and not (11 <= total % 100 <= 14) else
            f"{total} исполнителей"
        )

        while self._container_layout.count() > 1:
            item = self._container_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._section_headers.clear()
        self._scroll.verticalScrollBar().setValue(0)

        insert_pos = 0

        def _insert(w):
            nonlocal insert_pos
            self._container_layout.insertWidget(insert_pos, w)
            insert_pos += 1

        ordered_letters = _ALPHABET_LETTERS + (["#"] if "#" in groups else [])
        for letter in ordered_letters:
            if letter not in groups:
                continue
            header = self._make_section_header(letter)
            _insert(header)
            self._section_headers[letter] = header
            for artist in groups[letter]:
                _insert(self._make_artist_row(artist))

        available = set(groups.keys())
        for letter, lbl in self._index_buttons.items():
            enabled = letter in available
            color = COLORS["TEXT_PRIMARY"] if enabled else COLORS["BORDER"]
            lbl.setStyleSheet(f"color: {color};")
            lbl.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
            if enabled:
                lbl.mousePressEvent = partial(self._on_index_clicked, letter)
            else:
                lbl.mousePressEvent = lambda e: None

    def _on_index_clicked(self, letter: str, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        header = self._section_headers.get(letter)
        if not header:
            return
        self._scroll.verticalScrollBar().setValue(max(0, header.pos().y() - 4))

    def _make_section_header(self, letter: str) -> QWidget:
        lbl = QLabel(letter)
        lbl.setContentsMargins(8, 14, 8, 4)
        lbl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {COLORS['PRIMARY']};")
        return lbl

    def _make_artist_row(self, artist: dict) -> QWidget:
        row = QWidget()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setObjectName("srRow")
        row.setStyleSheet(
            "QWidget#srRow { background: transparent; border-radius: 8px; }"
            f"QWidget#srRow:hover {{ background-color: {COLORS['SURFACE_LIGHT']}; }}"
        )
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 7, 8, 7)
        lay.setSpacing(14)

        avatar = QLabel()
        avatar.setFixedSize(42, 42)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 21px;")
        cover_rel = artist.get("cover", "")
        if cover_rel:
            full = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
            key = cache_key(full, 42, 21)
            cached = cover_cache.get(key)
            if cached and not cached.isNull():
                avatar.setPixmap(cached)
            else:
                def _cb(loaded_url, img, sz, rad, lbl=avatar):
                    try:
                        pm = QPixmap.fromImage(img) if img else QPixmap()
                        if not pm.isNull():
                            cover_cache.set(cache_key(loaded_url, sz, rad), pm)
                            lbl.setPixmap(pm)
                    except Exception:
                        pass
                _start_image_loader([full], 42, 21, _cb, self._runners)
        lay.addWidget(avatar)

        name_lbl = QLabel(clean_artist_name(artist.get("artist", "")))
        name_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10.5pt 'Segoe UI';")
        lay.addWidget(name_lbl, 1)

        def on_click(event, a=artist):
            if event.button() == Qt.MouseButton.LeftButton:
                self.artist_selected.emit(a)
        row.mousePressEvent = on_click
        return row


# ──────────────────────────────────────────────────────────────────────────────
# Welcome page
# ──────────────────────────────────────────────────────────────────────────────

class WelcomePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl = QLabel("Выберите исполнителя из списка")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setFont(QFont("Segoe UI", 14))
        lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
        layout.addWidget(lbl)


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────

_LIKED_ROW_ROLE = Qt.ItemDataRole.UserRole + 1
_ARTIST_ROW_ROLE = Qt.ItemDataRole.UserRole + 2


class _SidebarItem(QWidget):
    """Single item in the sidebar with a 40x40 cover + label."""
    clicked = pyqtSignal(object)

    COVER_SIZE = 40

    def __init__(self, text: str, data, radius: int = 6, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarItem")
        self._data = data
        self._radius = radius
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build_ui(text)
        self._set_style(hover=False, selected=False)

    def _build_ui(self, text: str):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(10)

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setStyleSheet(
            f"background: {COLORS['SURFACE_LIGHT']}; border-radius: {self._radius}px;"
        )
        lay.addWidget(self._cover_label)

        self._name_label = QLabel(text)
        self._name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._name_label.setWordWrap(False)
        lay.addWidget(self._name_label, 1)

    def _set_style(self, hover: bool, selected: bool):
        c = COLORS
        active = hover or selected
        bg = c["SURFACE_LIGHT"] if active else "transparent"
        color = c["TEXT_PRIMARY"] if active else c["TEXT_SECONDARY"]
        self.setStyleSheet(f"QWidget#sidebarItem {{ background: {bg}; border-radius: 8px; }}")
        self._name_label.setStyleSheet(f"color: {color}; font: 10pt 'Segoe UI';")

    def set_cover(self, pixmap: QPixmap):
        if pixmap and not pixmap.isNull():
            pm = make_rounded_pixmap(pixmap, self.COVER_SIZE, self._radius)
            self._cover_label.setPixmap(pm)
            # Clear background once we have a real cover
            self._cover_label.setStyleSheet(f"border-radius: {self._radius}px;")

    def set_cover_text(self, text: str, bg: str = ""):
        """Fallback when no image: show a symbol."""
        bg = bg or COLORS["SURFACE_LIGHT"]
        self._cover_label.setText(text)
        self._cover_label.setStyleSheet(
            f"background: {bg}; border-radius: {self._radius}px; "
            f"color: {COLORS['TEXT_PRIMARY']}; font-size: 18px; qproperty-alignment: AlignCenter;"
        )

    def set_selected(self, selected: bool):
        self._selected = selected
        self._set_style(hover=False, selected=selected)

    def enterEvent(self, event):
        self._set_style(hover=True, selected=self._selected)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_style(hover=False, selected=self._selected)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._data)
        super().mousePressEvent(event)


class Sidebar(QWidget):
    artist_selected = pyqtSignal(dict)
    album_selected = pyqtSignal(dict, dict)   # (album, artist)
    liked_tracks_selected = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(260)
        self.setStyleSheet(
            f"QWidget#sidebar {{ background-color: {COLORS['SURFACE']}; "
            f"border-right: 1px solid {COLORS['SURFACE_LIGHT']}; }}"
        )
        self._items: list[_SidebarItem] = []
        self._runners: list = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Username row
        self._user_row = QHBoxLayout()
        self._user_row.setContentsMargins(6, 0, 0, 0)
        self._user_label = QLabel("")
        self._user_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._user_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        self._user_row.addWidget(self._user_label)
        self._user_row.addStretch(1)
        layout.addLayout(self._user_row)

        header = QLabel("Библиотека")
        header.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        header.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; padding: 4px 6px;")
        layout.addWidget(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(get_scrollbar_style() + "QScrollArea { background: transparent; border: none; }")

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._items_layout = QVBoxLayout(self._container)
        self._items_layout.setContentsMargins(0, 0, 4, 0)
        self._items_layout.setSpacing(2)
        self._items_layout.addStretch(1)

        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll, 1)

    def set_username(self, login: str):
        self._user_label.setText(login or "")

    def load_account_content(self, liked: bool, artists: list[dict], albums: list = None):
        _stop_runners(self._runners)
        for item in self._items:
            self._items_layout.removeWidget(item)
            item.deleteLater()
        self._items.clear()

        self._add_item(self._make_liked_item())

        for a in artists:
            self._add_item(self._make_artist_item(a))

        for al, ar in (albums or []):
            self._add_item(self._make_album_item(al, ar))

    def _add_item(self, item: _SidebarItem):
        # Remove placeholder if present
        for i in range(self._items_layout.count()):
            w = self._items_layout.itemAt(i)
            if w and w.widget() and w.widget().objectName() == "_sidebar_placeholder":
                w.widget().deleteLater()
                break
        self._items_layout.insertWidget(self._items_layout.count() - 1, item)
        self._items.append(item)

    def _make_liked_item(self) -> _SidebarItem:
        item = _SidebarItem("Понравившиеся треки", "_liked_tracks_", radius=6)
        item.clicked.connect(lambda _: self.liked_tracks_selected.emit())
        icon_path = os.path.join(ICONS_DIR, "liked_icon.png")
        if os.path.exists(icon_path):
            pm = QPixmap(icon_path)
            if not pm.isNull():
                item.set_cover(pm)
                return item
        item.set_cover_text("♥", COLORS["PRIMARY"])
        return item

    def _make_artist_item(self, artist: dict) -> _SidebarItem:
        name = clean_artist_name(artist.get("artist", "")) or "Неизвестно"
        item = _SidebarItem(name, artist, radius=20)
        item.clicked.connect(lambda data: self.artist_selected.emit(data))
        cover_rel = artist.get("cover", "")
        if cover_rel:
            url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
            self._load_cover(item, url, 40, 20)
        else:
            item.set_cover_text("♪")
        return item

    def _make_album_item(self, album: dict, artist: dict) -> _SidebarItem:
        name = clean_title(album.get("title", "")) or "Неизвестно"
        item = _SidebarItem(name, (album, artist), radius=6)
        item.clicked.connect(lambda data: self.album_selected.emit(data[0], data[1]))
        cover_rel = album.get("cover", "")
        if cover_rel:
            url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
            self._load_cover(item, url, 40, 6)
        else:
            item.set_cover_text("♪")
        return item

    def _load_cover(self, item: _SidebarItem, url: str, size: int, radius: int):
        key = cache_key(url, size, radius)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            item.set_cover(cached)
            return

        def on_loaded(loaded_url, img, s, r):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, s, r), pm)
                    item.set_cover(pm)
            except Exception:
                pass

        _start_image_loader([url], size, radius, on_loaded, self._runners)

    def select_artist(self, artist_name: str):
        for item in self._items:
            if isinstance(item._data, dict) and item._data.get("artist") == artist_name:
                item.set_selected(True)
            else:
                item.set_selected(False)


# ──────────────────────────────────────────────────────────────────────────────
# Settings panel
# ──────────────────────────────────────────────────────────────────────────────

class _ToggleSwitch(QWidget):
    """iOS-style animated on/off switch, used in place of a plain QCheckBox
    for settings toggles."""

    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked = False
        self._knob_pos = 0.0  # 0.0 = off, 1.0 = on
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

    def _get_knob_pos(self):
        return self._knob_pos

    def _set_knob_pos(self, value):
        self._knob_pos = float(value)
        self.update()

    knobPos = pyqtProperty(float, fget=_get_knob_pos, fset=_set_knob_pos)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        checked = bool(checked)
        if self._checked == checked:
            return
        self._checked = checked
        self._animate_to(1.0 if checked else 0.0)

    def _animate_to(self, target: float):
        self._anim.stop()
        self._anim.setStartValue(self._knob_pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self._animate_to(1.0 if self._checked else 0.0)
            self.toggled.emit(self._checked)
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = styles_module.COLORS
        off = QColor(c["BORDER"])
        on = QColor(c["PRIMARY"])
        t = self._knob_pos
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )

        rect = QRectF(self.rect().adjusted(1, 1, -1, -1))
        radius = rect.height() / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(rect, radius, radius)

        knob_d = rect.height() - 4
        knob_x = rect.x() + 2 + (rect.width() - knob_d - 4) * t
        knob_y = rect.y() + 2
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(QRectF(knob_x, knob_y, knob_d, knob_d))
        p.end()

class SettingsPage(QWidget):
    logout_clicked = pyqtSignal()
    accent_changed = pyqtSignal(str)
    theme_changed = pyqtSignal(str)
    scale_changed = pyqtSignal(float)
    discord_rpc_toggled = pyqtSignal(bool)
    cache_cleared = pyqtSignal()

    SCALE_PRESETS = [("75%", 0.75), ("100%", 1.0), ("125%", 1.25), ("150%", 1.5)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._discord_toggle = None
        self._accent_btns: list[QPushButton] = []
        self._current_accent = ""
        self._theme_btns: dict[str, QPushButton] = {}
        self._current_theme = "dark"
        self._scale_btns: dict[float, QPushButton] = {}
        self._current_scale = 1.0
        self._build_ui()

    def _make_card(self, parent_layout: QVBoxLayout) -> QVBoxLayout:
        card = QWidget()
        card.setStyleSheet(
            f"QWidget {{ background: {COLORS['SURFACE']}; border-radius: 12px; }}"
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 16, 18, 16)
        inner.setSpacing(12)
        parent_layout.addWidget(card)
        return inner

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setStyleSheet(
            f"color: {COLORS['TEXT_SECONDARY']}; font: 600 8.5pt 'Segoe UI'; letter-spacing: 1px;"
        )
        return lbl

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(20)

        hdr = QLabel("Настройки")
        hdr.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        layout.addWidget(hdr)

        # ── Accent color card ────────────────────────────────────────────────
        accent_card = self._make_card(layout)
        accent_card.addWidget(self._section_label("Цвет акцента"))

        palette_row = QHBoxLayout()
        palette_row.setSpacing(10)
        palette_row.setContentsMargins(0, 0, 0, 0)

        for preset in styles_module.ACCENT_COLOR_PRESETS:
            color = preset["colors"][0]
            btn = QPushButton()
            btn.setFixedSize(32, 32)
            btn.setToolTip(preset["title"])
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("accentColor", color)
            btn.clicked.connect(partial(self._on_accent_clicked, color))
            palette_row.addWidget(btn)
            self._accent_btns.append(btn)

        palette_row.addStretch(1)
        accent_card.addLayout(palette_row)
        self._restyle_accent_buttons()

        # ── Appearance card (theme + UI scale) ──────────────────────────────
        appearance_card = self._make_card(layout)
        appearance_card.addWidget(self._section_label("Тема"))

        theme_row = QHBoxLayout()
        theme_row.setSpacing(10)
        theme_row.setContentsMargins(0, 0, 0, 0)
        for mode, label in (("dark", "Тёмная"), ("light", "Светлая")):
            btn = QPushButton(label)
            btn.setFixedHeight(32)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(partial(self._on_theme_clicked, mode))
            theme_row.addWidget(btn)
            self._theme_btns[mode] = btn
        theme_row.addStretch(1)
        appearance_card.addLayout(theme_row)

        appearance_card.addWidget(self._section_label("Масштаб интерфейса"))

        scale_row = QHBoxLayout()
        scale_row.setSpacing(10)
        scale_row.setContentsMargins(0, 0, 0, 0)
        for label, value in self.SCALE_PRESETS:
            btn = QPushButton(label)
            btn.setFixedHeight(32)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(partial(self._on_scale_clicked, value))
            scale_row.addWidget(btn)
            self._scale_btns[value] = btn
        scale_row.addStretch(1)
        appearance_card.addLayout(scale_row)

        restart_hint = QLabel("Тема и масштаб применяются после перезапуска приложения.")
        restart_hint.setWordWrap(True)
        restart_hint.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        appearance_card.addWidget(restart_hint)

        self._restyle_theme_buttons()
        self._restyle_scale_buttons()

        # ── Integrations card ────────────────────────────────────────────────
        integ_card = self._make_card(layout)
        integ_card.addWidget(self._section_label("Интеграция"))

        discord_row = QHBoxLayout()
        discord_row.setSpacing(10)
        discord_row.setContentsMargins(0, 0, 0, 0)
        discord_lbl = QLabel("Discord Rich Presence")
        discord_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        discord_row.addWidget(discord_lbl)
        discord_row.addStretch(1)

        self._discord_toggle = _ToggleSwitch()
        self._discord_toggle.toggled.connect(self.discord_rpc_toggled.emit)
        discord_row.addWidget(self._discord_toggle)
        integ_card.addLayout(discord_row)

        # ── Data card ────────────────────────────────────────────────────────
        data_card = self._make_card(layout)
        data_card.addWidget(self._section_label("Данные"))

        cache_row = QHBoxLayout()
        cache_row.setContentsMargins(0, 0, 0, 0)
        cache_row.setSpacing(10)
        cache_lbl = QLabel("Кеш обложек и библиотеки")
        cache_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        cache_row.addWidget(cache_lbl)
        cache_row.addStretch(1)

        clear_cache_btn = QPushButton("Очистить кеш")
        clear_cache_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clear_cache_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_cache_btn.setFixedHeight(30)
        clear_cache_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 12px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        clear_cache_btn.clicked.connect(self.cache_cleared.emit)
        cache_row.addWidget(clear_cache_btn)
        data_card.addLayout(cache_row)

        layout.addStretch(1)

        # Logout button at bottom
        logout_btn = QPushButton("Выйти из аккаунта")
        logout_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        logout_btn.setFixedHeight(36)
        logout_btn.clicked.connect(self.logout_clicked.emit)
        logout_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; border-radius: 8px; "
            f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI'; }}"
            f"QPushButton:hover {{ border-color: #ff7a7a; color: #ff7a7a; }}"
        )
        layout.addWidget(logout_btn)

    def _restyle_accent_buttons(self):
        """Redraw swatches, ring-highlighting + checkmark on whichever matches the current accent."""
        current = (self._current_accent or "").strip().upper()
        for btn in self._accent_btns:
            color = btn.property("accentColor")
            selected = bool(current) and (color or "").strip().upper() == current
            btn.setText("✓" if selected else "")
            border = COLORS["TEXT_PRIMARY"] if selected else "transparent"
            btn.setStyleSheet(
                f"QPushButton {{ background: {color}; border: 2px solid {border}; border-radius: 16px; "
                f"color: #000; font-weight: 700; font-size: 13px; }}"
                f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; }}"
            )

    def set_selected_accent(self, color: str):
        """Reflect the currently active accent in the swatch row (call on load and after changes)."""
        self._current_accent = color or ""
        self._restyle_accent_buttons()

    def _style_choice_button(self, btn: QPushButton, selected: bool):
        c = COLORS
        if selected:
            btn.setStyleSheet(
                f"QPushButton {{ background: {c['PRIMARY']}; border: none; border-radius: 16px; "
                f"color: #000; font: 600 9.5pt 'Segoe UI'; padding: 0 14px; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {c['BORDER']}; border-radius: 16px; "
                f"color: {c['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 14px; }}"
                f"QPushButton:hover {{ border-color: {c['TEXT_PRIMARY']}; color: {c['TEXT_PRIMARY']}; }}"
            )

    def _restyle_theme_buttons(self):
        for mode, btn in self._theme_btns.items():
            self._style_choice_button(btn, mode == self._current_theme)

    def _restyle_scale_buttons(self):
        for value, btn in self._scale_btns.items():
            self._style_choice_button(btn, abs(value - self._current_scale) < 0.01)

    def set_selected_theme(self, mode: str):
        self._current_theme = mode if mode in ("dark", "light") else "dark"
        self._restyle_theme_buttons()

    def set_selected_scale(self, scale: float):
        try:
            self._current_scale = float(scale)
        except (TypeError, ValueError):
            self._current_scale = 1.0
        self._restyle_scale_buttons()

    def apply_accent(self):
        """Re-apply styles after accent color change (the 'selected' highlight
        on theme/scale buttons uses the accent color)."""
        self._restyle_theme_buttons()
        self._restyle_scale_buttons()
        if self._discord_toggle is not None:
            self._discord_toggle.update()

    def set_discord_rpc_enabled(self, enabled: bool):
        if self._discord_toggle is None:
            return
        self._discord_toggle.blockSignals(True)
        self._discord_toggle.setChecked(enabled)
        self._discord_toggle.blockSignals(False)

    def _on_accent_clicked(self, color: str):
        set_accent_color(color)
        self.set_selected_accent(color)
        self.accent_changed.emit(color)

    def _on_theme_clicked(self, mode: str):
        self.set_selected_theme(mode)
        self.theme_changed.emit(mode)

    def _on_scale_clicked(self, scale: float):
        self.set_selected_scale(scale)
        self.scale_changed.emit(scale)


# ──────────────────────────────────────────────────────────────────────────────
# Main application window
# ──────────────────────────────────────────────────────────────────────────────

class _LibraryWorker(QObject):
    finished = pyqtSignal()

    def __init__(self, library_manager):
        super().__init__()
        self._lm = library_manager

    @pyqtSlot()
    def run(self):
        try:
            self._lm.refresh_from_network()
        except Exception as e:
            print(f"Library refresh error: {e}")
        self.finished.emit()


class _PlayerDataWorker(QObject):
    finished = pyqtSignal(dict)

    def __init__(self, account_manager):
        super().__init__()
        self._account_manager = account_manager

    @pyqtSlot()
    def run(self):
        try:
            data = self._account_manager.fetch_player_data() or {}
        except Exception:
            data = {}
        self.finished.emit(data)


class _PlayerDataSaveWorker(QObject):
    finished = pyqtSignal()

    def __init__(self, account_manager, updates: dict):
        super().__init__()
        self._account_manager = account_manager
        self._updates = updates

    @pyqtSlot()
    def run(self):
        try:
            self._account_manager.save_player_data(self._updates)
        except Exception:
            pass
        self.finished.emit()


class _DiscordConnectSignal(QObject):
    """Bridges a background Discord-connect attempt back to the main thread.
    pypresence's connect() blocks synchronously (its own asyncio loop) while
    it tries to reach Discord's IPC socket — if Discord isn't running that
    would stall the caller for a noticeable moment, so it always runs on a
    plain daemon thread (not QThread): daemon=True means it can never block
    process exit or get torn down mid-run, so there's no thread-lifecycle
    risk at all if the app closes while a connection attempt is in flight."""
    finished = pyqtSignal(object, bool)  # (DiscordRPC instance, connected)


class _SearchIcon(QWidget):
    """Small drawn magnifying-glass icon. There's no safe monochrome 'search'
    character outside the emoji-prone Unicode blocks, so this is hand-painted
    instead of relying on a font glyph that can render as a color emoji on Windows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(COLORS["TEXT_SECONDARY"]))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(1, 1, 9, 9)
        p.drawLine(9, 9, 14, 14)
        p.end()


class MusicApp(QWidget):
    logout_requested = pyqtSignal()

    def __init__(self, account_manager=None):
        super().__init__()
        self.setWindowTitle("Memify")
        self.resize(1280, 800)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))

        self._account_manager = account_manager
        self.library_manager = LibraryManager()
        self.player = PlayerController()

        self._current_artist: dict | None = None
        self._current_album: dict | None = None
        self._search_generation = 0
        self._search_thread: QThread | None = None
        self._search_worker: SearchWorker | None = None
        self._download_threads: list[QThread] = []
        self._img_runners: list = []   # [thread, worker] entries from _start_image_loader
        self._prev_page_before_search: QWidget | None = None
        self._loading = False
        self._closing = False  # set once closeEvent starts, so scheduled/async
        # callbacks don't spawn more background threads after teardown began.
        self._media_keys = None

        # Account state
        self._liked_tracks: list = []        # list of track dicts from server
        self._subscriptions: list = []       # list of artist names
        self._album_subscriptions: list = [] # list of "artist||album" strings
        self._player_data_thread: QThread | None = None
        self._library_thread: QThread | None = None

        self._discord_rpc = None
        self._discord_connecting = False
        self._discord_connect_signal = None
        self._discord_refresh_timer: QTimer | None = None
        self._state_restored = False
        self._nav_restored = False   # True once page navigation succeeded
        self._playing_url: str = ""
        self._playing_track: dict | None = None
        self._player_warmed_up = False

        self._load_settings()
        self._setup_ui()
        self._setup_player()
        self._setup_media_keys()
        self._setup_search_worker()
        self._apply_loaded_settings()

        QTimer.singleShot(0, self._load_library_then_player_data)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_settings(self):
        self._settings: dict = {}
        try:
            if os.path.exists(APP_SETTINGS_FILE):
                with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    self._settings = json.load(f)
        except Exception:
            pass
        accent = self._settings.get("accent_color", COLORS["PRIMARY"])
        set_accent_color(accent)
        set_theme(self._settings.get("theme", "dark"))
        app = QApplication.instance()
        if app:
            styles_module.apply_palette(app)
        self._audio_device_id = self._settings.get("audio_output_device_id")

    def _apply_loaded_settings(self):
        """Apply settings that require UI to already be built (called after _setup_ui)."""
        self._settings_page.set_selected_accent(self._settings.get("accent_color", COLORS["PRIMARY"]))
        self._settings_page.set_selected_theme(self._settings.get("theme", "dark"))
        self._settings_page.set_selected_scale(self._settings.get("ui_scale", 1.0))

        rpc_enabled = bool(self._settings.get("discord_rpc", False))
        self._settings_page.set_discord_rpc_enabled(rpc_enabled)
        if rpc_enabled:
            QTimer.singleShot(500, self._init_discord_rpc)

        saved_shuffle = bool(self._settings.get("shuffle", False))
        if saved_shuffle:
            self.player.shuffle_enabled = True
        self._controls.set_shuffle(saved_shuffle)

        saved_repeat = self._settings.get("repeat", "off")
        if saved_repeat in ("off", "track", "album"):
            self.player.repeat_mode = saved_repeat
            self._controls.set_repeat(saved_repeat)

    def _save_settings(self):
        try:
            self._settings["audio_output_device_id"] = self.player._preferred_audio_output_id
            with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_ui_state(self, **kwargs):
        self._settings.update(kwargs)
        try:
            with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── UI setup ──────────────────────────────────────────────────────────────

    def _setup_ui(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["BACKGROUND"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["TEXT_PRIMARY"]))
        self.setPalette(palette)
        self.setStyleSheet(get_scrollbar_style())

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Top bar
        self._build_top_bar(main)

        # Body: sidebar + page stack
        body = QWidget()
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)

        self._sidebar = Sidebar()
        self._sidebar.artist_selected.connect(self._on_artist_selected)
        self._sidebar.album_selected.connect(self._on_sidebar_album_selected)
        self._sidebar.liked_tracks_selected.connect(self._on_liked_tracks_selected)
        if self._account_manager and self._account_manager.active_login:
            self._sidebar.set_username(self._account_manager.active_login)
        body_row.addWidget(self._sidebar)

        # Page stack — pre-built, never recreated
        self._page_stack = QStackedWidget()
        self._welcome_page = WelcomePage()
        self._artist_page = ArtistPage()
        self._album_page = AlbumPage()
        self._search_page = SearchPage()
        self._all_artists_page = AllArtistsPage()
        self._settings_page = SettingsPage()

        for page in [self._welcome_page, self._artist_page, self._album_page,
                     self._search_page, self._all_artists_page, self._settings_page]:
            self._page_stack.addWidget(page)

        self._page_stack.setCurrentWidget(self._welcome_page)
        self._page_stack.setStyleSheet(
            f"QStackedWidget {{ background: {COLORS['BACKGROUND']}; }}"
        )
        body_row.addWidget(self._page_stack, 1)

        main.addWidget(body, 1)

        # Full-window cover viewer overlay (parented to self so it covers everything)
        self._cover_viewer = CoverViewerOverlay(self)
        self._disc_overlay = NowPlayingDiscOverlay(self)

        # Playback controls
        self._controls = PlaybackControls(self)
        self._controls.artist_clicked.connect(self._on_controls_artist_clicked)
        self._controls.album_clicked.connect(self._on_controls_album_clicked)
        self._controls.cover_clicked.connect(self._open_now_playing_disc)
        self._controls.play_btn.clicked.connect(self.player.toggle_playback)
        self._controls.prev_btn.clicked.connect(self.player.play_prev)
        self._controls.next_btn.clicked.connect(self.player.play_next)
        self._controls.shuffle_btn.clicked.connect(self._on_shuffle)
        self._controls.repeat_btn.clicked.connect(self._on_repeat)
        self._controls.progress_slider.sliderReleased.connect(self._on_seek)
        self._controls.volume_slider.valueChanged.connect(
            lambda v: [self.player.set_volume(v), self._save_ui_state(volume=v)]
        )
        self._controls.like_clicked.connect(self._on_like_clicked)
        saved_vol = self._settings.get("volume", 80)
        self._controls.set_volume(saved_vol)
        self.player.set_volume(saved_vol)
        main.addWidget(self._controls)

        # Wire album/artist page signals
        self._artist_page.album_clicked.connect(self._on_album_selected)
        self._artist_page.artist_like_clicked.connect(self._on_artist_like_clicked)
        self._album_page.track_play_requested.connect(self._on_track_play_requested)
        self._album_page.artist_name_clicked.connect(self._on_controls_artist_clicked)
        self._album_page.download_album_requested.connect(self._download_album)
        self._album_page.download_track_requested.connect(self._download_track)
        self._album_page.track_like_clicked.connect(self._on_album_track_like_clicked)
        self._album_page.album_like_clicked.connect(self._on_album_like_clicked)
        self._album_page.cover_clicked.connect(self._cover_viewer.show_for)
        self._search_page.result_selected.connect(self._on_search_result_selected)
        self._all_artists_page.artist_selected.connect(self._navigate_to_artist)
        self._settings_page.logout_clicked.connect(self._on_logout)
        self._settings_page.accent_changed.connect(self._on_accent_changed)
        self._settings_page.theme_changed.connect(self._on_theme_changed)
        self._settings_page.scale_changed.connect(self._on_scale_changed)
        self._settings_page.discord_rpc_toggled.connect(self._on_discord_rpc_toggled)
        self._settings_page.cache_cleared.connect(self._on_cache_cleared)

    def _build_top_bar(self, parent_layout):
        bar = QWidget()
        bar.setObjectName("globalTopBar")
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setFixedHeight(58)
        bar.setStyleSheet(
            f"QWidget#globalTopBar {{ background: {COLORS['SURFACE']}; "
            f"border-bottom: 1px solid {COLORS['BORDER']}; }}"
        )
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(0)

        # ── Left panel: logo left-aligned (stretch=1) ──────────────────────────
        left_panel = QWidget()
        left_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lp_layout = QHBoxLayout(left_panel)
        lp_layout.setContentsMargins(16, 0, 0, 0)
        lp_layout.setSpacing(0)

        logo_widget = QWidget()
        logo_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        logo_layout = QHBoxLayout(logo_widget)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        logo_layout.setSpacing(8)

        self._logo_icon_lbl = QLabel()
        self._logo_icon_lbl.setFixedSize(26, 26)
        self._logo_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if os.path.exists(APP_ICON):
            pm = QPixmap(APP_ICON).scaled(26, 26, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._logo_icon_lbl.setPixmap(pm)
        else:
            self._logo_icon_lbl.setText("M")
            self._logo_icon_lbl.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
            self._logo_icon_lbl.setStyleSheet(f"color: {COLORS['PRIMARY']}; background: transparent;")
        logo_layout.addWidget(self._logo_icon_lbl)

        self._logo_text_lbl = QLabel("Memify")
        self._logo_text_lbl.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._logo_text_lbl.setStyleSheet(f"color: {COLORS['PRIMARY']}; background: transparent;")
        logo_layout.addWidget(self._logo_text_lbl)

        logo_widget.mousePressEvent = lambda _e: self._go_home()
        lp_layout.addWidget(logo_widget)
        lp_layout.addStretch(1)

        # ── Center: search wrapper (stretch=2, truly centered) ─────────────────
        self._search_wrapper = QWidget()
        self._search_wrapper.setObjectName("searchWrapper")
        self._search_wrapper.setFixedHeight(38)
        self._search_wrapper.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_wrapper.setStyleSheet(
            f"QWidget#searchWrapper {{ background: {COLORS['SURFACE_LIGHT']}; "
            f"border: 1.5px solid {COLORS['BORDER']}; border-radius: 19px; }}"
        )
        sw_layout = QHBoxLayout(self._search_wrapper)
        sw_layout.setContentsMargins(12, 0, 14, 0)
        sw_layout.setSpacing(6)

        search_icon_lbl = _SearchIcon(self)
        sw_layout.addWidget(search_icon_lbl)

        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Поиск исполнителей, альбомов, треков...")
        self._search_bar.setFrame(False)
        self._search_bar.setStyleSheet(
            f"QLineEdit {{ background: transparent; color: {COLORS['TEXT_PRIMARY']}; "
            f"border: none; font: 10.5pt 'Segoe UI'; }}"
        )
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(280)
        self._search_timer.timeout.connect(self._perform_search)
        self._search_bar.textChanged.connect(self._on_search_text_changed)
        sw_layout.addWidget(self._search_bar, 1)

        # ── Right panel: settings right-aligned (stretch=1) ────────────────────
        right_panel = QWidget()
        right_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        rp_layout = QHBoxLayout(right_panel)
        rp_layout.setContentsMargins(0, 0, 16, 0)
        rp_layout.setSpacing(0)
        rp_layout.addStretch(1)

        artists_btn = QPushButton("Исполнители")
        artists_btn.setFixedHeight(34)
        artists_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        artists_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        artists_btn.setToolTip("Все исполнители на сервере")
        artists_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; border-radius: 17px; "
            f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 16px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        artists_btn.clicked.connect(self._show_all_artists)
        rp_layout.addWidget(artists_btn)
        rp_layout.addSpacing(10)

        settings_btn = QPushButton("≡")
        settings_btn.setFixedSize(36, 36)
        settings_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setToolTip("Настройки")
        settings_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 18px; "
            f"color: {COLORS['TEXT_SECONDARY']}; font-size: 17px; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; background: {COLORS['SURFACE_HOVER']}; }}"
        )
        settings_btn.clicked.connect(self._toggle_settings)
        rp_layout.addWidget(settings_btn)

        bar_layout.addWidget(left_panel, 1)
        bar_layout.addWidget(self._search_wrapper, 2)
        bar_layout.addWidget(right_panel, 1)

        parent_layout.addWidget(bar)
        self._settings_visible = False

        # Highlight search border on focus
        app_inst = QApplication.instance()
        if app_inst:
            app_inst.focusChanged.connect(self._on_search_focus_changed)

    def _on_search_focus_changed(self, _old, new_widget):
        focused = new_widget is self._search_bar
        border = COLORS["PRIMARY"] if focused else COLORS["BORDER"]
        self._search_wrapper.setStyleSheet(
            f"QWidget#searchWrapper {{ background: {COLORS['SURFACE_LIGHT']}; "
            f"border: 1.5px solid {border}; border-radius: 19px; }}"
        )

    # ── Library loading ───────────────────────────────────────────────────────

    def _load_library_then_player_data(self):
        if self._loading or self._closing:
            return
        self._loading = True

        # Step 1: Instantaneously load library from local file cache so navigation works right away
        try:
            if os.path.exists(LIBRARY_CACHE_FILE):
                with open(LIBRARY_CACHE_FILE, "r", encoding="utf-8") as _f:
                    _cached = json.load(_f)
                if isinstance(_cached, list) and _cached:
                    self.library_manager.library = [
                        a for a in _cached
                        if isinstance(a, dict) and not (a.get("artist", "") or "").startswith("---")
                    ]
                    self.library_manager.build_search_index()
        except Exception:
            pass

        # Step 2: Apply locally cached player data with library already available
        if self._account_manager:
            local = self._read_local_player_data()
            if local:
                self._on_player_data_loaded(local)
            else:
                self._sidebar.load_account_content(liked=False, artists=[])

        # Step 3: Refresh library from server in background (won't block UI)
        worker = _LibraryWorker(self.library_manager)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)
        worker.finished.connect(self._on_library_loaded)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self._library_thread = thread
        thread.start()

    def _on_library_loaded(self):
        self._loading = False
        self._library_thread = None
        # Retry the player warm-up now that real track URLs may be available
        # — on a fresh login (no local library cache yet) the earlier
        # timer-based attempt had nothing to warm the network path up with.
        self._try_warm_up_player()
        # Refresh sidebar now that library is available (artists/albums can be resolved)
        if self._account_manager:
            self._update_sidebar_from_account()
            self._fetch_player_data()
            # Retry navigation if it failed earlier because library was empty
            if self._state_restored and not self._nav_restored:
                self._retry_nav_restore()
        else:
            self._sidebar.load_account_content(liked=False, artists=[])

    def _fetch_player_data(self):
        if self._closing:
            return
        if self._player_data_thread:
            try:
                self._player_data_thread.quit()
            except Exception:
                pass

        worker = _PlayerDataWorker(self._account_manager)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)
        worker.finished.connect(self._on_player_data_loaded)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        thread.start()
        self._player_data_thread = thread

    def _on_player_data_loaded(self, data: dict):
        self._liked_tracks = data.get("liked_tracks", []) or []
        self._subscriptions = data.get("subscriptions", []) or []
        self._album_subscriptions = data.get("album_subscriptions", []) or []
        self._update_sidebar_from_account()
        if not self._state_restored:
            QTimer.singleShot(0, self._restore_ui_state)

    def _restore_ui_state(self):
        if self._state_restored:
            return
        self._state_restored = True

        # Restore bottom bar track display, and prime the player with the real
        # album/track so the play button can actually resume it (not just show it).
        last_played = self._settings.get("last_played_track")
        if isinstance(last_played, dict) and last_played.get("title"):
            track = {
                "title": last_played.get("title", ""),
                "artist_name": last_played.get("artist_name", ""),
                "_real_album_title": last_played.get("album_title", ""),
                "album_id": last_played.get("album_id", ""),
            }
            artist = {"artist": last_played.get("artist_name", "")}
            album = {
                "title": last_played.get("album_title", ""),
                "cover": last_played.get("album_cover", ""),
            }
            display_artist_names = self._display_artist_names(
                last_played.get("album_id", ""), last_played.get("artist_name", "")
            )
            self._controls.update_track_info(track, artist, album, display_artist_names=display_artist_names)
            self._load_controls_cover(album)
            self._prime_player_for_resume(last_played)

        # Restore navigation
        page = self._settings.get("last_view_page", "")
        artist_name = self._settings.get("last_view_artist", "")
        album_title = self._settings.get("last_view_album", "")

        if page == "liked":
            self._on_liked_tracks_selected()
            self._nav_restored = True
            return

        if not artist_name:
            self._nav_restored = True
            return

        artist = self.library_manager.get_artist_by_name(artist_name)
        if not artist:
            # Library not ready yet — _on_library_loaded will retry
            return

        self._navigate_to_artist(artist)

        if page == "album" and album_title:
            for al in artist.get("albums", []):
                if clean_title(al.get("title", "")) == clean_title(album_title):
                    self._navigate_to_album(al, artist)
                    break
        self._nav_restored = True

    def _prime_player_for_resume(self, last_played: dict):
        """Load the real album/track (found via the library) into the player
        without starting playback, so pressing the bottom-bar play button
        actually resumes the last-played track instead of doing nothing."""
        artist_name = last_played.get("artist_name", "")
        album_title = last_played.get("album_title", "")
        track_title = last_played.get("title", "")
        if not artist_name or not album_title or not track_title:
            return

        artist = self.library_manager.get_artist_by_name(artist_name)
        if not artist:
            return

        album = None
        for al in artist.get("albums", []):
            if clean_title(al.get("title", "")) == clean_title(album_title):
                album = al
                break
        if not album:
            return

        idx = None
        for i, t in enumerate(album.get("tracks", [])):
            if clean_title(t.get("title", "")) == clean_title(track_title):
                idx = i
                break
        if idx is None:
            return

        self.player.set_album(album, artist)
        try:
            pos = self.player.shuffled_indices.index(idx)
        except ValueError:
            pos = idx
        self.player.current_track = pos
        self.player.current_track_idx = idx

    def _retry_nav_restore(self):
        """Retry page navigation after library loaded from network (first attempt failed with empty library)."""
        if self.player.current_playing_album is None:
            last_played = self._settings.get("last_played_track")
            if isinstance(last_played, dict) and last_played.get("title"):
                self._prime_player_for_resume(last_played)

        page = self._settings.get("last_view_page", "")
        artist_name = self._settings.get("last_view_artist", "")
        album_title = self._settings.get("last_view_album", "")

        if page == "liked":
            self._on_liked_tracks_selected()
            self._nav_restored = True
            return

        if not artist_name:
            self._nav_restored = True
            return

        artist = self.library_manager.get_artist_by_name(artist_name)
        if not artist:
            return

        self._navigate_to_artist(artist)
        if page == "album" and album_title:
            for al in artist.get("albums", []):
                if clean_title(al.get("title", "")) == clean_title(album_title):
                    self._navigate_to_album(al, artist)
                    break
        self._nav_restored = True

    def _update_sidebar_from_account(self):
        library = self.library_manager.get_library()
        artist_index = {(a.get("artist") or "").strip(): a for a in library}

        # Preserve subscription order (newest appended last → reverse = newest first)
        followed_artists = []
        for name in reversed(self._subscriptions):
            artist_obj = artist_index.get((name or "").strip())
            if artist_obj and artist_obj not in followed_artists:
                followed_artists.append(artist_obj)

        # Resolve album subscriptions in subscription order (newest last → reverse = newest first)
        liked_albums: list[tuple] = []
        for key in reversed(self._album_subscriptions):
            parts = key.split("||", 1)
            if len(parts) != 2:
                continue
            artist_name, album_title = parts[0].strip(), parts[1].strip()
            artist_obj = artist_index.get(artist_name)
            if not artist_obj:
                continue
            for al in artist_obj.get("albums", []):
                if (al.get("title") or "").strip() == album_title:
                    liked_albums.append((al, artist_obj))
                    break

        has_liked = bool(self._liked_tracks)
        self._sidebar.load_account_content(
            liked=has_liked, artists=followed_artists, albums=liked_albums
        )

    def _player_data_cache_path(self) -> str:
        """Local player-data cache path for the currently logged-in account —
        one file per login, so cached likes/subscriptions from a previously
        used account on this machine never leak into a different account."""
        login = (self._account_manager.active_login if self._account_manager else "") or "_guest"
        return os.path.join(PLAYER_DATA_CACHE_DIR, f"{_safe_filename(login)}.json")

    def _read_local_player_data(self) -> dict:
        try:
            path = self._player_data_cache_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    def _write_local_player_data(self, data: dict):
        try:
            with open(self._player_data_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_player_data_async(self, updates: dict):
        if not self._account_manager:
            return
        # Always send full current state to avoid partial overwrites on server
        full = {
            "liked_tracks": self._liked_tracks,
            "subscriptions": self._subscriptions,
            "album_subscriptions": self._album_subscriptions,
        }
        full.update(updates)
        # Write to local cache immediately so next startup sees it right away
        self._write_local_player_data(full)
        worker = _PlayerDataSaveWorker(self._account_manager, full)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._cleanup_save_thread(thread, worker))
        thread.start()
        self._download_threads.append(thread)

    def _cleanup_save_thread(self, thread: QThread, worker):
        try:
            self._download_threads.remove(thread)
        except ValueError:
            pass
        try:
            worker.deleteLater()
        except Exception:
            pass
        try:
            thread.deleteLater()
        except Exception:
            pass

    # ── Player setup ──────────────────────────────────────────────────────────

    def _setup_player(self):
        self.player.setup_connections()
        self.player.set_callbacks(
            track_changed=self._on_track_changed,
            playback_state_changed=self._on_playback_state_changed,
            position_changed=self._on_position_changed,
            duration_changed=self._on_duration_changed,
            album_finished=self._on_album_finished,
            album_previous=self._on_album_previous,
        )
        saved_vol = self._settings.get("volume", 80)
        self.player.set_volume(saved_vol)
        if self._audio_device_id:
            self.player.set_audio_output_device(self._audio_device_id)
        # Short delay so this doesn't compete with the very first paint of
        # the window, but otherwise as early as possible: warm_up() now uses
        # its own throwaway player (see PlayerController.warm_up()), so
        # there's no risk in starting it early even if the user then clicks
        # play immediately — the earlier this fires, the more likely it
        # finishes before a real, fast first click. Also retried from
        # _on_library_loaded(): a real track URL is needed to warm up the
        # network-streaming path, not just local decoding, and the library
        # may not be available yet at this point (fresh login, no local
        # cache) — see _try_warm_up_player().
        QTimer.singleShot(150, self._try_warm_up_player)

    def _first_track_url(self) -> str | None:
        try:
            for artist in self.library_manager.get_library():
                for album in (artist.get("albums") or []):
                    for track in (album.get("tracks") or []):
                        url = track.get("url")
                        if url:
                            return SERVER_URL + url if not url.startswith("http") else url
        except Exception:
            pass
        return None

    def _try_warm_up_player(self):
        if self._player_warmed_up or self._closing:
            return
        url = self._first_track_url()
        if url:
            # Found a real track URL — this is the warmup that actually
            # matters (it's what exercises the network/HTTP code path), so
            # don't bother repeating it once done.
            self._player_warmed_up = True
        self.player.warm_up(url)

    def _setup_media_keys(self):
        if not _MEDIA_KEYS_AVAILABLE:
            return
        if not self._settings.get("global_media_keys", True):
            return
        try:
            self._media_keys = MediaKeysHandler(self.player, self)
            self._media_keys.setup_media_keys()
        except Exception:
            self._media_keys = None

    # ── Search ────────────────────────────────────────────────────────────────

    def _setup_search_worker(self):
        worker = SearchWorker()
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)
        worker.finished.connect(self._on_search_results)
        self._search_worker = worker
        self._search_thread = thread
        thread.start()

    def _on_search_text_changed(self, text: str):
        self._search_timer.stop()
        if text.strip():
            self._search_timer.start()
        else:
            self._search_page.set_loading(False)
            if self._page_stack.currentWidget() == self._search_page:
                self._go_home()

    def _perform_search(self):
        query = self._search_bar.text().strip()
        if not query:
            return
        self._search_generation += 1
        if self._page_stack.currentWidget() != self._search_page:
            self._prev_page_before_search = self._page_stack.currentWidget()
        self._page_stack.setCurrentWidget(self._search_page)
        self._search_page.set_loading(True)
        if self._search_worker:
            self._search_worker.request.emit(query, self._search_generation)

    def _on_search_results(self, results: list, generation: int):
        if generation != self._search_generation:
            return
        self._search_page.update_results(results)

    def _on_search_result_selected(self, result: SearchResult):
        self._search_bar.clear()
        if result.type == "artist" and result.artist_obj:
            self._navigate_to_artist(result.artist_obj)
        elif result.type == "album" and result.artist_obj and result.album_obj:
            self._navigate_to_artist(result.artist_obj)
            QTimer.singleShot(50, lambda: self._navigate_to_album(result.album_obj, result.artist_obj))
        elif result.type == "track" and result.artist_obj and result.album_obj and result.track_obj:
            self._navigate_to_artist(result.artist_obj)
            QTimer.singleShot(50, lambda: self._navigate_to_album(result.album_obj, result.artist_obj))
            try:
                tracks = result.album_obj.get("tracks", [])
                idx = tracks.index(result.track_obj) if result.track_obj in tracks else 0
                QTimer.singleShot(80, lambda: self._play_track(idx, result.album_obj, result.artist_obj))
            except Exception:
                pass

    # ── Navigation ────────────────────────────────────────────────────────────

    def _go_home(self):
        self._search_bar.clear()
        self._search_page.set_loading(False)
        if self._current_artist:
            self._page_stack.setCurrentWidget(self._artist_page)
        else:
            self._page_stack.setCurrentWidget(self._welcome_page)

    def _toggle_settings(self):
        if self._page_stack.currentWidget() == self._settings_page:
            self._go_home()
        else:
            self._page_stack.setCurrentWidget(self._settings_page)

    def _show_all_artists(self):
        self._all_artists_page.load_artists(self.library_manager.get_library())
        self._page_stack.setCurrentWidget(self._all_artists_page)

    def _navigate_to_artist(self, artist: dict):
        self._current_artist = artist
        self._sidebar.select_artist(artist.get("artist", ""))
        self._artist_page.load_artist(artist)
        artist_name = (artist.get("artist") or "").strip()
        self._artist_page.set_liked(artist_name in self._subscriptions)
        self._page_stack.setCurrentWidget(self._artist_page)
        self._save_ui_state(
            last_view_page="artist",
            last_view_artist=artist_name,
            last_view_album="",
        )

    def _navigate_to_album(self, album: dict, artist: dict):
        self._current_album = album
        self._current_artist = artist
        display_artist_names = self._display_artist_names(album.get("album_id"), artist.get("artist", ""))
        self._album_page.load_album(
            album, artist, playing_url=self._playing_url, display_artist_names=display_artist_names,
            playing_track=self._playing_track,
        )
        self._album_page._album_like_btn.setVisible(True)
        album_key = self._album_key(artist.get("artist", ""), album.get("title", ""))
        self._album_page.set_album_liked(album_key in self._album_subscriptions)
        liked_urls = self._liked_urls_set()
        self._album_page.refresh_track_likes(liked_urls)
        self._page_stack.setCurrentWidget(self._album_page)
        self._save_ui_state(
            last_view_page="album",
            last_view_artist=(artist.get("artist") or "").strip(),
            last_view_album=(album.get("title") or "").strip(),
        )

    def _on_artist_selected(self, artist: dict):
        self._navigate_to_artist(artist)

    def _on_sidebar_album_selected(self, album: dict, artist: dict):
        self._current_artist = artist
        self._navigate_to_album(album, artist)

    def _on_album_selected(self, album: dict, artist: dict):
        self._navigate_to_album(album, artist)

    def _resolve_liked_track(self, lt: dict, library: list) -> dict:
        """Return the real track dict from library if found; otherwise construct one from stored fields."""
        url = lt.get("url", "")
        artist_name = lt.get("artist_name", "")
        album_title = lt.get("album_title", "")
        for artist in library:
            if not isinstance(artist, dict):
                continue
            a_name = clean_artist_name(artist.get("artist", ""))
            if artist_name and a_name != clean_artist_name(artist_name):
                continue
            for album in artist.get("albums", []):
                if album_title and clean_title(album.get("title", "")) != clean_title(album_title):
                    continue
                for track in album.get("tracks", []):
                    t_url = track.get("url", "")
                    full_url = SERVER_URL + t_url if t_url and not t_url.startswith("http") else t_url
                    if url in (t_url, full_url):
                        result = dict(track)
                        result.setdefault("artist_name", artist.get("artist", ""))
                        result["_real_album_title"] = album.get("title", "")
                        result["_real_album_cover"] = album.get("cover", "")
                        return result
        return {
            "title": lt.get("track_title") or lt.get("title", ""),
            "url": url,
            "artist_name": artist_name,
            "duration": lt.get("duration"),
            "_real_album_title": album_title,
            "_real_album_cover": lt.get("album_cover", ""),
        }

    def _on_liked_tracks_selected(self):
        """Show liked tracks as a virtual album in the album page."""
        library = self.library_manager.get_library()
        tracks = []
        for lt in self._liked_tracks:
            if isinstance(lt, dict):
                tracks.append(self._resolve_liked_track(lt, library))
            elif isinstance(lt, str):
                tracks.append({"url": lt, "title": lt.split("/")[-1]})

        icon_path = os.path.join(ICONS_DIR, "liked_icon.png")
        cover = icon_path if os.path.exists(icon_path) else ""
        virtual_album = {
            "title": "Понравившиеся треки",
            "tracks": tracks,
            "cover": cover,
            "_is_liked_album": True,
        }
        virtual_artist = {"artist": ""}
        self._current_album = virtual_album
        self._current_artist = virtual_artist
        self._album_page.load_album(
            virtual_album, virtual_artist, playing_url=self._playing_url, playing_track=self._playing_track
        )
        self._album_page._album_like_btn.setVisible(False)
        self._album_page.refresh_track_likes(self._liked_urls_set())
        self._page_stack.setCurrentWidget(self._album_page)
        self._save_ui_state(last_view_page="liked", last_view_artist="", last_view_album="")

    def _on_logout(self):
        if self._account_manager:
            self._account_manager.logout()
        self.player.stop()
        # Clear saved navigation so fresh login starts at welcome page
        self._save_ui_state(
            last_view_page="", last_view_artist="", last_view_album="",
            last_played_track={},
        )
        self.logout_requested.emit()

    def _on_cache_cleared(self):
        cover_cache.clear()
        cover_cache.clear_disk_cache()
        try:
            if os.path.exists(LIBRARY_CACHE_FILE):
                os.remove(LIBRARY_CACHE_FILE)
        except Exception:
            pass
        try:
            path = self._player_data_cache_path()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        self.library_manager.clear_cache()
        self._loading = False
        self._load_library_then_player_data()

    def _on_controls_artist_clicked(self, artist_name: str):
        artist = self.library_manager.get_artist_by_name(artist_name)
        if artist:
            self._navigate_to_artist(artist)

    def _on_controls_album_clicked(self, album_title: str, artist_name: str):
        # Prefer the exact album object that's actually loaded in the player —
        # matching by title text alone can land on the wrong album when two
        # albums under the same artist share the same (cleaned) title.
        playing_album = self.player.current_playing_album
        playing_artist = self.player.current_playing_artist
        if playing_album and playing_artist and not playing_album.get("_is_liked_album"):
            self._navigate_to_album(playing_album, playing_artist)
            return

        # Playing from a virtual album (e.g. liked tracks) — resolve the real
        # album via its album_id when possible (exact), falling back to title text.
        album_id = ""
        if playing_album and self.player.current_track_idx is not None:
            try:
                track = playing_album["tracks"][self.player.current_track_idx]
                album_id = str(track.get("album_id") or "").strip()
            except (IndexError, KeyError, TypeError):
                pass

        artist = self.library_manager.get_artist_by_name(artist_name)
        if not artist:
            return
        album = self._find_album_in_artist(artist, album_id, album_title)
        if album:
            self._navigate_to_album(album, artist)

    @staticmethod
    def _find_album_in_artist(artist: dict, album_id: str, album_title: str) -> dict | None:
        albums = artist.get("albums", []) if artist else []
        if album_id:
            for al in albums:
                if str(al.get("album_id") or "").strip() == album_id:
                    return al
        cleaned = clean_title(album_title) if album_title else ""
        if cleaned and cleaned != "Неизвестно":
            for al in albums:
                if clean_title(al.get("title", "")) == cleaned:
                    return al
        return None

    # ── Playback ──────────────────────────────────────────────────────────────

    def _on_track_play_requested(self, track_idx: int, album: dict, artist: dict):
        self._play_track(track_idx, album, artist)

    def _play_track(self, track_idx: int, album: dict, artist: dict):
        self.player.set_album(album, artist)
        self.player.play_track(track_idx)

    def _on_like_clicked(self):
        if not self._account_manager:
            return
        track = self.player.current_track
        album = self.player.current_playing_album
        artist = self.player.current_playing_artist
        if track is None or not album or self.player.current_track_idx is None:
            return
        try:
            track_obj = album["tracks"][self.player.current_track_idx]
        except (IndexError, KeyError, TypeError):
            return
        rel_url = track_obj.get("url", "")
        track_url = (SERVER_URL + rel_url if rel_url and not rel_url.startswith("http") else rel_url)
        artist_name = track_obj.get("artist_name") or (artist or {}).get("artist", "") or ""
        # Use real album title if playing from liked tracks virtual album
        if album.get("_is_liked_album"):
            album_title = track_obj.get("_real_album_title", "")
        else:
            album_title = album.get("title", "") or ""
        track_title = track_obj.get("title", "") or ""
        album_id = str(track_obj.get("album_id") or "").strip()

        my_keys = _track_like_keys(track_obj, rel_url)
        is_liked = bool(my_keys & self._liked_urls_set())

        if is_liked:
            self._liked_tracks = [
                lt for lt in self._liked_tracks if not _liked_entry_matches(lt, my_keys)
            ]
        else:
            self._liked_tracks.insert(0, {
                "url": rel_url or track_url,
                "artist_name": artist_name,
                "album_title": album_title,
                "track_title": track_title,
                "album_id": album_id,
            })

        self._controls.set_like_state(not is_liked, enabled=True)
        self._save_player_data_async({"liked_tracks": self._liked_tracks})
        self._update_sidebar_from_account()
        if self._current_album and self._current_album.get("_is_liked_album"):
            self._on_liked_tracks_selected()

    def _sync_like_button(self):
        """Update the ♥ button state for the current track."""
        if not self._account_manager:
            return
        album = self.player.current_playing_album
        if not album or self.player.current_track_idx is None:
            self._controls.set_like_state(False, enabled=False)
            return
        try:
            track_obj = album["tracks"][self.player.current_track_idx]
        except (IndexError, KeyError, TypeError):
            self._controls.set_like_state(False, enabled=False)
            return
        is_liked = bool(_track_like_keys(track_obj) & self._liked_urls_set())
        self._controls.set_like_state(is_liked, enabled=True)

    # ── Like helpers ──────────────────────────────────────────────────────────

    def _liked_urls_set(self) -> set:
        """URL variants and album_id-based identity keys of all liked tracks
        — the latter let a like on one artist's copy of a shared/duplicated
        album also match the other artist's identical copy."""
        keys = set()
        for lt in self._liked_tracks:
            if isinstance(lt, dict):
                keys |= _track_like_keys(lt, lt.get("url", ""))
            elif isinstance(lt, str) and lt:
                keys |= _track_like_keys({}, lt)
        return keys

    @staticmethod
    def _album_key(artist_name: str, album_title: str) -> str:
        return f"{(artist_name or '').strip()}||{(album_title or '').strip()}"

    def _on_album_track_like_clicked(self, track: dict):
        if not self._account_manager:
            return
        rel_url = track.get("url", "")
        track_url = (SERVER_URL + rel_url) if rel_url and not rel_url.startswith("http") else rel_url
        my_keys = _track_like_keys(track, rel_url)
        is_liked = bool(my_keys & self._liked_urls_set())
        if is_liked:
            self._liked_tracks = [
                lt for lt in self._liked_tracks if not _liked_entry_matches(lt, my_keys)
            ]
        else:
            current_album = self._current_album or {}
            if current_album.get("_is_liked_album"):
                album_title = track.get("_real_album_title", "")
            else:
                album_title = current_album.get("title", "")
            self._liked_tracks.insert(0, {
                "url": rel_url or track_url,
                "artist_name": track.get("artist_name", ""),
                "album_title": album_title,
                "track_title": track.get("title", ""),
                "album_id": str(track.get("album_id") or "").strip(),
            })
        self._save_player_data_async({"liked_tracks": self._liked_tracks})
        self._update_sidebar_from_account()
        # Sync playback controls if this is the current track
        self._sync_like_button()
        # Refresh liked tracks page to show updated order
        if self._current_album and self._current_album.get("_is_liked_album"):
            self._on_liked_tracks_selected()
        else:
            self._album_page.set_track_liked(track, not is_liked)

    def _on_album_like_clicked(self):
        if not self._account_manager or not self._current_album:
            return
        artist_name = (self._current_artist or {}).get("artist", "")
        album_title = self._current_album.get("title", "")
        key = self._album_key(artist_name, album_title)
        if key in self._album_subscriptions:
            self._album_subscriptions.remove(key)
            self._album_page.set_album_liked(False)
        else:
            self._album_subscriptions.append(key)
            self._album_page.set_album_liked(True)
        self._save_player_data_async({"album_subscriptions": self._album_subscriptions})
        self._update_sidebar_from_account()

    def _on_artist_like_clicked(self):
        if not self._account_manager or not self._current_artist:
            return
        artist_name = (self._current_artist.get("artist") or "").strip()
        if not artist_name:
            return
        if artist_name in self._subscriptions:
            self._subscriptions.remove(artist_name)
            self._artist_page.set_liked(False)
        else:
            self._subscriptions.append(artist_name)
            self._artist_page.set_liked(True)
        self._save_player_data_async({"subscriptions": self._subscriptions})
        self._update_sidebar_from_account()

    def _on_accent_changed(self, color: str):
        # Single source of truth for persistence — avoid a second writer
        # (SettingsPage used to write the file itself, which got clobbered
        # by the next _save_ui_state()/_save_settings() call elsewhere).
        self._save_ui_state(accent_color=color)

        self._controls.apply_accent()
        self._album_page.apply_accent()
        self._artist_page.apply_accent()
        self._cover_viewer.apply_accent()
        self._settings_page.apply_accent()
        c = COLORS
        # The search field's pill border lives on _search_wrapper, not on the
        # QLineEdit itself (which stays borderless) — just refresh it for the
        # current focus state so it doesn't fall out of sync with the new accent.
        self._on_search_focus_changed(None, QApplication.instance().focusWidget() if QApplication.instance() else None)
        self._logo_icon_lbl.setStyleSheet(f"color: {c['PRIMARY']}; background: transparent;")
        self._logo_text_lbl.setStyleSheet(f"color: {c['PRIMARY']}; background: transparent;")
        # Update application-wide highlight color so Fusion style hover matches accent
        app = QApplication.instance()
        if app:
            palette = app.palette()
            palette.setColor(QPalette.ColorRole.Highlight, QColor(color))
            app.setPalette(palette)

    def _on_theme_changed(self, mode: str):
        self._save_ui_state(theme=mode)
        self._offer_restart()

    def _on_scale_changed(self, scale: float):
        self._save_ui_state(ui_scale=scale)
        self._offer_restart()

    def _offer_restart(self):
        """Theme and UI scale are baked into every widget at construction/
        QApplication startup — rebuilding the whole app live isn't practical,
        so ask to relaunch the process instead."""
        reply = QMessageBox.question(
            self,
            "Перезапуск",
            "Изменения применятся после перезапуска приложения. Перезапустить сейчас?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._restart_app()

    def _restart_app(self):
        from PyQt6.QtCore import QProcess
        try:
            if getattr(sys, "frozen", False):
                # Packaged build: sys.executable IS the app — sys.argv[0] is
                # also the exe's own path here, so prepending sys.executable
                # (the old bug) duplicated it as an extra argv[1] to itself.
                program = sys.executable
                args = list(sys.argv[1:])
            else:
                # Dev run: sys.executable is the interpreter, sys.argv[0] is
                # the script path — both genuinely need to be passed along.
                program = sys.executable
                args = [os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
            ok, _pid = QProcess.startDetached(program, args)
            if not ok:
                print(f"[Restart] QProcess.startDetached failed for {program!r} {args!r}")
                return
        except Exception as e:
            print(f"[Restart] failed: {e}")
            return
        # Close the real top-level window (not just this widget) so
        # closeEvent/thread cleanup runs before the process actually exits.
        top = self.window()
        if top:
            top.close()

    # ── Discord RPC ───────────────────────────────────────────────────────────

    def _on_discord_rpc_toggled(self, enabled: bool):
        if enabled:
            self._settings["discord_rpc"] = True
            # Connects in the background; _on_discord_connect_result flips the
            # checkbox back off and updates the setting if it fails.
            self._init_discord_rpc()
        else:
            self._dispose_discord_rpc()
            self._settings["discord_rpc"] = False
        self._save_settings()

    def _init_discord_rpc(self):
        if self._discord_rpc and getattr(self._discord_rpc, "connected", False):
            return
        if self._discord_rpc:
            # A previous attempt exists but never connected (e.g. Discord
            # wasn't running) — dispose it before starting a new one, or its
            # Presence/asyncio loop is silently dropped and prints a "QThread/
            # event loop destroyed" warning during garbage collection later.
            self._dispose_discord_rpc()
        if self._discord_connecting:
            return  # already trying to connect

        self._discord_connecting = True
        signal = _DiscordConnectSignal(self)
        signal.finished.connect(self._on_discord_connect_result)
        self._discord_connect_signal = signal  # keep it alive until the callback fires

        def worker():
            from ui.discord_rpc import DiscordRPC
            rpc = DiscordRPC()
            try:
                rpc.connect()
            except Exception as e:
                print(f"[RPC] init error: {e}")
            signal.finished.emit(rpc, bool(getattr(rpc, "connected", False)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_discord_connect_result(self, rpc, connected: bool):
        self._discord_connecting = False
        self._discord_connect_signal = None
        if connected:
            self._discord_rpc = rpc
            self._refresh_discord_presence()
        else:
            try:
                rpc.disconnect()
            except Exception:
                pass
            self._discord_rpc = None
            if self._settings.get("discord_rpc"):
                self._settings["discord_rpc"] = False
                self._settings_page.set_discord_rpc_enabled(False)
                self._save_settings()

    def _dispose_discord_rpc(self):
        if not self._discord_rpc:
            return
        try:
            self._discord_rpc.clear()
        except Exception:
            pass
        try:
            self._discord_rpc.disconnect()
        except Exception:
            pass
        self._discord_rpc = None

    def _schedule_discord_presence_refresh(self, delay_ms: int = 180):
        if not self._discord_rpc:
            return
        if self._discord_refresh_timer is None:
            self._discord_refresh_timer = QTimer(self)
            self._discord_refresh_timer.setSingleShot(True)
            self._discord_refresh_timer.timeout.connect(self._refresh_discord_presence)
        self._discord_refresh_timer.stop()
        self._discord_refresh_timer.start(max(0, delay_ms))

    def _refresh_discord_presence(self):
        try:
            rpc = self._discord_rpc
            if not rpc or not getattr(rpc, "connected", False):
                return
            album = self.player.current_playing_album
            artist = self.player.current_playing_artist
            if not album or self.player.current_track_idx is None:
                rpc.clear()
                return
            try:
                track = album["tracks"][self.player.current_track_idx]
            except (IndexError, KeyError, TypeError):
                rpc.clear()
                return
            if not track:
                rpc.clear()
                return

            rpc_title = track.get("title", "") or "Неизвестно"
            rpc_artist_names = self._display_artist_names(
                track.get("album_id") or album.get("album_id"),
                track.get("artist_name") or (artist or {}).get("artist", ""),
            )
            rpc_artist = ", ".join(rpc_artist_names) or "Неизвестно"
            rpc_album_title = track.get("_real_album_title") or album.get("title", "") or ""

            # Cover: prefer real album cover
            cover_rel = track.get("_real_album_cover") or album.get("cover", "")
            if cover_rel and not (os.path.isabs(cover_rel) and os.path.exists(cover_rel)):
                rpc_cover = (SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel)
            else:
                rpc_cover = None

            rpc_pos_ms = self.player.get_current_position()
            rpc_dur_ms = self.player.get_duration()

            if self.player.is_playing():
                rpc.set_play(rpc_title, rpc_artist, rpc_album_title, rpc_cover, rpc_pos_ms, rpc_dur_ms)
            else:
                rpc.set_pause()
        except Exception as e:
            print(f"[RPC] refresh error: {e}")

    def _on_shuffle(self, checked: bool):
        if self.player.shuffle_enabled != checked:
            self.player.toggle_shuffle()
        self._save_ui_state(shuffle=self.player.shuffle_enabled)

    def _on_repeat(self):
        mode = self.player.toggle_repeat()
        self._controls.set_repeat(mode)
        self._save_ui_state(repeat=mode)

    def _on_seek(self):
        value = self._controls.progress_slider.value()
        self.player.seek_position(value / 10.0)

    # ── Player callbacks ──────────────────────────────────────────────────────

    def _display_artist_names(self, album_id: str, fallback_artist_name: str) -> list:
        """All artist names to show for a track/album — several, sorted А-Я,
        when the album is shared (same album_id) across multiple artists;
        otherwise just the single real artist."""
        album_id = str(album_id or "").strip()
        if album_id:
            artists = self.library_manager.get_artists_for_album_id(album_id)
            if len(artists) > 1:
                return [clean_artist_name(a) for a in artists]
        return [clean_artist_name(fallback_artist_name)] if fallback_artist_name else []

    def _on_track_changed(self, track: dict, artist: dict, album: dict):
        display_artist_names = self._display_artist_names(
            track.get("album_id") or (album or {}).get("album_id"),
            track.get("artist_name") or (artist or {}).get("artist", ""),
        )
        self._controls.update_track_info(track, artist, album, display_artist_names=display_artist_names)
        self._controls.set_playing(True)
        self._load_controls_cover(album)
        self._sync_like_button()
        self._schedule_discord_presence_refresh(90)
        self._schedule_discord_presence_refresh(650)
        # Highlight playing track everywhere
        self._playing_url = track.get("url", "") or ""
        self._playing_track = track
        self._album_page.mark_playing_url(self._playing_url, track)
        self._search_page.refresh_playing(self._playing_url, track)
        # Save track info for next-launch restore
        artist_name = track.get("artist_name") or (artist or {}).get("artist", "") or ""
        album_title = track.get("_real_album_title") or (album or {}).get("title", "") or ""
        album_id = str(track.get("album_id") or (album or {}).get("album_id") or "").strip()
        cover = (album or {}).get("cover", "") if not (album or {}).get("_is_liked_album") else track.get("_real_album_cover", "")
        self._save_ui_state(last_played_track={
            "title": track.get("title", ""),
            "artist_name": artist_name,
            "album_title": album_title,
            "album_cover": cover,
            "album_id": album_id,
        })

    def _resolve_playing_cover_rel(self, album: dict) -> str:
        """Cover path for the currently playing album — prefers the real
        album cover from the track when playing the liked-tracks virtual album."""
        cover_rel = (album or {}).get("cover", "")
        if album and album.get("_is_liked_album") and self.player.current_track_idx is not None:
            try:
                track = album["tracks"][self.player.current_track_idx]
                real = track.get("_real_album_cover", "")
                if real:
                    cover_rel = real
            except (IndexError, KeyError, TypeError):
                pass
        return cover_rel

    def _load_controls_cover(self, album: dict):
        cover_rel = self._resolve_playing_cover_rel(album)
        if not cover_rel or (os.path.isabs(cover_rel) and os.path.exists(cover_rel)):
            self._controls.set_cover(None)
            return
        cover_url = SERVER_URL + cover_rel if not cover_rel.startswith("http") else cover_rel
        key = cache_key(cover_url, 56, 6)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._controls.set_cover(cached)
            return

        def on_loaded(url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(url, size, radius), pm)
                    self._controls.set_cover(pm)
            except Exception:
                pass

        _start_image_loader([cover_url], 56, 6, on_loaded, self._img_runners)

    def _open_now_playing_disc(self):
        album = self.player.current_playing_album
        if not album:
            return
        cover_rel = self._resolve_playing_cover_rel(album)
        if not cover_rel:
            return
        self._disc_overlay.show_for(cover_rel, self.player.is_playing())

    def _on_playback_state_changed(self, is_playing: bool):
        self._controls.set_playing(is_playing)
        self._disc_overlay.set_playing(is_playing)
        self._schedule_discord_presence_refresh(90)
        self._schedule_discord_presence_refresh(650)

    def _on_position_changed(self):
        pos = self.player.get_current_position()
        dur = self.player.get_duration()
        self._controls.update_position(pos, dur)

    def _on_duration_changed(self):
        pos = self.player.get_current_position()
        dur = self.player.get_duration()
        self._controls.update_position(pos, dur)

    def _on_album_finished(self, artist: dict, album: dict) -> bool:
        """Repeat is off and the album just played through — instead of just
        stopping, move on to the artist's next album (wrapping back to their
        first once the last one finishes), so a whole artist keeps playing
        continuously rather than going silent after one album."""
        if not artist or not album or album.get("_is_liked_album"):
            return False
        artist_name = (artist.get("artist") or "").strip()
        if not artist_name:
            return False
        full_artist = self.library_manager.get_artist_by_name(artist_name) or artist
        albums = full_artist.get("albums") or []
        if len(albums) < 1:
            return False
        current_title = clean_title(album.get("title", ""))
        current_idx = next(
            (i for i, al in enumerate(albums) if clean_title(al.get("title", "")) == current_title),
            None,
        )
        if current_idx is None:
            return False
        next_album = albums[(current_idx + 1) % len(albums)]
        self._play_track(0, next_album, full_artist)
        return True

    def _on_album_previous(self, artist: dict, album: dict) -> bool:
        return False

    # ── Download ──────────────────────────────────────────────────────────────

    def _download_album(self, album: dict, artist: dict, folder: str):
        tracks = album.get("tracks", []) or []
        artist_name = clean_artist_name(artist.get("artist", "") or "Unknown")
        album_name = clean_title(album.get("title", "") or "Unknown")
        dest_dir = os.path.join(folder, _safe_filename(artist_name), _safe_filename(album_name))

        tasks = []
        for i, track in enumerate(tracks):
            rel = track.get("url", "")
            if not rel:
                continue
            url = SERVER_URL + rel if not rel.startswith("http") else rel
            filename = f"{i + 1:02d} - {_safe_filename(clean_title(track.get('title', '') or 'track'))}.mp3"
            tasks.append({"url": url, "path": os.path.join(dest_dir, filename), "label": filename})

        if not tasks:
            return
        self._run_download(tasks, f"Скачивание альбома «{album_name}»")

    def _download_track(self, album: dict, artist: dict, track_idx: int, folder: str):
        tracks = album.get("tracks", []) or []
        if track_idx >= len(tracks):
            return
        track = tracks[track_idx]
        rel = track.get("url", "")
        if not rel:
            return
        url = SERVER_URL + rel if not rel.startswith("http") else rel
        title = _safe_filename(clean_title(track.get("title", "") or "track"))
        filename = f"{track_idx + 1:02d} - {title}.mp3"
        tasks = [{"url": url, "path": os.path.join(folder, filename), "label": filename}]
        self._run_download(tasks, f"Скачивание «{title}»")

    def _run_download(self, tasks: list, title: str):
        progress = QProgressDialog(title, "Отмена", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        worker = DownloadWorker(tasks)
        thread = QThread(QApplication.instance())
        worker.moveToThread(thread)

        def on_progress(file_num, total_files, downloaded, total_bytes, label):
            if total_bytes > 0:
                pct = int((file_num - 1) / total_files * 100 + downloaded / total_bytes / total_files * 100)
                progress.setValue(min(pct, 99))
            progress.setLabelText(f"{file_num}/{total_files}: {label}")
            if progress.wasCanceled():
                worker.cancel()

        def on_finished(success, failed, cancelled):
            progress.setValue(100)
            progress.close()
            if not cancelled:
                msg = f"Скачано: {success}"
                if failed:
                    msg += f", ошибок: {failed}"
                QMessageBox.information(self, "Готово", msg)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.finished.connect(thread.quit)
        thread.started.connect(worker.run)
        progress.canceled.connect(worker.cancel)
        thread.start()
        self._download_threads.append(thread)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_cover_viewer", None) and self._cover_viewer.isVisible():
            self._cover_viewer.setGeometry(self.rect())
        if getattr(self, "_disc_overlay", None) and self._disc_overlay.isVisible():
            self._disc_overlay.setGeometry(self.rect())

    # ── Close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        # Set before anything else so any QTimer/async callback still in
        # flight (library refresh, player-data fetch) bails out instead of
        # spawning a fresh background thread after teardown has started.
        self._closing = True
        self.player.stop()
        self._save_settings()
        if self._search_thread:
            try:
                if self._search_worker:
                    self._search_worker.abort()
                self._search_thread.quit()
                self._search_thread.wait(500)
            except Exception:
                pass
        for t in (self._library_thread, self._player_data_thread):
            if t:
                try:
                    t.quit()
                    t.wait(300)
                except Exception:
                    pass
        self._album_page._stop_duration_loader()
        for t in list(_dying_threads):
            try:
                t.wait(300)
            except Exception:
                pass
        # Stop image loader runners ([thread, worker] pairs) — MusicApp's own,
        # plus every child page/overlay that spawns its own cover loaders.
        # Leaving any of these running when the window closes is exactly what
        # causes "QThread: Destroyed while thread is still running" aborts, so
        # give each a brief grace period to actually finish, not just a signal to stop.
        _stop_runners_and_wait(self._img_runners)
        _stop_runners_and_wait(self._artist_page._runners)
        _stop_runners_and_wait(self._artist_page._album_grid._runners)
        _stop_runners_and_wait(self._album_page._runners)
        _stop_runners_and_wait(self._cover_viewer._runners)
        _stop_runners_and_wait(self._disc_overlay._runners)
        _stop_runners_and_wait(self._search_page._runners)
        _stop_runners_and_wait(self._sidebar._runners)
        # Stop bare download threads
        for t in list(self._download_threads):
            try:
                t.quit()
                t.wait(200)
            except Exception:
                pass
        if self._media_keys:
            try:
                self._media_keys.listener.stop()
            except Exception:
                pass
        # Any in-flight Discord connect attempt runs on a daemon thread —
        # nothing to wait on, it can't block or crash process exit.
        self._dispose_discord_rpc()
        super().closeEvent(event)


def _safe_filename(name: str) -> str:
    """Strip characters illegal in filenames."""
    import re
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip(". ") or "unknown"
