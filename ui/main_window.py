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
import uuid
import queue
import random
import base64
import threading
from functools import partial

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea, QGridLayout,
    QLineEdit, QApplication, QSizePolicy, QStackedWidget,
    QMenu, QFileDialog, QProgressDialog, QSpacerItem,
    QAbstractItemView, QFrame, QMessageBox, QGraphicsDropShadowEffect,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsBlurEffect, QGraphicsOpacityEffect,
    QStyledItemDelegate, QStyle, QSlider, QColorDialog, QInputDialog, QToolTip,
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, QUrl, QSize, QRect, QRectF, pyqtSignal, pyqtSlot, QObject, QPoint, QPointF,
    QPropertyAnimation, pyqtProperty, QEasingCurve, QBuffer,
)
from datetime import date, timedelta
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QImage, QFontMetrics, QCursor, QPainter, QPainterPath, QPen, QBrush, QLinearGradient, QDesktopServices

from config import SERVER_URL, APP_ICON, ICONS_DIR, APP_SETTINGS_FILE, PLAYER_DATA_CACHE_DIR, LIBRARY_CACHE_FILE, WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT, DATA_DIR, APP_VERSION, LOCAL_MUSIC_DIR
from core.library import LibraryManager, SearchResult
from core.local_library import ensure_local_music_dir, scan_local_library
from core.player_vlc import PlayerController, get_eq_band_frequencies
try:
    from core.account import AccountManager as _AccountManager
except ImportError:
    _AccountManager = None
try:
    from core.youtube import search_youtube as _search_youtube, resolve_stream_url as _resolve_youtube_stream
except ImportError:
    _search_youtube = None
    _resolve_youtube_stream = None
from ui.playback_controls import PlaybackControls, ClickableLabel
from ui.album_widget import AlbumWidget
from ui.changelog_data import CHANGELOG
from ui.shimmer_placeholder import ShimmerLabel
import ui.styles as styles_module
from ui.styles import COLORS, get_scrollbar_style, set_accent_color, set_theme, get_theme
from utils.format_utils import clean_title, clean_artist_name, format_duration, normalize_track_url, resolve_media_url
from utils.image_utils import make_rounded_pixmap, load_pixmap_from_url
from utils.cover_cache import cover_cache, cache_key
from workers.image_loader import ImageLoaderWorker
from workers.search_worker import SearchWorker
from workers.download_worker import DownloadWorker
from workers.track_duration_worker import TrackDurationWorker
from workers.lyrics_worker import LyricsWorker
from workers.artist_bio_worker import ArtistBioWorker
from workers.public_playlists_worker import PublicPlaylistsWorker

try:
    from utils.media_keys import MediaKeysHandler
    _MEDIA_KEYS_AVAILABLE = True
except ImportError:
    _MEDIA_KEYS_AVAILABLE = False

try:
    from utils.mpris_service import MPRISService, is_supported as _mpris_is_supported
    _MPRIS_AVAILABLE = True
except ImportError:
    _MPRIS_AVAILABLE = False


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


def _start_lyrics_worker(
    artist: str, title: str, album: str, duration_sec: int,
    on_finished_cb, runners_store: list,
):
    """Same thread/worker bookkeeping as _start_image_loader, for a single
    one-shot LyricsWorker lookup instead of a queue of image URLs."""
    worker = LyricsWorker(artist, title, album, duration_sec)
    thread = QThread(QApplication.instance())
    worker.moveToThread(thread)

    entry = [thread, worker]
    runners_store.append(entry)

    def _cleanup():
        worker.deleteLater()
        thread.deleteLater()
        try:
            runners_store.remove(entry)
        except ValueError:
            pass

    worker.finished.connect(on_finished_cb)
    worker.finished.connect(thread.quit)
    thread.finished.connect(_cleanup)
    thread.started.connect(worker.run)
    thread.start()
    return entry


def _start_artist_bio_worker(artist: str, on_finished_cb, runners_store: list):
    """Same thread/worker bookkeeping as _start_lyrics_worker, for a single
    one-shot ArtistBioWorker lookup."""
    worker = ArtistBioWorker(artist)
    thread = QThread(QApplication.instance())
    worker.moveToThread(thread)

    entry = [thread, worker]
    runners_store.append(entry)

    def _cleanup():
        worker.deleteLater()
        thread.deleteLater()
        try:
            runners_store.remove(entry)
        except ValueError:
            pass

    worker.finished.connect(on_finished_cb)
    worker.finished.connect(thread.quit)
    thread.finished.connect(_cleanup)
    thread.started.connect(worker.run)
    thread.start()
    return entry


def _start_public_playlists_worker(artist_name: str, album_ids: list, limit: int, on_finished_cb, runners_store: list):
    """Same thread/worker bookkeeping as _start_artist_bio_worker, for a
    single one-shot PublicPlaylistsWorker lookup."""
    worker = PublicPlaylistsWorker(artist_name, album_ids, limit)
    thread = QThread(QApplication.instance())
    worker.moveToThread(thread)

    entry = [thread, worker]
    runners_store.append(entry)

    def _cleanup():
        worker.deleteLater()
        thread.deleteLater()
        try:
            runners_store.remove(entry)
        except ValueError:
            pass

    worker.finished.connect(on_finished_cb)
    worker.finished.connect(thread.quit)
    thread.finished.connect(_cleanup)
    thread.started.connect(worker.run)
    thread.start()
    return entry


# artist name (lowercased) -> bio text, shared for the whole process lifetime
# between the "Об исполнителе" now-playing card and the artist page — opening
# the same artist in both only ever fetches it once.
_artist_bio_cache: dict[str, str] = {}


def _lookup_artist_bio(artist: str, on_result_cb, runners_store: list):
    """Resolves `artist`'s bio via _artist_bio_cache when already known,
    otherwise fetches it through ArtistBioWorker and caches the result.
    on_result_cb(bio: str) is always called exactly once, synchronously on
    a cache hit or from the worker's finished signal on a miss — callers
    that care about staleness (the artist changing again before a fetch
    returns) need their own request-id guard inside the callback, same as
    _refresh_now_playing_lyrics does for lyrics."""
    key = artist.strip().lower()
    if not key:
        on_result_cb("")
        return
    cached = _artist_bio_cache.get(key)
    if cached is not None:
        on_result_cb(cached)
        return

    def on_finished(_artist, bio, _key=key):
        _artist_bio_cache[_key] = bio
        on_result_cb(bio)

    _start_artist_bio_worker(artist, on_finished, runners_store)


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


def _make_youtube_icon_pixmap(size: int) -> QPixmap:
    """Drawn in code (no bundled asset needed) so the search-source toggle
    button (see MusicApp._toggle_search_source) can flip to a recognizable
    YouTube glyph without shipping/licensing an actual logo image."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#FF0000"))
    r = size * 0.24
    painter.drawRoundedRect(QRectF(0, 0, size, size), r, r)
    painter.setBrush(QColor("#FFFFFF"))
    cx, cy = size / 2.0, size / 2.0
    tw, th = size * 0.34, size * 0.40
    triangle = QPainterPath()
    triangle.moveTo(cx - tw * 0.35, cy - th / 2.0)
    triangle.lineTo(cx - tw * 0.35, cy + th / 2.0)
    triangle.lineTo(cx + tw * 0.65, cy)
    triangle.closeSubpath()
    painter.drawPath(triangle)
    painter.end()
    return pm


def _track_identity_url(track: dict) -> str:
    """The URL that identifies a track for like/collection matching. For a
    YouTube track this must be the permanent youtube.com/watch link, not
    track["url"] — once played, that's overwritten in-memory with a
    resolved googlevideo.com stream (see MusicApp._resolve_track_url_for_player),
    which is different on every play and would never match what's actually
    stored in liked_tracks/playlists (see MusicApp._build_track_ref)."""
    track = track or {}
    return track.get("_permanent_url") or track.get("url", "")


def _track_like_keys(track: dict, url: str = "") -> set:
    """Keys identifying a track for like/playing-state matching.

    A URL is authoritative whenever one is available — it points at one
    specific file, so two tracks with different URLs are never "the same
    track" for like/now-playing purposes, no matter what else matches.
    Previously an album_id + normalized-title key was always added
    alongside it (to let a like on one artist's copy of a shared/duplicated
    album also match the other artist's identical copy) — but a title alone
    doesn't actually identify a track: an album can genuinely contain two
    different tracks that happen to share a title (e.g. two different
    bonus-edition masters of one song), and that fallback key made them
    collide — liking or playing one lit up both. The fallback is now only
    used when a track has no URL at all to go on.
    """
    keys = set()
    u = url or (track or {}).get("url", "") or ""
    if u:
        keys.add(u)
        if u.startswith("http"):
            keys.add(re.sub(r'^https?://[^/]+', '', u))
        elif not u.startswith("file://"):
            # file:// (local-library track) is already an absolute,
            # unique identifier on its own — no server-relative form to
            # add an alternate key for.
            keys.add(SERVER_URL + u)
        return keys
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


def _decode_base64_pixmap(data: str) -> QPixmap | None:
    """Decode a base64-encoded image (e.g. a playlist's custom cover) into a
    QPixmap, or None if it's missing/corrupt."""
    if not data:
        return None
    try:
        pm = QPixmap()
        if pm.loadFromData(base64.b64decode(data)) and not pm.isNull():
            return pm
    except Exception:
        pass
    return None


def _dominant_cover_color(pm: QPixmap) -> QColor | None:
    """Extracts a representative accent color from a cover pixmap, the way
    Spotify/Apple Music tint their now-playing view — a saturation-weighted
    average over a small downscale, rather than a plain pixel average (which
    tends toward a muddy grey/brown once you mix a whole cover together).
    Near-black and near-white/near-grey pixels are excluded from the weighted
    pool since they're usually letterboxing or a plain background rather
    than "the color of this cover", but still count in a fallback plain
    average for all-grayscale covers (e.g. b&w photography) that would
    otherwise have nothing left to weight. Returns None for a null/empty
    pixmap."""
    if pm is None or pm.isNull():
        return None
    img = pm.toImage().scaled(16, 16, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.FastTransformation)
    img = img.convertToFormat(QImage.Format.Format_RGB32)

    weighted_r = weighted_g = weighted_b = weight_total = 0.0
    plain_r = plain_g = plain_b = 0
    count = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = QColor(img.pixel(x, y))
            r, g, b = c.red(), c.green(), c.blue()
            plain_r += r
            plain_g += g
            plain_b += b
            count += 1
            _, s, v, _ = c.getHsvF()
            if v < 0.12 or (s < 0.12 and v > 0.92):
                continue
            weight = s + 0.05
            weighted_r += r * weight
            weighted_g += g * weight
            weighted_b += b * weight
            weight_total += weight

    if count == 0:
        return None
    if weight_total > 0:
        return QColor(int(weighted_r / weight_total), int(weighted_g / weight_total), int(weighted_b / weight_total))
    return QColor(int(plain_r / count), int(plain_g / count), int(plain_b / count))


def _blend_color(base: QColor, tint: QColor, t: float) -> QColor:
    """Linear RGB blend of `base` toward `tint` by fraction `t` (0..1)."""
    return QColor(
        int(base.red() + (tint.red() - base.red()) * t),
        int(base.green() + (tint.green() - base.green()) * t),
        int(base.blue() + (tint.blue() - base.blue()) * t),
    )


def _make_placeholder_cover(size: int, radius: int, glyph: str = "♪") -> QPixmap:
    """Generated "no cover" tile for playlists — a plain rounded surface
    with a music-note glyph, instead of reusing the liked-tracks heart icon."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.setClipPath(path)
    painter.fillRect(0, 0, size, size, QColor(COLORS["SURFACE_LIGHT"]))
    painter.setPen(QColor(COLORS["TEXT_SECONDARY"]))
    font = QFont("Segoe UI", max(10, int(size * 0.34)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return pm


def _make_play_pause_icon(playing: bool, size: int, color: str) -> QPixmap:
    """Same triangle/bars shapes and proportions as _TrackPlayGlyph (so this
    matches the rest of the app's play/pause iconography pixel-for-pixel),
    rendered as a flat solid-color pixmap instead — for use as a QPushButton
    icon, e.g. "Слушать"/"Играет", where the button's own accent-gradient
    background makes an accent-colored glyph invisible. A plain text glyph
    ("▮▮") looked like a smudged blob rather than two distinct bars; this
    draws the actual shape instead of leaning on a font's rendering of it."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    cx, cy = size / 2, size / 2
    glyph = size * 0.72

    if playing:
        bar_w = glyph * 0.26
        gap = glyph * 0.28
        bar_h = glyph * 0.95
        for dx in (-(gap / 2 + bar_w / 2), gap / 2 + bar_w / 2):
            p.drawRoundedRect(QRectF(cx + dx - bar_w / 2, cy - bar_h / 2, bar_w, bar_h), 1.0, 1.0)
    else:
        h = glyph * 0.95
        w = h * 0.87
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(0, h)
        path.lineTo(w, h / 2)
        path.closeSubpath()
        optical_nudge = w * 0.12  # same optical-centering nudge as _CircleButton/_TrackPlayGlyph
        path.translate(cx - w / 2 + optical_nudge, cy - h / 2)
        p.drawPath(path)
    p.end()
    return pm


# _settings keys that describe *this machine*, not the user's account —
# never synced to the server (a different device's audio device ID or media
# key support wouldn't mean anything here).
_LOCAL_ONLY_SETTINGS_KEYS = {
    "audio_output_device_id", "global_media_keys",
    # Эквалайзер настроен под конкретные наушники/колонки этой машины — на
    # другом устройстве (другой ПК, тем более телефон без поддержки EQ)
    # тот же изгиб частот скорее всего будет звучать плохо, поэтому не
    # синхронизируем это через аккаунт, как audio_output_device_id.
    "eq_enabled", "eq_preamp", "eq_bands",
}

# _settings keys that are baked into every widget/the whole QApplication at
# construction time — changing them live isn't practical, so a value that
# arrives from the server different from what's already running needs a
# restart prompt (same as changing them locally in Settings). accent_color
# is deliberately not here — unlike theme/scale, it already applies live
# (see _refresh_accent_widgets()).
_RESTART_REQUIRED_SETTINGS_KEYS = {"theme", "ui_scale"}


class _AccentGradientLabel(QLabel):
    """A QLabel whose text renders in the accent color — a genuine two-stop
    horizontal gradient brush when the user picked a second accent color,
    otherwise identical to plain QLabel painting (driven by whatever
    stylesheet is already set on it, e.g. `color: {PRIMARY}`). Drop-in
    replacement for QLabel anywhere text is colored with the accent —
    playing-track titles, section headers, the logo, etc.

    Defaults to always-accent (matches labels whose text is permanently
    accent-colored, e.g. the logo or a section header). For a label that
    only sometimes shows the accent color (e.g. a track title that's
    accent-colored while playing but plain white otherwise), the caller
    MUST call set_accent_active(False) for the "otherwise" case — without
    it this widget has no way to know the accent gradient shouldn't apply
    right now, and would gradient-paint the label's text unconditionally
    any time gradient mode is on, regardless of what color it's actually
    supposed to be at that moment."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._accent_active = True

    def set_accent_active(self, active: bool):
        active = bool(active)
        if self._accent_active != active:
            self._accent_active = active
            self.update()

    def paintEvent(self, event):
        if not (self._accent_active and styles_module.is_gradient_accent()):
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self.font())
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, QColor(styles_module.get_accent()))
        grad.setColorAt(1.0, QColor(styles_module.get_accent_secondary() or styles_module.get_accent()))
        p.setPen(QPen(QBrush(grad), 0))
        p.drawText(self.rect(), int(self.alignment()), self.text())
        p.end()


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
                cover_url = resolve_media_url(album["cover"])

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
    """Pre-built page: artist header, bio, then a capped one-row preview of
    their albums (see _fill_album_row) with a "Показать всё" through to
    ArtistAllAlbumsPage for the rest — added instead of loading every
    album's cover up front like the old full grid did here, which is what
    actually made visiting a prolific artist's page slow."""
    album_clicked = pyqtSignal(dict, dict)  # (album, artist)
    artist_like_clicked = pyqtSignal()
    show_all_albums_clicked = pyqtSignal(dict)  # (artist,)
    track_play_requested = pyqtSignal(int, dict, dict)  # (track_idx_in_album, album, artist)
    track_like_clicked = pyqtSignal(dict, dict, dict)  # (track, album, artist) — album varies per row here
    playlist_clicked = pyqtSignal(dict)  # {"id", "name", "cover_data", "owner_login"} — see _fill_playlist_row

    ROW_ALBUM_CAP = 14  # cards actually built + cover-loaded; how many of
    # them are shown is separately capped by how many fit the current width
    # (see _reflow_album_row) — this is just the outer ceiling so an
    # artist with dozens of albums never gets them all loaded up front.
    _ALBUM_CARD_STEP = 170 + 16  # AlbumWidget width + row spacing

    RANDOM_TRACKS_INITIAL = 5
    RANDOM_TRACKS_MAX = 10

    PLAYLIST_CARD_CAP = 14  # random sample size when more than this many of
    # the account's own playlists feature the artist

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_artist: dict = {}
        self._runners: list = []          # artist header cover only
        self._album_row_runners: list = []   # kept separate from self._runners —
        # _fill_album_row cancels this list at its own start on every
        # load_artist() call, same as HomePage's _fill_albums; sharing it
        # with the header cover loader used to mean that cancellation also
        # killed the header avatar fetch a few milliseconds after it
        # started, before it ever got a chance to call back — the avatar
        # would then just keep showing whatever the previous artist's was.
        self._bio_runners: list = []
        self._bio_request_id = 0
        self._is_liked: bool = False
        self._album_cards: list = []
        self._total_album_count = 0
        self._random_cover_runners: list = []
        self._random_duration_worker: TrackDurationWorker | None = None
        self._random_track_rows: list = []
        self._random_tracks_pool: list = []  # [(track_idx_in_album, track, album), ...]
        self._random_shown_count = 0
        self._random_expanded = False
        self._last_playing_url = ""
        self._last_playing_track: dict | None = None
        self._last_paused = False
        self._playlist_cards: list = []
        self._playlist_runners: list = []
        self._playlist_request_id = 0
        self._build_ui()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._reflow_playlist_row)
        QTimer.singleShot(0, self._reflow_album_row)

    def _build_ui(self):
        # Scroll-wrapped (rather than a plain QVBoxLayout directly on self)
        # since this page used to rely on the old full album grid's own
        # internal QScrollArea to absorb any overflow — now that grid is
        # gone (replaced by the one-row preview), nothing else here scrolls,
        # and header + bio + up to 10 random tracks + the album row easily
        # exceeds the window's minimum height on its own. Without this,
        # content past the bottom edge doesn't just get clipped cleanly —
        # widgets that manage their own height dynamically (the random
        # tracks list growing from 5 to 10 rows on "Ещё") fight the page's
        # fixed available height and visibly overlap instead.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page_scroll.setStyleSheet(get_scrollbar_style())

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 16, 20, 16)
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
        info_col.addLayout(name_row)

        self._album_count_label = QLabel()
        self._album_count_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        info_col.addWidget(self._album_count_label)

        # Pill button styled like AlbumPage's "Слушать" — solid/accent when not
        # subscribed ("Подписаться"), outlined once subscribed ("Вы подписаны").
        # Sits below the album count, left-aligned, rather than crowding the name.
        self._artist_like_btn = QPushButton("Подписаться")
        self._artist_like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._artist_like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._artist_like_btn.setFixedHeight(34)
        self._artist_like_btn.clicked.connect(self.artist_like_clicked.emit)
        like_btn_row = QHBoxLayout()
        like_btn_row.setContentsMargins(0, 4, 0, 0)
        like_btn_row.addWidget(self._artist_like_btn)
        like_btn_row.addStretch(1)
        info_col.addLayout(like_btn_row)

        info_col.addStretch(1)
        header_row.addLayout(info_col, 1)
        layout.addWidget(header, 0)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {COLORS['BORDER']};")
        layout.addWidget(divider, 0)

        # Bio — hidden until a lookup actually returns text (see _load_bio),
        # since not every artist has one available.
        self._bio_label = QLabel("")
        self._bio_label.setWordWrap(True)
        self._bio_label.setFont(QFont("Segoe UI", 10))
        self._bio_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
        self._bio_label.setVisible(False)
        layout.addWidget(self._bio_label, 0)

        # ── Random tracks — a small taster row pulled from across every
        # album, shown above "Музыка" itself (see RANDOM_TRACKS_INITIAL/MAX,
        # _fill_random_tracks). Whole section hidden when the artist has no
        # tracks at all instead of showing an empty header.
        self._random_section = QWidget()
        random_section_layout = QVBoxLayout(self._random_section)
        random_section_layout.setContentsMargins(0, 0, 0, 0)
        random_section_layout.setSpacing(12)

        random_label = QLabel("Случайные треки")
        random_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        random_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        random_section_layout.addWidget(random_label)

        self._random_tracks_container = QWidget()
        self._random_tracks_layout = QVBoxLayout(self._random_tracks_container)
        self._random_tracks_layout.setContentsMargins(0, 0, 0, 0)
        self._random_tracks_layout.setSpacing(2)
        random_section_layout.addWidget(self._random_tracks_container)

        # Below the list, under the last row (5 or 10) — not up in the
        # header — toggles between expanding to 10 and collapsing back to 5.
        self._random_toggle_btn = QPushButton("Ещё")
        self._random_toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._random_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._random_toggle_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {COLORS['TEXT_SECONDARY']}; "
            f"font: 9.5pt 'Segoe UI'; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        self._random_toggle_btn.clicked.connect(self._on_random_toggle_clicked)
        self._random_toggle_btn.setVisible(False)
        toggle_row = QHBoxLayout()
        toggle_row.addStretch(1)
        toggle_row.addWidget(self._random_toggle_btn)
        toggle_row.addStretch(1)
        random_section_layout.addLayout(toggle_row)

        layout.addWidget(self._random_section, 0)

        music_hdr_row = QHBoxLayout()
        music_hdr_row.setSpacing(10)
        music_label = QLabel("Музыка")
        music_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        music_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        music_hdr_row.addWidget(music_label)
        music_hdr_row.addStretch(1)
        self._show_all_btn = QPushButton("Показать всё")
        self._show_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._show_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._show_all_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {COLORS['TEXT_SECONDARY']}; "
            f"font: 9.5pt 'Segoe UI'; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        self._show_all_btn.clicked.connect(
            lambda: self.show_all_albums_clicked.emit(self._current_artist)
        )
        self._show_all_btn.setVisible(False)
        music_hdr_row.addWidget(self._show_all_btn)
        layout.addLayout(music_hdr_row)

        # One-row album preview, styled like the old full grid's cards —
        # no scrolling/arrows, just however many actually fit the current
        # width (see _reflow_album_row); shrinks the visible count on a
        # narrower window instead of wrapping to a second row or scrolling.
        self._album_row_widget = QWidget()
        self._album_row_layout = QHBoxLayout(self._album_row_widget)
        self._album_row_layout.setContentsMargins(0, 0, 0, 0)
        self._album_row_layout.setSpacing(16)
        layout.addWidget(self._album_row_widget, 0)

        # ── Playlists featuring this artist — same one-row, fit-to-width
        # card style as the album row above (see _reflow_playlist_row).
        # Only ever searches the account's own playlists (see
        # _fill_playlist_row) — "+"-ed/subscribed ones aren't stored with
        # their track list locally, so checking those would mean a network
        # round trip per playlist just to find out whether any of them even
        # qualify. Whole section stays hidden when nothing matches.
        self._playlist_section = QWidget()
        playlist_section_layout = QVBoxLayout(self._playlist_section)
        playlist_section_layout.setContentsMargins(0, 0, 0, 0)
        playlist_section_layout.setSpacing(12)

        playlist_label = QLabel("Плейлисты, в которых есть исполнитель")
        playlist_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        playlist_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        playlist_section_layout.addWidget(playlist_label)

        self._playlist_row_widget = QWidget()
        self._playlist_row_layout = QHBoxLayout(self._playlist_row_widget)
        self._playlist_row_layout.setContentsMargins(0, 0, 0, 0)
        self._playlist_row_layout.setSpacing(16)
        playlist_section_layout.addWidget(self._playlist_row_widget)

        layout.addWidget(self._playlist_section, 0)
        self._playlist_section.setVisible(False)

        layout.addStretch(1)

        page_scroll.setWidget(content)
        root.addWidget(page_scroll)

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
            cover_url = resolve_media_url(cover_rel)
            self._load_artist_cover(cover_url)
        else:
            self._cover_label.setPixmap(QPixmap())

        self._load_bio(name)
        self._fill_random_tracks(albums)
        self._fill_album_row(albums)
        self._fill_playlist_row(name, albums)

    def _fill_album_row(self, albums: list):
        _stop_runners(self._album_row_runners)
        row = self._album_row_layout
        while row.count():
            item = row.takeAt(0)
            if item and item.widget():
                # hide() immediately, not just deleteLater() — a removed
                # widget otherwise stays visible at its old geometry (still
                # a real child, just no longer layout-managed) until Qt
                # actually gets around to the deferred delete, which can
                # briefly paint it overlapping the newly reflowed cards.
                item.widget().hide()
                item.widget().deleteLater()
        self._album_cards.clear()
        self._total_album_count = len(albums)

        preview = albums[:self.ROW_ALBUM_CAP]
        if not preview:
            placeholder = QLabel("Нет альбомов")
            placeholder.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
            row.addWidget(placeholder, 0, Qt.AlignmentFlag.AlignTop)
            self._show_all_btn.setVisible(False)
            return

        cover_urls = []
        for album in preview:
            cover_url = resolve_media_url(album["cover"]) if album.get("cover") else ""
            card = AlbumWidget(album, cover_url, widget_size=170, cover_size=150)
            card.clicked.connect(self._on_album_clicked)
            row.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)
            self._album_cards.append(card)
            cover_urls.append(cover_url)
        row.addStretch(1)

        self._load_row_covers(cover_urls)
        # Deferred, not called synchronously — right after building these
        # cards, self._album_row_widget.width() can still be a stale value
        # from before layout has actually run this pass (same reason
        # AlbumGridWidget.resizeEvent defers its own _reflow()).
        QTimer.singleShot(0, self._reflow_album_row)

    def _reflow_album_row(self):
        """However many AlbumWidget cards fit the current width, edge to
        edge, stay visible; the rest are hidden (not wrapped to a second
        row, not scrollable) — matches how the old full grid's column count
        itself responded to the window width, just clamped to one row."""
        if not self._album_cards:
            return
        available = self._album_row_widget.width() or self.width()
        fit = max(1, available // self._ALBUM_CARD_STEP)
        visible = min(fit, len(self._album_cards))
        for i, card in enumerate(self._album_cards):
            card.setVisible(i < visible)
        self._show_all_btn.setVisible(self._total_album_count > visible)

    def _fill_playlist_row(self, artist_name: str, albums: list):
        """Kicks off a server-side search (see PublicPlaylistsWorker /
        server.py's /playlists/public_for_artist) for public playlists —
        from any account, not just this one — that feature this artist,
        since that's data no client has locally. Section stays as it was
        (or empty, on first load) until the result comes back."""
        _stop_runners(self._playlist_runners)
        album_ids = [
            str(a.get("album_id") or "").strip()
            for a in albums if isinstance(a, dict) and a.get("album_id")
        ]
        self._playlist_request_id += 1
        request_id = self._playlist_request_id

        def on_finished(playlists: list, _rid=request_id):
            if _rid != self._playlist_request_id:
                return  # a newer artist was opened before this returned
            self._apply_playlist_results(playlists)

        if not artist_name and not album_ids:
            self._apply_playlist_results([])
            return

        _start_public_playlists_worker(
            artist_name, album_ids, self.PLAYLIST_CARD_CAP, on_finished, self._playlist_runners
        )

    def _apply_playlist_results(self, playlists: list):
        row = self._playlist_row_layout
        while row.count():
            item = row.takeAt(0)
            if item and item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self._playlist_cards.clear()

        self._playlist_section.setVisible(bool(playlists))
        if not playlists:
            return

        for pl in playlists:
            if not isinstance(pl, dict):
                continue
            name = pl.get("name") or "Плейлист"
            card = AlbumWidget({"title": name}, "", widget_size=170, cover_size=150)
            pixmap = _decode_base64_pixmap(pl.get("cover_data") or "") or _make_placeholder_cover(150, 14)
            card.set_cover(pixmap)
            summary = {
                "id": pl.get("id", ""), "name": name,
                "cover_data": pl.get("cover_data") or "",
                "owner_login": pl.get("owner_login", ""),
            }
            card.clicked.connect(lambda _pl, s=summary: self.playlist_clicked.emit(s))
            row.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)
            self._playlist_cards.append(card)
        row.addStretch(1)
        QTimer.singleShot(0, self._reflow_playlist_row)

    def _reflow_playlist_row(self):
        """Same fit-to-width, single-row behavior as _reflow_album_row —
        the whole reason for it was "displayed also in one row like
        albums", so this reuses the identical approach rather than a
        different one."""
        if not self._playlist_cards:
            return
        available = self._playlist_row_widget.width() or self.width()
        fit = max(1, available // self._ALBUM_CARD_STEP)
        visible = min(fit, len(self._playlist_cards))
        for i, card in enumerate(self._playlist_cards):
            card.setVisible(i < visible)

    def _fill_random_tracks(self, albums: list):
        _stop_runners(self._random_cover_runners)
        self._stop_random_duration_loader()
        while self._random_tracks_layout.count():
            item = self._random_tracks_layout.takeAt(0)
            if item and item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self._random_track_rows.clear()

        pool = []
        for album in albums:
            for track_idx, track in enumerate(album.get("tracks", []) or []):
                if isinstance(track, dict):
                    pool.append((track_idx, track, album))

        self._random_tracks_pool = (
            random.sample(pool, min(self.RANDOM_TRACKS_MAX, len(pool))) if pool else []
        )
        self._random_shown_count = 0
        self._random_expanded = False
        self._random_toggle_btn.setText("Ещё")
        self._random_section.setVisible(bool(self._random_tracks_pool))
        if self._random_tracks_pool:
            self._reveal_random_tracks(self.RANDOM_TRACKS_INITIAL)
        self._random_toggle_btn.setVisible(len(self._random_tracks_pool) > self.RANDOM_TRACKS_INITIAL)

    def _reveal_random_tracks(self, up_to: int):
        target = min(up_to, len(self._random_tracks_pool))
        cover_pairs = []
        urls_needing_duration = []
        while self._random_shown_count < target:
            track_idx, track, album = self._random_tracks_pool[self._random_shown_count]
            row_idx = len(self._random_track_rows)
            row = TrackRow(track_idx, track, display_number=row_idx + 1)
            row.show_cover(True)
            row.play_requested.connect(
                lambda idx, al=album, ar=self._current_artist: self.track_play_requested.emit(idx, al, ar)
            )
            row.like_clicked.connect(
                lambda _idx, t=track, al=album, ar=self._current_artist: self.track_like_clicked.emit(t, al, ar)
            )
            self._random_tracks_layout.addWidget(row)
            self._random_track_rows.append(row)

            # A newly built row needs the current playing/paused state
            # applied right away — otherwise it sits un-highlighted until
            # the next track change even if it happens to be the track
            # already playing (e.g. right after load_artist(), or after
            # "Ещё" reveals rows 6-10 mid-playback).
            keys = _track_like_keys(self._last_playing_track or {}, self._last_playing_url)
            is_playing = bool(keys) and bool(row.track_identity_keys() & keys)
            row.set_playing(is_playing)
            if is_playing:
                row.set_paused(self._last_paused)

            cover_rel = album.get("cover", "")
            if cover_rel:
                cover_pairs.append((row_idx, resolve_media_url(cover_rel)))

            if not track.get("duration"):
                url = resolve_media_url(track.get("url", ""))
                if url:
                    urls_needing_duration.append((row_idx, url))

            self._random_shown_count += 1

        if cover_pairs:
            self._load_random_covers(cover_pairs)
        if urls_needing_duration:
            self._start_random_duration_loader(urls_needing_duration)

    def _collapse_random_tracks(self):
        self._stop_random_duration_loader()
        while len(self._random_track_rows) > self.RANDOM_TRACKS_INITIAL:
            row = self._random_track_rows.pop()
            self._random_tracks_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._random_shown_count = len(self._random_track_rows)

    def _on_random_toggle_clicked(self):
        if self._random_expanded:
            self._collapse_random_tracks()
            self._random_expanded = False
            self._random_toggle_btn.setText("Ещё")
        else:
            self._reveal_random_tracks(self.RANDOM_TRACKS_MAX)
            self._random_expanded = True
            self._random_toggle_btn.setText("Свернуть")

    def _load_random_covers(self, pairs: list):
        to_load = []
        for row_idx, url in pairs:
            key = cache_key(url, 36, 4)
            cached = cover_cache.get(key)
            if cached and not cached.isNull() and row_idx < len(self._random_track_rows):
                self._random_track_rows[row_idx].set_cover_pixmap(cached)
            else:
                to_load.append((row_idx, url))
        if not to_load:
            return

        # Multiple random-picked tracks very commonly share the same
        # album (and so the same cover url) — map to every row that wants
        # it, not just the first, since ImageLoaderWorker only fetches/
        # emits once per distinct url. Mapping to a single index left
        # every row past the first one blank once the image arrived,
        # which is exactly the "some tracks have no cover" bug this fixes.
        rows_by_url: dict[str, list[int]] = {}
        for row_idx, url in to_load:
            rows_by_url.setdefault(url, []).append(row_idx)
        load_urls = list(rows_by_url.keys())

        def on_loaded(url, img, size, radius):
            if img is None:
                return
            try:
                pm = QPixmap.fromImage(img)
                if pm.isNull():
                    return
                cover_cache.set(cache_key(url, size, radius), pm)
                for row_idx in rows_by_url.get(url, []):
                    if row_idx < len(self._random_track_rows):
                        self._random_track_rows[row_idx].set_cover_pixmap(pm)
            except Exception:
                pass

        _start_image_loader(load_urls, 36, 4, on_loaded, self._random_cover_runners)

    def _start_random_duration_loader(self, index_url_pairs: list):
        urls = [u for _, u in index_url_pairs]
        idx_map = {u: i for i, u in index_url_pairs}

        worker = TrackDurationWorker(urls, parent=self)
        self._random_duration_worker = worker

        def on_duration(url: str, ms: int):
            if ms <= 0:
                return
            idx = idx_map.get(url)
            if idx is not None and idx < len(self._random_track_rows):
                self._random_track_rows[idx].update_duration(ms)

        def on_finished(w=worker):
            if self._random_duration_worker is w:
                self._random_duration_worker = None
            w.deleteLater()

        worker.duration_ready.connect(on_duration)
        worker.finished.connect(on_finished)
        worker.start()

    def _stop_random_duration_loader(self):
        if self._random_duration_worker:
            try:
                self._random_duration_worker.stop()
            except Exception:
                pass
            self._random_duration_worker = None

    def _load_row_covers(self, urls: list):
        valid = [(i, u) for i, u in enumerate(urls) if u]
        if not valid:
            return
        to_load = []
        for i, url in valid:
            key = cache_key(url, 150, 14)
            cached = cover_cache.get(key)
            if cached and not cached.isNull() and i < len(self._album_cards):
                self._album_cards[i].set_cover(cached)
            else:
                to_load.append((i, url))
        if not to_load:
            return

        # A url can legitimately appear more than once (two albums sharing
        # a cover file) — map to every card that wants it, not just the
        # first, since ImageLoaderWorker only fetches/emits once per
        # distinct url. Mapping to a single index left every card past the
        # first one blank.
        cards_by_url: dict[str, list[int]] = {}
        for i, url in to_load:
            cards_by_url.setdefault(url, []).append(i)
        load_urls = list(cards_by_url.keys())

        def on_loaded(url, img, size, radius):
            if img is None:
                return
            try:
                pm = QPixmap.fromImage(img)
                if pm.isNull():
                    return
                cover_cache.set(cache_key(url, size, radius), pm)
                for card_idx in cards_by_url.get(url, []):
                    if card_idx < len(self._album_cards):
                        self._album_cards[card_idx].set_cover(pm)
            except Exception:
                pass

        _start_image_loader(load_urls, 150, 14, on_loaded, self._album_row_runners)

    def _load_bio(self, artist_name: str):
        _stop_runners(self._bio_runners)
        self._bio_label.setText("")
        self._bio_label.setVisible(False)
        if not artist_name:
            return
        self._bio_request_id += 1
        request_id = self._bio_request_id

        def on_result(bio: str, _rid=request_id):
            if _rid != self._bio_request_id:
                return  # a newer artist was opened before this returned
            self._bio_label.setText(bio)
            self._bio_label.setVisible(bool(bio))

        _lookup_artist_bio(artist_name, on_result, self._bio_runners)

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

    def mark_random_tracks_playing(self, url: str, track: dict | None = None):
        """Highlights (accent color + the number-column play/pause glyph,
        both built into TrackRow already) whichever random-tracks row
        matches the track currently playing — same matching/behavior as
        AlbumPage.mark_playing_url, just against self._random_track_rows
        instead of a single album's own tracklist."""
        self._last_playing_url = url
        self._last_playing_track = track
        keys = _track_like_keys(track or {}, url)
        for row in self._random_track_rows:
            row.set_playing(bool(keys) and bool(row.track_identity_keys() & keys))

    def set_random_tracks_paused(self, is_paused: bool):
        self._last_paused = is_paused
        for row in self._random_track_rows:
            row.set_paused(is_paused)

    def set_liked(self, liked: bool):
        self._is_liked = liked
        c = COLORS
        if liked:
            self._artist_like_btn.setText("Вы подписаны")
            self._artist_like_btn.setToolTip("Отписаться от исполнителя")
            self._artist_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1.5px solid {c['PRIMARY']}; border-radius: 17px; "
                f"color: {c['PRIMARY']}; font: 10pt 'Segoe UI'; font-weight: 600; padding: 0 18px; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; border-color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self._artist_like_btn.setText("Подписаться")
            self._artist_like_btn.setToolTip("Подписаться на исполнителя")
            self._artist_like_btn.setStyleSheet(
                f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 17px; "
                f"color: #000; font: 10pt 'Segoe UI'; font-weight: 600; padding: 0 18px; }}"
                f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
            )

    def apply_accent(self):
        self.set_liked(self._is_liked)
        for row in self._random_track_rows:
            row.apply_accent()


class ArtistAllAlbumsPage(QWidget):
    """Every album from one artist, plain — reached via ArtistPage's
    "Показать всё". Kept as its own page rather than expanding the row
    strip in place specifically so ArtistPage's own visit never has to load
    every cover; this page only starts loading them once the user actually
    asks to see everything."""
    album_clicked = pyqtSignal(dict, dict)  # (album, artist)
    back_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_artist: dict = {}
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 0)
        layout.setSpacing(16)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)

        back_btn = QPushButton("←")
        back_btn.setFixedSize(34, 34)
        back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(
            f"QPushButton {{ background: {COLORS['SURFACE_LIGHT']}; border: none; border-radius: 17px; "
            f"color: {COLORS['TEXT_PRIMARY']}; font: 13pt 'Segoe UI'; }}"
            f"QPushButton:hover {{ background: {COLORS['SURFACE_HOVER']}; }}"
        )
        back_btn.clicked.connect(self.back_clicked.emit)
        header_row.addWidget(back_btn)

        self._title_label = QLabel("Музыка")
        self._title_label.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        self._title_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        header_row.addWidget(self._title_label, 1)
        layout.addLayout(header_row, 0)

        self._album_grid = AlbumGridWidget(self)
        self._album_grid.album_clicked.connect(self._on_album_clicked)
        layout.addWidget(self._album_grid, 1)

    def load_artist(self, artist: dict):
        self._current_artist = artist
        name = clean_artist_name(artist.get("artist", "") or "")
        self._title_label.setText(f"Музыка — {name}" if name else "Музыка")
        self._album_grid.load_albums(artist.get("albums", []) or [], artist)

    def _on_album_clicked(self, album: dict):
        self.album_clicked.emit(album, self._current_artist)


# ──────────────────────────────────────────────────────────────────────────────
# Album / tracklist page
# ──────────────────────────────────────────────────────────────────────────────

class _TrackPlayGlyph(QWidget):
    """Hand-drawn play triangle / pause bars for the track-number column —
    the exact same shapes as the big circular play/pause button in the
    bottom bar (see _CircleButton in playback_controls.py), just without
    the circle backing and filled with the accent color/gradient instead of
    a fixed one. Drawn rather than a font glyph so it matches that button
    pixel-for-pixel instead of depending on font/size-dependent rendering."""

    _GLYPH_SIZE = 11.0  # fixed visual size, independent of the widget's own box

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playing = False  # True => bars, False => triangle

    def set_playing(self, playing: bool):
        playing = bool(playing)
        if self._playing != playing:
            self._playing = playing
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rf = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(styles_module.accent_brush(rf.left(), 0, rf.right(), 0))
        cx, cy = rf.center().x(), rf.center().y()
        size = self._GLYPH_SIZE

        if self._playing:
            bar_w = size * 0.26
            gap = size * 0.28
            bar_h = size * 0.95
            for dx in (-(gap / 2 + bar_w / 2), gap / 2 + bar_w / 2):
                p.drawRoundedRect(
                    QRectF(cx + dx - bar_w / 2, cy - bar_h / 2, bar_w, bar_h), 1.0, 1.0
                )
        else:
            h = size * 0.95
            w = h * 0.87
            path = QPainterPath()
            path.moveTo(0, 0)
            path.lineTo(0, h)
            path.lineTo(w, h / 2)
            path.closeSubpath()
            # Same optical-centering nudge as _CircleButton's triangle.
            optical_nudge = w * 0.12
            path.translate(cx - w / 2 + optical_nudge, cy - h / 2)
            p.drawPath(path)
        p.end()


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
        self._hovered = False
        self._is_playing_state = False  # this row is the current track (playing or paused)
        self._is_paused_state = False   # only meaningful while _is_playing_state is True
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

        self._num_label = _AccentGradientLabel(str(self._display_number))
        self._num_label.set_accent_active(False)  # plain TEXT_SECONDARY until this row is the playing track
        self._num_label.setFixedWidth(24)
        self._num_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._num_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';")
        row.addWidget(self._num_label)

        # Same fixed width as the label, its own glyph drawn at a fixed
        # visual size regardless of that box (see _GLYPH_SIZE) — only one
        # of the two is ever visible (toggled in _update_playing_icon()),
        # a hidden widget takes no layout space, so whichever is shown
        # ends up sitting in the exact same slot the other one just left.
        self._num_icon = _TrackPlayGlyph()
        self._num_icon.setFixedSize(24, 20)
        self._num_icon.hide()
        row.addWidget(self._num_icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(36, 36)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 4px;")
        self._cover_label.setVisible(False)
        row.addWidget(self._cover_label)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(0)

        title = clean_title(self._track.get("title", "")) or "Неизвестно"
        self._title_label = _AccentGradientLabel(title)
        self._title_label.set_accent_active(False)  # plain TEXT_PRIMARY until this row is the playing track
        self._title_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        self._title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title_col.addWidget(self._title_label)

        # Below the title (not inline) — mainly relevant for playlist tracks,
        # which can each come from a different artist/album.
        artist_name = self._track.get("artist_name", "")
        if artist_name:
            self._artist_label = QLabel(clean_artist_name(artist_name))
            self._artist_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 8.5pt 'Segoe UI';")
            self._artist_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            if self._track.get("_is_youtube"):
                artist_row = QHBoxLayout()
                artist_row.setContentsMargins(0, 0, 0, 0)
                artist_row.setSpacing(4)
                yt_icon_lbl = QLabel()
                yt_icon_lbl.setFixedSize(11, 11)
                yt_icon_lbl.setPixmap(_make_youtube_icon_pixmap(11))
                artist_row.addWidget(yt_icon_lbl)
                artist_row.addWidget(self._artist_label)
                artist_row.addStretch(1)
                title_col.addLayout(artist_row)
            else:
                title_col.addWidget(self._artist_label)

        row.addLayout(title_col, 1)

        self._like_btn = QPushButton("+")
        self._like_btn.setFixedSize(26, 26)
        self._like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._like_btn.setToolTip("Добавить в плейлист")
        self._like_btn.clicked.connect(lambda: self.like_clicked.emit(self._index))
        row.addWidget(self._like_btn)
        # Always occupies its 26x26 slot (never setVisible(False)) — hiding/showing
        # a layout-managed widget on hover made the row briefly reflow/widen every
        # time; painted fully transparent instead, so the reserved space never changes.
        self._apply_add_button_style()

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

    def _apply_add_button_style(self):
        c = COLORS
        if self._liked:
            color, hover = c['PRIMARY'], c['PRIMARY_HOVER']
        elif self._hovered:
            color, hover = c['TEXT_SECONDARY'], c['TEXT_PRIMARY']
        else:
            # Painted fully transparent (not hidden) so it keeps reserving
            # its 26x26 slot — see the comment where the button is built.
            color = hover = "transparent"
        self._like_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1.5px solid {color}; border-radius: 13px; "
            f"color: {color}; font-size: 13px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {hover}; border-color: {hover}; }}"
        )

    def set_liked(self, liked: bool):
        """Despite the name, this now means "belongs to at least one
        collection (liked tracks or a playlist)" — accent-colored and always
        shown when true, invisible-until-hovered otherwise."""
        self._liked = liked
        self._apply_add_button_style()

    def enterEvent(self, event):
        self._hovered = True
        self._apply_add_button_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._apply_add_button_style()
        super().leaveEvent(event)

    def track_url(self) -> str:
        url = self._track.get("url", "") or ""
        return url

    def track_identity_keys(self) -> set:
        # Playlists (unlike the liked-tracks page) hand this row the *live*
        # track dict, not a display-only copy — once played, a YouTube
        # track's url field gets overwritten in place with a resolved
        # stream (see MusicApp._resolve_track_url_for_player), which
        # differs every time. _track_identity_url prefers _permanent_url
        # when present so this keeps matching _playing_url either way,
        # whether this row's dict got mutated (playlists) or never does
        # (the liked-tracks page's own copy, which is why this bug didn't
        # show up there — its url simply never changes).
        return _track_like_keys(self._track, _track_identity_url(self._track))

    def set_playing(self, is_playing: bool):
        """Marks this row as the current track (playing OR paused) — call
        set_paused() separately to say which of the two it actually is."""
        self._is_playing_state = is_playing
        accent = COLORS["PRIMARY"]
        self._update_playing_icon()
        self._num_label.set_accent_active(is_playing)
        self._num_label.setStyleSheet(
            f"color: {accent}; font: 10pt 'Segoe UI';" if is_playing
            else f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';"
        )
        self._title_label.set_accent_active(is_playing)
        self._title_label.setStyleSheet(
            f"color: {accent}; font: 10pt 'Segoe UI';" if is_playing
            else f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';"
        )

    def set_paused(self, is_paused: bool):
        """Only changes the icon on the current-track row — actively playing
        shows the "||" bars, paused shows "▶" (harmless no-op on every other
        row, since _update_playing_icon() only branches on it when this row
        is also the current track)."""
        self._is_paused_state = is_paused
        self._update_playing_icon()

    def _update_playing_icon(self):
        if not self._is_playing_state:
            self._num_icon.hide()
            self._num_label.show()
            return
        self._num_icon.set_playing(not self._is_paused_state)
        self._num_label.hide()
        self._num_icon.show()

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
        if self._track.get("local"):
            # Already a local file — nothing to download.
            return
        menu = QMenu(self)
        menu.setStyleSheet(_menu_style())
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
    playlist_cover_edit_requested = pyqtSignal()
    playlist_creator_clicked = pyqtSignal(str)  # creator's login
    play_pause_toggle_requested = pyqtSignal()  # "Играет" clicked while this is the playing album/playlist

    _PLAY_ALL_TEXT_IDLE = "Слушать"
    _PLAY_ALL_TEXT_PLAYING = "Играет"
    _PLAY_ALL_ICON_SIZE = 18
    _PLAY_ALL_ICON_LEFT_INSET = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_album: dict = {}
        self._current_artist: dict = {}
        self._track_rows: list[TrackRow] = []
        self._disc_headers: list[QWidget] = []
        self._album_liked: bool = False
        self._is_current_playing_target: bool = False  # this album/playlist is what's loaded in the player
        self._duration_worker: TrackDurationWorker | None = None
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
        self._cover_label.mousePressEvent = self._on_cover_pressed
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

        # Shown instead of _artist_names_widget for playlists — the creator's
        # display name, clickable like an artist chip, opens their profile.
        self._playlist_creator_label = QPushButton("")
        self._playlist_creator_label.setFlat(True)
        self._playlist_creator_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._playlist_creator_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._playlist_creator_label.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {COLORS['TEXT_SECONDARY']}; "
            f"font: 10pt 'Segoe UI'; text-align: left; padding: 0; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; text-decoration: underline; }}"
        )
        self._playlist_creator_label.clicked.connect(
            lambda: self.playlist_creator_clicked.emit(self._current_album.get("_playlist_owner_login", ""))
        )
        self._playlist_creator_label.setVisible(False)
        artist_row.addWidget(self._playlist_creator_label)

        self._album_like_btn = QPushButton("+")
        self._album_like_btn.setFixedSize(30, 30)
        self._album_like_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._album_like_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._album_like_btn.setToolTip("Сохранить альбом в библиотеку")
        self._album_like_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1.5px solid {COLORS['TEXT_SECONDARY']}; "
            f"border-radius: 15px; color: {COLORS['TEXT_SECONDARY']}; font-size: 15px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {COLORS['TEXT_PRIMARY']}; border-color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        self._album_like_btn.clicked.connect(self.album_like_clicked.emit)
        artist_row.addWidget(self._album_like_btn)
        artist_row.addStretch(1)
        info_col.addLayout(artist_row)

        self._track_count_label = QLabel()
        self._track_count_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        info_col.addWidget(self._track_count_label)

        # Play all button — fixed width (fits the longer of the two labels)
        # so it doesn't resize when the text swaps between idle/playing.
        # The icon sits flush at the button's left edge while the text stays
        # centered across the whole button — QPushButton's own icon+text
        # can't be split like that (they move as one group), so the icon is
        # a separate transparent-to-clicks QLabel overlaid on top instead.
        # Icon is hand-drawn (see _make_play_pause_icon), not a font glyph —
        # a plain "▮▮" text character rendered as a blurry blob rather than
        # two distinct bars.
        self._play_all_btn = QPushButton(self._PLAY_ALL_TEXT_IDLE)
        self._play_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_all_btn.setFixedHeight(36)
        btn_font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        self._play_all_btn.setFont(btn_font)
        fm = QFontMetrics(btn_font)
        label_w = max(
            fm.horizontalAdvance(self._PLAY_ALL_TEXT_IDLE),
            fm.horizontalAdvance(self._PLAY_ALL_TEXT_PLAYING),
        )
        btn_w = label_w + 2 * self._PLAY_ALL_ICON_LEFT_INSET + self._PLAY_ALL_ICON_SIZE + 36
        self._play_all_btn.setFixedWidth(btn_w)
        self._play_all_btn.clicked.connect(self._on_play_all_clicked)
        c = COLORS
        self._play_all_btn.setStyleSheet(
            f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 18px; "
            f"color: #000; font: 10pt 'Segoe UI'; font-weight: 600; padding-left: 14px; }}"
            f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
        )

        self._play_all_icon = QLabel(self._play_all_btn)
        self._play_all_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._play_all_icon.setPixmap(_make_play_pause_icon(False, self._PLAY_ALL_ICON_SIZE, "#000"))
        icon_y = (36 - self._PLAY_ALL_ICON_SIZE) // 2
        self._play_all_icon.setGeometry(
            self._PLAY_ALL_ICON_LEFT_INSET, icon_y, self._PLAY_ALL_ICON_SIZE, self._PLAY_ALL_ICON_SIZE
        )

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 8, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addWidget(self._play_all_btn)

        self._dl_btn = dl_btn = QPushButton("↓ Скачать альбом")
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

        # Stretch goes *before* btn_row (not after) so it's the one absorbing
        # the gap between the meta text and the buttons — pushes "Слушать"/
        # "Скачать альбом" all the way down to sit flush with the cover's
        # bottom edge, matching the web client's .page-actions (margin-top:
        # auto in style.css), instead of just trailing right under the text.
        info_col.addStretch(1)
        info_col.addLayout(btn_row)
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

    def _on_cover_pressed(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._current_album.get("_is_playlist"):
            if self._current_album.get("_playlist_editable"):
                self.playlist_cover_edit_requested.emit()
            return
        if self._current_album.get("cover") and not self._current_album.get("_is_liked_album"):
            self.cover_clicked.emit(self._current_album, self._current_artist)

    def _on_play_all_clicked(self):
        if self._is_current_playing_target:
            # Already playing (or paused) this exact album/playlist — toggle
            # pause/resume in place instead of restarting from track 0.
            self.play_pause_toggle_requested.emit()
        else:
            self.track_play_requested.emit(0, self._current_album, self._current_artist)

    def set_playback_state(self, is_current_target: bool, is_playing: bool):
        """is_current_target: this exact album/playlist is what's loaded in
        the player right now (playing or paused). is_playing: playback is
        actively running — only meaningful together with is_current_target."""
        self._is_current_playing_target = is_current_target
        playing_now = is_current_target and is_playing
        self._play_all_icon.setPixmap(_make_play_pause_icon(playing_now, self._PLAY_ALL_ICON_SIZE, "#000"))
        self._play_all_btn.setText(self._PLAY_ALL_TEXT_PLAYING if playing_now else self._PLAY_ALL_TEXT_IDLE)

    def load_album(self, album: dict, artist: dict, playing_url: str = "", display_artist_names: list | None = None,
                    playing_track: dict | None = None, is_paused: bool = False):
        """Update this page for a different album."""
        self._current_album = album
        self._current_artist = artist
        self._stop_duration_loader()

        is_liked_album = bool(album.get("_is_liked_album"))
        is_playlist = bool(album.get("_is_playlist"))
        is_virtual = is_liked_album or is_playlist

        album_name = clean_title(album.get("title", "")) or "Неизвестно"
        artist_name = clean_artist_name(artist.get("artist", "")) or ""
        tracks = album.get("tracks", []) or []

        self._dl_btn.setVisible(not album.get("local"))

        self._album_name_label.setText(album_name)

        if is_playlist:
            self._type_label.setText("Плейлист")
            self._type_label.setVisible(True)
            self._set_artist_names([])
            self._playlist_creator_label.setText(album.get("_playlist_creator_name") or "")
            self._playlist_creator_label.setVisible(bool(album.get("_playlist_creator_name")))
        else:
            self._type_label.setText("Альбом")
            # The "liked tracks" virtual album isn't a real album and has no
            # real artist — showing the "Альбом" type label and an artist chip
            # that just reads "Неизвестно" (clean_artist_name's fallback for an
            # empty name) is pure noise here, so both are hidden for it.
            self._type_label.setVisible(not is_liked_album)
            self._playlist_creator_label.setVisible(False)
            names = [] if is_liked_album else (
                display_artist_names if display_artist_names else ([artist_name] if artist_name else [])
            )
            self._set_artist_names(names)

        editable_playlist = is_playlist and bool(album.get("_playlist_editable"))
        if is_playlist:
            self._cover_label.setCursor(
                Qt.CursorShape.PointingHandCursor if editable_playlist else Qt.CursorShape.ArrowCursor
            )
            self._cover_label.setToolTip("Изменить обложку плейлиста" if editable_playlist else "")
        else:
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
        cover_pm = _decode_base64_pixmap(album.get("_cover_data", "")) if is_playlist else None
        if cover_pm is not None:
            self._cover_label.setPixmap(make_rounded_pixmap(cover_pm, 180, 14))
        elif cover_rel:
            if os.path.isabs(cover_rel) and os.path.exists(cover_rel):
                pm = QPixmap(cover_rel)
                if not pm.isNull():
                    self._cover_label.setPixmap(make_rounded_pixmap(pm, 180, 14))
                else:
                    self._cover_label.setPixmap(QPixmap())
            else:
                cover_url = resolve_media_url(cover_rel)
                self._load_album_cover(cover_url)
        elif is_playlist:
            self._cover_label.setPixmap(_make_placeholder_cover(180, 14))
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
                row = TrackRow(i, track, display_number=display_number)
                row.play_requested.connect(lambda idx, al=album, ar=artist: self.track_play_requested.emit(idx, al, ar))
                row.download_requested.connect(self._on_track_download_requested)
                row.like_clicked.connect(lambda idx, t=track: self.track_like_clicked.emit(t))
                self._tracks_layout.insertWidget(self._tracks_layout.count() - 1, row)
                self._track_rows.append(row)

                if is_virtual:
                    row.show_cover(True)
                    cover_rel = track.get("_real_album_cover", "")
                    if cover_rel:
                        self._load_track_cover(row, cover_rel)

                if not track.get("duration"):
                    url = resolve_media_url(track.get("url", ""))
                    if url:
                        urls_needing_duration.append((i, url))
        finally:
            self._tracks_container.setUpdatesEnabled(True)

        if urls_needing_duration:
            self._start_duration_loader([(i, u) for i, u in urls_needing_duration])

        if playing_url or playing_track:
            self.mark_playing_url(playing_url, playing_track)
        self.set_paused(is_paused)

    def mark_playing(self, track_idx: int):
        for i, row in enumerate(self._track_rows):
            row.set_playing(i == track_idx)

    def mark_playing_url(self, url: str, track: dict | None = None):
        """Highlight the row whose track matches url — or, for albums shared
        across artists, the same album_id + normalized title; clear all others."""
        keys = _track_like_keys(track or {}, url)
        for row in self._track_rows:
            row.set_playing(bool(keys) and bool(row.track_identity_keys() & keys))

    def set_paused(self, is_paused: bool):
        """Switches the current-track row's icon between "||" (actually
        playing) and "▶" (paused) — see TrackRow.set_paused()."""
        for row in self._track_rows:
            row.set_paused(is_paused)

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
        url = resolve_media_url(cover_rel)
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

        # TrackDurationWorker owns a real QMediaPlayer, so — unlike the old
        # ffprobe-subprocess version — it must live on the main thread, not
        # a QThread; it's internally async (signals/timers) so this doesn't
        # block the UI.
        worker = TrackDurationWorker(urls, parent=self)
        self._duration_worker = worker

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

        def on_finished(w=worker):
            if self._duration_worker is w:
                self._duration_worker = None
            w.deleteLater()

        worker.duration_ready.connect(on_duration)
        worker.finished.connect(on_finished)
        worker.start()

    def _stop_duration_loader(self):
        if self._duration_worker:
            old_worker = self._duration_worker
            self._duration_worker = None
            try:
                old_worker.duration_ready.disconnect()
            except Exception:
                pass
            try:
                old_worker.finished.disconnect()
            except Exception:
                pass
            try:
                old_worker.stop()
            except Exception:
                pass
            old_worker.deleteLater()

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
        noun = "плейлист" if self._current_album.get("_is_playlist") else "альбом"
        if liked:
            self._album_like_btn.setToolTip(f"Убрать {noun} из библиотеки")
            self._album_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1.5px solid {c['PRIMARY']}; "
                f"border-radius: 15px; color: {c['PRIMARY']}; font-size: 15px; font-weight: 600; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; border-color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self._album_like_btn.setToolTip(f"Сохранить {noun} в библиотеку")
            self._album_like_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1.5px solid {c['TEXT_SECONDARY']}; "
                f"border-radius: 15px; color: {c['TEXT_SECONDARY']}; font-size: 15px; font-weight: 600; }}"
                f"QPushButton:hover {{ color: {c['TEXT_PRIMARY']}; border-color: {c['TEXT_PRIMARY']}; }}"
            )

    def refresh_track_likes(self, liked_keys: set):
        for row in self._track_rows:
            row.set_liked(bool(row.track_identity_keys() & liked_keys))

    def apply_accent(self):
        c = COLORS
        self._play_all_btn.setStyleSheet(
            f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 18px; "
            f"color: #000; font: 10pt 'Segoe UI'; font-weight: 600; padding-left: 14px; }}"
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
            f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 19px; "
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

        url = resolve_media_url(cover_rel)
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


class LyricsViewerOverlay(QWidget):
    """Full-window overlay for the current track's lyrics — opened via the
    button next to the volume slider (PlaybackControls.lyrics_clicked, see
    MusicApp._on_lyrics_button_clicked). Same "poverh vsego" treatment as
    CoverViewerOverlay (blurred backdrop snapshot, Escape/click-outside to
    close), but split instead of centered: cover settles into the left
    half, scrollable lyrics text into the right half, each sliding in from
    its own edge on open — cover_container/text_container are deliberately
    NOT layout-managed (a QLayout would fight setGeometry() every frame),
    their resting rects are computed by hand in _resting_rects().

    When lrclib.net has synced (LRC) lyrics for the track, each line is its
    own clickable QLabel instead of one big block of text — set_position()
    (fed from MusicApp._on_position_changed while this overlay is visible)
    highlights whichever line is currently playing and auto-scrolls it into
    view, and clicking a line emits line_clicked(ms) to seek there. The
    highlight itself crossfades rather than snapping — each line carries its
    own QGraphicsOpacityEffect, dimmed at rest and animated up to full
    brightness as it becomes current (and back down as it stops being
    current), alongside the already-animated auto-scroll. Falls back to one
    plain scrollable block when only unsynced text is available."""

    MARGIN = 56
    GAP = 40
    ANIM_MS = 320
    LINE_FADE_MS = 260
    OPACITY_IDLE = 0.45
    OPACITY_ACTIVE = 1.0

    # Idle/active differ in weight/size/background (snap instantly, applied
    # right when a line becomes/stops being current) — the actual "fade" the
    # user sees comes from the per-label QGraphicsOpacityEffect animating
    # between OPACITY_IDLE/OPACITY_ACTIVE on top of these, not from the
    # color itself, so both styles use plain full-white text here.
    _LINE_STYLE_IDLE = (
        "color: #FFFFFF; font: 11pt 'Segoe UI'; "
        "background: transparent; padding: 3px 4px; border-radius: 4px;"
    )
    _LINE_STYLE_ACTIVE = (
        "color: #FFFFFF; font: 600 12pt 'Segoe UI'; "
        "background: rgba(255,255,255,0.10); padding: 3px 4px; border-radius: 4px;"
    )

    line_clicked = pyqtSignal(int)  # ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backdrop_pixmap: QPixmap | None = None
        self._full_pixmap: QPixmap | None = None
        self._anim_cover: QPropertyAnimation | None = None
        self._anim_text: QPropertyAnimation | None = None
        self._scroll_anim: QPropertyAnimation | None = None
        self._runners: list = []
        self._synced_lines: list[tuple[int, str]] = []
        self._line_labels: list[QLabel] = []
        self._line_effects: list[QGraphicsOpacityEffect] = []
        self._line_fade_anims: dict[int, QPropertyAnimation] = {}
        self._active_line_idx: int = -1
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
        self._close_btn = QPushButton("✕", self)
        self._close_btn.setFixedSize(36, 36)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); border: none; border-radius: 18px; "
            "color: #FFFFFF; font-size: 15px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }"
        )
        self._close_btn.clicked.connect(self.hide_viewer)

        # ── Left: cover ──────────────────────────────────────────────────
        self._cover_container = QWidget(self)
        cover_col = QVBoxLayout(self._cover_container)
        cover_col.setContentsMargins(0, 0, 0, 0)
        cover_col.setSpacing(18)
        cover_col.addStretch(1)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 18)
        shadow.setColor(QColor(0, 0, 0, 180))
        self._image_label.setGraphicsEffect(shadow)
        cover_col.addWidget(self._image_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._caption_label = QLabel()
        self._caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption_label.setWordWrap(True)
        self._caption_label.setStyleSheet("color: #FFFFFF; font: 600 13pt 'Segoe UI'; background: transparent;")
        cover_col.addWidget(self._caption_label)
        cover_col.addStretch(1)

        # ── Right: scrollable lyrics ─────────────────────────────────────
        self._text_container = QWidget(self)
        text_col = QVBoxLayout(self._text_container)
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(14)

        lyrics_hdr = QLabel("Текст песни")
        lyrics_hdr.setStyleSheet("color: #FFFFFF; font: 600 13pt 'Segoe UI'; background: transparent;")
        text_col.addWidget(lyrics_hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(get_scrollbar_style() + "QScrollArea { background: transparent; }")
        self._scroll.viewport().setStyleSheet("background: transparent;")

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 16, 0)
        scroll_layout.setSpacing(0)

        # Unsynced fallback — one big wrapped block of plain text.
        self._plain_label = QLabel("")
        self._plain_label.setWordWrap(True)
        self._plain_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._plain_label.setStyleSheet(
            "color: rgba(255,255,255,0.92); font: 11pt 'Segoe UI'; background: transparent;"
        )
        scroll_layout.addWidget(self._plain_label)

        # Synced (LRC) — one clickable QLabel per line, filled in by
        # set_lyrics_data(); highlighted/scrolled to by set_position().
        self._lines_container = QWidget()
        self._lines_container.setStyleSheet("background: transparent;")
        self._lines_layout = QVBoxLayout(self._lines_container)
        self._lines_layout.setContentsMargins(0, 0, 0, 0)
        self._lines_layout.setSpacing(4)
        scroll_layout.addWidget(self._lines_container)
        self._lines_container.setVisible(False)

        scroll_layout.addStretch(1)
        self._scroll.setWidget(scroll_content)
        text_col.addWidget(self._scroll, 1)

    def _resting_rects(self) -> tuple[QRect, QRect]:
        m, gap = self.MARGIN, self.GAP
        top = m + 56  # leaves room under the close button
        avail_h = max(1, self.height() - top - m)
        half_w = max(1, int((self.width() - 2 * m - gap) / 2))
        cover_rect = QRect(m, top, half_w, avail_h)
        text_rect = QRect(m + half_w + gap, top, half_w, avail_h)
        return cover_rect, text_rect

    def _apply_resting_geometry(self):
        cover_rect, text_rect = self._resting_rects()
        self._cover_container.setGeometry(cover_rect)
        self._text_container.setGeometry(text_rect)
        self._resize_cover_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())
        self._close_btn.move(self.width() - self.MARGIN // 2 - 36, 20)
        self._apply_resting_geometry()

    def mousePressEvent(self, event):
        # Clicks land here only when they miss every child widget — the
        # dim/blurred backdrop itself.
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

    def show_for(self, title: str, artist_name: str, cover_rel: str):
        self._caption_label.setText(f"{title} • {artist_name}" if artist_name else title)
        self._full_pixmap = None
        self._resize_cover_pixmap()
        self._load_cover(cover_rel)

        if self.parent():
            self.setGeometry(self.parent().rect())
            snapshot = self.parent().grab()
            self._backdrop_pixmap = _blurred_backdrop(snapshot)
        self._close_btn.move(self.width() - self.MARGIN // 2 - 36, 20)

        cover_rect, text_rect = self._resting_rects()
        start_cover = QRect(-cover_rect.width(), cover_rect.y(), cover_rect.width(), cover_rect.height())
        start_text = QRect(self.width(), text_rect.y(), text_rect.width(), text_rect.height())
        self._cover_container.setGeometry(start_cover)
        self._text_container.setGeometry(start_text)

        self.raise_()
        self.show()
        self.setFocus()
        self.update()

        self._anim_cover = QPropertyAnimation(self._cover_container, b"geometry", self)
        self._anim_cover.setDuration(self.ANIM_MS)
        self._anim_cover.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_cover.setStartValue(start_cover)
        self._anim_cover.setEndValue(cover_rect)
        self._anim_cover.start()

        self._anim_text = QPropertyAnimation(self._text_container, b"geometry", self)
        self._anim_text.setDuration(self.ANIM_MS)
        self._anim_text.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_text.setStartValue(start_text)
        self._anim_text.setEndValue(text_rect)
        self._anim_text.start()

    def _resize_cover_pixmap(self):
        if not self._full_pixmap or self._full_pixmap.isNull():
            self._image_label.setFixedSize(1, 1)
            self._image_label.setPixmap(QPixmap())
            return
        side = max(160, min(440, self._cover_container.width() - 20, self._cover_container.height() - 100))
        self._image_label.setFixedSize(side, side)
        scaled = self._full_pixmap.scaled(
            side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._image_label.setPixmap(scaled)

    def _load_cover(self, cover_rel: str):
        # Same original-resolution fetch as CoverViewerOverlay.show_for
        # (size=0, radius=0) — the bottom bar's own cached cover is only
        # 56px, too blurry once scaled up to this overlay's ~400px cover.
        if not cover_rel:
            return
        if os.path.isabs(cover_rel) and os.path.exists(cover_rel):
            self._full_pixmap = QPixmap(cover_rel)
            self._resize_cover_pixmap()
            return
        url = resolve_media_url(cover_rel)
        _stop_runners(self._runners)

        def on_loaded(_loaded_url, img, _size, _radius):
            pm = QPixmap.fromImage(img) if img else QPixmap()
            if not pm.isNull():
                self._full_pixmap = pm
                self._resize_cover_pixmap()

        _start_image_loader([url], 0, 0, on_loaded, self._runners)

    def _clear_lines(self):
        for anim in self._line_fade_anims.values():
            anim.stop()
        self._line_fade_anims = {}
        while self._lines_layout.count():
            item = self._lines_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._line_labels = []
        self._line_effects = []
        self._active_line_idx = -1

    def set_lyrics_loading(self):
        self._synced_lines = []
        self._clear_lines()
        self._lines_container.setVisible(False)
        self._plain_label.setVisible(True)
        self._plain_label.setStyleSheet(
            "color: rgba(255,255,255,0.6); font: italic 11pt 'Segoe UI'; background: transparent;"
        )
        self._plain_label.setText("Загрузка текста…")

    def set_lyrics_data(self, plain: str, synced: list[tuple[int, str]]):
        self._synced_lines = list(synced or [])
        self._clear_lines()

        if self._synced_lines:
            self._plain_label.setVisible(False)
            self._lines_container.setVisible(True)
            for ms, text in self._synced_lines:
                lbl = QLabel(text or "♪")
                lbl.setWordWrap(True)
                lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                lbl.setStyleSheet(self._LINE_STYLE_IDLE)
                lbl.mousePressEvent = lambda _e, _ms=ms: self.line_clicked.emit(_ms)
                effect = QGraphicsOpacityEffect(lbl)
                effect.setOpacity(self.OPACITY_IDLE)
                lbl.setGraphicsEffect(effect)
                self._lines_layout.addWidget(lbl)
                self._line_labels.append(lbl)
                self._line_effects.append(effect)
            return

        self._lines_container.setVisible(False)
        self._plain_label.setVisible(True)
        if plain:
            self._plain_label.setStyleSheet(
                "color: rgba(255,255,255,0.92); font: 11pt 'Segoe UI'; background: transparent;"
            )
            self._plain_label.setText(plain)
        else:
            self._plain_label.setStyleSheet(
                "color: rgba(255,255,255,0.6); font: italic 11pt 'Segoe UI'; background: transparent;"
            )
            self._plain_label.setText("Текст не найден")

    def _fade_line(self, idx: int, target_opacity: float):
        existing = self._line_fade_anims.pop(idx, None)
        if existing is not None:
            existing.stop()
        effect = self._line_effects[idx]
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(self.LINE_FADE_MS)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.setStartValue(effect.opacity())
        anim.setEndValue(target_opacity)
        anim.finished.connect(lambda _idx=idx: self._line_fade_anims.pop(_idx, None))
        self._line_fade_anims[idx] = anim
        anim.start()

    def set_position(self, ms: int):
        """Highlights whichever synced line is currently playing and
        scrolls it into view — called from MusicApp._on_position_changed
        on every ~500ms player tick while this overlay is visible. The
        highlight itself crossfades (see _fade_line) rather than snapping,
        alongside the already-animated scroll. A no-op when the current
        track has no synced lyrics (self._synced_lines empty, e.g.
        plain-text-only or nothing loaded yet)."""
        if not self._synced_lines:
            return
        idx = -1
        for i, (t, _text) in enumerate(self._synced_lines):
            if t <= ms:
                idx = i
            else:
                break
        if idx == self._active_line_idx:
            return
        if 0 <= self._active_line_idx < len(self._line_labels):
            self._line_labels[self._active_line_idx].setStyleSheet(self._LINE_STYLE_IDLE)
            self._fade_line(self._active_line_idx, self.OPACITY_IDLE)
        self._active_line_idx = idx
        if 0 <= idx < len(self._line_labels):
            lbl = self._line_labels[idx]
            lbl.setStyleSheet(self._LINE_STYLE_ACTIVE)
            self._fade_line(idx, self.OPACITY_ACTIVE)
            self._scroll_to_label(lbl)

    def _scroll_to_label(self, lbl: QLabel):
        content = self._scroll.widget()
        if not content:
            return
        y = lbl.mapTo(content, QPoint(0, 0)).y()
        target = y - self._scroll.viewport().height() // 2 + lbl.height() // 2
        bar = self._scroll.verticalScrollBar()
        target = max(bar.minimum(), min(bar.maximum(), target))
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
        self._scroll_anim = QPropertyAnimation(bar, b"value", self)
        self._scroll_anim.setDuration(220)
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.start()


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

        url = resolve_media_url(cover_rel)
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


class _AvatarCropCanvas(QWidget):
    """Discord-style pan & zoom picker: drag the image to reposition it,
    slider/wheel to zoom, fixed circular frame in the center marking what
    ends up as the avatar (it's always displayed circular — see
    _AvatarButton). Offset/scale are clamped so the frame is always fully
    covered by the image, never showing empty canvas."""

    FRAME_SIZE = 260
    CANVAS_SIZE = 300
    zoom_changed = pyqtSignal(int)  # 0-100, mirrors the external zoom slider

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.CANVAS_SIZE, self.CANVAS_SIZE)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._source: QPixmap | None = None
        self._min_scale = 1.0
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._dragging = False
        self._drag_start = QPointF()
        self._offset_start = QPointF()

    def set_image(self, pixmap: QPixmap):
        self._source = pixmap if pixmap and not pixmap.isNull() else None
        if self._source:
            w, h = self._source.width(), self._source.height()
            self._min_scale = self.FRAME_SIZE / max(1, min(w, h))
            self._scale = self._min_scale
            self._offset = QPointF(0.0, 0.0)
        self.update()

    def set_zoom(self, percent: int):
        """Driven by the external slider (0-100 -> min_scale..min_scale*4x)."""
        if not self._source:
            return
        t = max(0.0, min(1.0, percent / 100.0))
        self._scale = self._min_scale * (1.0 + 3.0 * t)
        self._clamp_offset()
        self.update()

    def _zoom_percent(self) -> int:
        max_scale = self._min_scale * 4.0
        if max_scale <= self._min_scale:
            return 0
        t = (self._scale - self._min_scale) / (max_scale - self._min_scale)
        return int(round(max(0.0, min(1.0, t)) * 100))

    def _clamp_offset(self):
        if not self._source:
            return
        img_w = self._source.width() * self._scale
        img_h = self._source.height() * self._scale
        max_x = max(0.0, (img_w - self.FRAME_SIZE) / 2.0)
        max_y = max(0.0, (img_h - self.FRAME_SIZE) / 2.0)
        self._offset = QPointF(
            min(max_x, max(-max_x, self._offset.x())),
            min(max_y, max(-max_y, self._offset.y())),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(24, 24, 24))

        if self._source:
            c = self.CANVAS_SIZE / 2.0
            img_w = self._source.width() * self._scale
            img_h = self._source.height() * self._scale
            x = c - img_w / 2.0 + self._offset.x()
            y = c - img_h / 2.0 + self._offset.y()
            painter.drawPixmap(QRectF(x, y, img_w, img_h), self._source, QRectF(self._source.rect()))

        outer = QPainterPath()
        outer.addRect(QRectF(self.rect()))
        fx = (self.CANVAS_SIZE - self.FRAME_SIZE) / 2.0
        inner = QPainterPath()
        inner.addEllipse(QRectF(fx, fx, self.FRAME_SIZE, self.FRAME_SIZE))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 165))
        painter.drawPath(outer.subtracted(inner))

        painter.setPen(QPen(QColor(255, 255, 255, 220), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(fx, fx, self.FRAME_SIZE, self.FRAME_SIZE))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._source:
            self._dragging = True
            self._drag_start = event.position()
            self._offset_start = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._dragging:
            delta = event.position() - self._drag_start
            self._offset = QPointF(self._offset_start.x() + delta.x(), self._offset_start.y() + delta.y())
            self._clamp_offset()
            self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
        if not self._source:
            return
        factor = 1.08 if event.angleDelta().y() > 0 else (1.0 / 1.08)
        max_scale = self._min_scale * 4.0
        self._scale = min(max_scale, max(self._min_scale, self._scale * factor))
        self._clamp_offset()
        self.update()
        self.zoom_changed.emit(self._zoom_percent())

    def get_cropped_pixmap(self, output_size: int = 512) -> QPixmap:
        if not self._source:
            return QPixmap()
        c = self.CANVAS_SIZE / 2.0
        img_x = c - (self._source.width() * self._scale) / 2.0 + self._offset.x()
        img_y = c - (self._source.height() * self._scale) / 2.0 + self._offset.y()
        fx = (self.CANVAS_SIZE - self.FRAME_SIZE) / 2.0
        src_x = (fx - img_x) / self._scale
        src_y = (fx - img_y) / self._scale
        src_size = self.FRAME_SIZE / self._scale
        src_rect = QRectF(src_x, src_y, src_size, src_size).intersected(QRectF(self._source.rect()))
        cropped = self._source.copy(src_rect.toRect())
        if cropped.isNull():
            return QPixmap()
        return cropped.scaled(
            output_size, output_size,
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )


class AvatarCropOverlay(QWidget):
    """Full-window overlay (same look/pattern as CoverViewerOverlay) for
    picking which region of a just-chosen image becomes the avatar, instead
    of always silently center-cropping it."""

    avatar_confirmed = pyqtSignal(QPixmap)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backdrop_pixmap: QPixmap | None = None
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
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 36)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); border: none; border-radius: 18px; "
            "color: #FFFFFF; font-size: 15px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }"
        )
        close_btn.clicked.connect(self._cancel)
        close_row.addWidget(close_btn)
        outer.addLayout(close_row)

        outer.addStretch(1)

        center = QVBoxLayout()
        center.setSpacing(16)

        title = QLabel("Выберите область для аватара")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #FFFFFF; font: 600 12pt 'Segoe UI'; background: transparent;")
        center.addWidget(title)

        self._canvas = _AvatarCropCanvas()
        self._canvas.zoom_changed.connect(self._sync_zoom_slider)
        center.addWidget(self._canvas, 0, Qt.AlignmentFlag.AlignHCenter)

        zoom_wrap = QWidget()
        zoom_wrap.setStyleSheet("background: transparent;")
        zoom_row = QHBoxLayout(zoom_wrap)
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.setSpacing(10)
        zoom_lbl = QLabel("🔍")
        zoom_lbl.setStyleSheet("background: transparent; font-size: 11pt;")
        zoom_row.addWidget(zoom_lbl)
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(0, 100)
        self._zoom_slider.setValue(0)
        self._zoom_slider.setFixedWidth(220)
        self._zoom_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._zoom_slider.valueChanged.connect(self._canvas.set_zoom)
        zoom_row.addWidget(self._zoom_slider)
        center.addWidget(zoom_wrap, 0, Qt.AlignmentFlag.AlignHCenter)

        btn_wrap = QWidget()
        btn_wrap.setStyleSheet("background: transparent;")
        btn_row = QHBoxLayout(btn_wrap)
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(12)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setFixedHeight(38)
        cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); border: none; border-radius: 19px; "
            "color: #FFFFFF; font: 600 10.5pt 'Segoe UI'; padding: 0 22px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }"
        )
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)

        self._save_btn = QPushButton("Сохранить")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setFixedHeight(38)
        self._save_btn.clicked.connect(self._confirm)
        btn_row.addWidget(self._save_btn)
        center.addWidget(btn_wrap, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addLayout(center)
        outer.addStretch(2)
        self.apply_accent()

    def apply_accent(self):
        c = COLORS
        self._save_btn.setStyleSheet(
            f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 19px; "
            f"color: #000; font: 600 10.5pt 'Segoe UI'; padding: 0 22px; }}"
            f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
        )

    def _sync_zoom_slider(self, value: int):
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(value)
        self._zoom_slider.blockSignals(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)

    def show_for(self, pixmap: QPixmap):
        self._canvas.set_image(pixmap)
        self._sync_zoom_slider(0)
        if self.parent():
            self.setGeometry(self.parent().rect())
            snapshot = self.parent().grab()
            self._backdrop_pixmap = _blurred_backdrop(snapshot)
        self.raise_()
        self.show()
        self.setFocus()
        self.update()

    def _cancel(self):
        self.hide()

    def _confirm(self):
        cropped = self._canvas.get_cropped_pixmap(512)
        self.hide()
        if not cropped.isNull():
            self.avatar_confirmed.emit(cropped)


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
        pen = QPen()
        pen.setBrush(styles_module.accent_brush(0, 0, self._diameter, 0))
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

    def show_message(self, text: str):
        """Empty-results state with a specific reason (e.g. a YouTube search
        failure) instead of the generic "Ничего не найдено" — update_results([])
        can't tell "nothing matched" apart from "the search itself broke",
        which made the latter impossible to diagnose from a windowed app."""
        self.set_loading(False)
        self._results = []
        self._rebuild_list(empty_text=text)

    def _rebuild_list(self, empty_text: str = "Ничего не найдено"):
        _stop_runners(self._runners)
        self._track_title_labels.clear()
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        self._scroll.verticalScrollBar().setValue(0)

        if not self._results:
            self._count_label.setText(empty_text)
            return

        artists   = [r for r in self._results if r.type == "artist"][:4]
        albums    = [r for r in self._results if r.type == "album"][:6]
        playlists = [r for r in self._results if r.type == "playlist"][:10]
        tracks    = [r for r in self._results if r.type == "track"][:10]
        youtube   = [r for r in self._results if r.type == "youtube"][:15]
        total = len(artists) + len(albums) + len(playlists) + len(tracks) + len(youtube)
        self._count_label.setText(f"Найдено: {total}")

        insert_pos = 0

        def _insert(w):
            nonlocal insert_pos
            self._results_layout.insertWidget(insert_pos, w)
            insert_pos += 1

        if artists:
            _insert(self._make_grid_section("Исполнители", [self._make_artist_row(r) for r in artists]))

        if albums:
            _insert(self._make_grid_section("Альбомы", [self._make_album_row(r) for r in albums]))

        if playlists:
            _insert(self._make_grid_section("Плейлисты", [self._make_search_playlist_row(r) for r in playlists]))

        if tracks:
            _insert(self._make_grid_section("Треки", [self._make_track_row(r) for r in tracks]))

        if youtube:
            _insert(self._make_grid_section("YouTube", [self._make_youtube_row(r) for r in youtube]))

    def _make_grid_section(self, title: str, row_widgets: list) -> QWidget:
        """Section header + rows laid out two-per-row (row-major), so N
        results end up as N/2 on the left column and N/2 on the right."""
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._make_section_header(title))

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for i, w in enumerate(row_widgets):
            grid.addWidget(w, i // 2, i % 2)
        v.addLayout(grid)
        return container

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
        full = resolve_media_url(cover_url)
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
        title_lbl = _AccentGradientLabel(clean_title(result.track_title or ""))
        title_lbl.set_accent_active(False)  # plain TEXT_PRIMARY until refresh_playing() says otherwise
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

    def _make_search_playlist_row(self, result: SearchResult) -> QWidget:
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
        pm = result.playlist_cover_pixmap
        cover.setPixmap(make_rounded_pixmap(pm, 48, 6) if pm and not pm.isNull() else _make_placeholder_cover(48, 6))
        lay.addWidget(cover)

        pl = result.playlist_obj or {}
        txt = QVBoxLayout()
        txt.setSpacing(2)
        txt.setContentsMargins(0, 0, 0, 0)
        title_lbl = QLabel(clean_title(pl.get("name") or "Без названия"))
        title_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        txt.addWidget(title_lbl)
        subtitle = "Ваш плейлист" if result.playlist_editable else f"@{result.playlist_owner_login or ''}"
        sub_lbl = QLabel(subtitle)
        sub_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        txt.addWidget(sub_lbl)
        lay.addLayout(txt, 1)

        def on_click(event, r=result):
            if event.button() == Qt.MouseButton.LeftButton:
                self.result_selected.emit(r)
        row.mousePressEvent = on_click
        return row

    def _make_youtube_row(self, result: SearchResult) -> QWidget:
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

        yt = result.youtube_obj or {}

        thumb = QLabel()
        thumb.setFixedSize(48, 48)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 6px;")
        self._load_cover_into(yt.get("thumbnail", ""), thumb, 48, 6)
        lay.addWidget(thumb)

        txt = QVBoxLayout()
        txt.setSpacing(2)
        txt.setContentsMargins(0, 0, 0, 0)
        title_lbl = QLabel(yt.get("title") or "")
        title_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        title_lbl.setWordWrap(False)
        txt.addWidget(title_lbl)
        duration_txt = format_duration(int(yt.get("duration") or 0) * 1000)
        sub_lbl = QLabel(f"{yt.get('uploader') or 'YouTube'} • {duration_txt}")
        sub_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        txt.addWidget(sub_lbl)
        lay.addLayout(txt, 1)

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
        if result.type == "playlist":
            return self._make_search_playlist_row(result)
        if result.type == "youtube":
            return self._make_youtube_row(result)
        return self._make_track_row(result)

    def refresh_playing(self, url: str, track: dict | None = None):
        """Highlight track rows that match the currently playing track (by URL,
        or — for albums shared across artists — by album_id + normalized title)."""
        accent = COLORS["PRIMARY"]
        keys = _track_like_keys(track or {}, url)
        for title_lbl, row_keys in self._track_title_labels:
            is_playing = bool(keys) and bool(row_keys & keys)
            title_lbl.set_accent_active(is_playing)
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
        lbl = _AccentGradientLabel(letter)
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
            full = resolve_media_url(cover_rel)
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
# Home page — the "main menu" opened via the Memify logo: a random spread of
# albums and artists pulled from the whole library, reshuffled on every visit.
# ──────────────────────────────────────────────────────────────────────────────

class _NoWheelScrollArea(QScrollArea):
    """A wheel event here is deliberately never accepted — Qt bubbles an
    ignored wheel event up to the parent widget on its own, so hovering one
    of these horizontal strips scrolls the *page* instead of doing nothing
    (or worse, nudging the strip itself by a few px if its content is ever
    marginally taller than the viewport)."""

    def wheelEvent(self, event):
        event.ignore()


class _CarouselStrip(QWidget):
    """A single-row horizontal strip with no scrollbar at all — just a pair
    of arrow buttons overlaid on each side, revealed on hover and hidden
    again both on mouse-leave and whenever there's nothing further to
    scroll to in that direction."""

    ARROW_SIZE = 32
    STEP = 380  # px per click — a little over one album card + its gap

    def __init__(self, height: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self._hovering = False
        self._scroll_anim: QPropertyAnimation | None = None

        self._scroll = _NoWheelScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        row_container = QWidget()
        self.row = QHBoxLayout(row_container)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(16)
        self._scroll.setWidget(row_container)

        self._left_btn = self._make_arrow_btn("‹", self._scroll_left)
        self._right_btn = self._make_arrow_btn("›", self._scroll_right)

        hbar = self._scroll.horizontalScrollBar()
        hbar.rangeChanged.connect(lambda *_a: self._update_arrows())
        hbar.valueChanged.connect(lambda *_a: self._update_arrows())
        self._update_arrows()

    def _make_arrow_btn(self, text: str, handler) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setFixedSize(self.ARROW_SIZE, self.ARROW_SIZE)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            f"QPushButton {{ background: {COLORS['SURFACE']}; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: {self.ARROW_SIZE // 2}px; color: {COLORS['TEXT_PRIMARY']}; font: 13pt 'Segoe UI'; }}"
            f"QPushButton:hover {{ background: {COLORS['SURFACE_HOVER']}; border-color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        btn.clicked.connect(handler)
        btn.hide()
        return btn

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scroll.setGeometry(0, 0, self.width(), self.height())
        y = (self.height() - self.ARROW_SIZE) // 2
        self._left_btn.move(6, y)
        self._right_btn.move(self.width() - self.ARROW_SIZE - 6, y)
        self._left_btn.raise_()
        self._right_btn.raise_()

    def _scroll_left(self):
        self._animate_scroll(-self.STEP)

    def _scroll_right(self):
        self._animate_scroll(self.STEP)

    def _animate_scroll(self, delta: int):
        bar = self._scroll.horizontalScrollBar()
        target = max(bar.minimum(), min(bar.maximum(), bar.value() + delta))
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
        self._scroll_anim = QPropertyAnimation(bar, b"value", self)
        self._scroll_anim.setDuration(220)
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.start()

    def _update_arrows(self):
        bar = self._scroll.horizontalScrollBar()
        can_left = bar.value() > bar.minimum()
        can_right = bar.value() < bar.maximum()
        self._left_btn.setVisible(self._hovering and can_left)
        self._right_btn.setVisible(self._hovering and can_right)

    def enterEvent(self, event):
        self._hovering = True
        self._update_arrows()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self._left_btn.hide()
        self._right_btn.hide()
        super().leaveEvent(event)


class HomePage(QWidget):
    album_clicked = pyqtSignal(dict, dict)  # (album, artist)
    artist_selected = pyqtSignal(dict)

    ALBUM_COUNT = 12
    ARTIST_COUNT = 10
    _ALBUM_CARD_W = 170
    _ARTIST_TILE_W = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runners: list = []
        self._library: list[dict] = []
        self._continue_cards: list[AlbumWidget] = []
        self._album_cards: list[AlbumWidget] = []
        self._artist_tiles: list[QWidget] = []
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(get_scrollbar_style())
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 16, 20, 24)
        layout.setSpacing(20)

        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        title = QLabel("Главное меню")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        header_row.addWidget(title)
        header_row.addStretch(1)

        refresh_btn = QPushButton("⟳  Обновить")
        refresh_btn.setFixedHeight(32)
        refresh_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setToolTip("Показать другую подборку")
        refresh_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; border-radius: 16px; "
            f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 14px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        refresh_btn.clicked.connect(self.reshuffle)
        header_row.addWidget(refresh_btn)
        layout.addLayout(header_row)

        self._continue_label = QLabel("Продолжить слушать")
        self._continue_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._continue_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        self._continue_label.setVisible(False)
        layout.addWidget(self._continue_label)

        self._continue_scroll, self._continue_row = self._make_row_strip(240)
        self._continue_scroll.setVisible(False)
        layout.addWidget(self._continue_scroll)

        albums_label = QLabel("Случайные альбомы")
        albums_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        albums_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        layout.addWidget(albums_label)

        self._album_scroll, self._album_row = self._make_row_strip(240)
        layout.addWidget(self._album_scroll)

        artists_label = QLabel("Случайные исполнители")
        artists_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        artists_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        layout.addWidget(artists_label)

        self._artist_scroll, self._artist_row = self._make_row_strip(160)
        layout.addWidget(self._artist_scroll)

        self._build_changelog_section(layout)

        layout.addStretch(1)
        self._scroll.setWidget(container)
        outer.addWidget(self._scroll)

    PAST_CHANGELOG_COUNT = 4

    def _build_changelog_section(self, layout: QVBoxLayout):
        """Changelog for the version this build was downloaded with, plus
        the previous few updates below it — always the very last thing on
        the page."""
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {COLORS['BORDER']};")
        layout.addWidget(divider)

        self._add_changelog_entry(layout, APP_VERSION, current=True)

        versions = sorted(
            CHANGELOG.keys(),
            key=lambda v: tuple(int(p) for p in v.split(".")),
            reverse=True,
        )
        start = versions.index(APP_VERSION) + 1 if APP_VERSION in versions else 0
        for version in versions[start:start + self.PAST_CHANGELOG_COUNT]:
            self._add_changelog_entry(layout, version, current=False)

    def _add_changelog_entry(self, layout: QVBoxLayout, version: str, current: bool):
        block = QWidget()
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(0, 0, 0, 0)
        block_layout.setSpacing(6)

        title = QLabel(f"Что нового в версии {version}" if current else f"Версия {version}")
        title.setFont(QFont("Segoe UI", 14 if current else 11, QFont.Weight.Bold))
        title.setStyleSheet(
            f"color: {COLORS['TEXT_PRIMARY'] if current else COLORS['TEXT_SECONDARY']};"
        )
        block_layout.addWidget(title)

        entries = CHANGELOG.get(version) or (
            ["Список изменений для этой версии пока не добавлен."] if current else []
        )
        for entry in entries:
            item_lbl = QLabel(f"•  {entry}")
            item_lbl.setWordWrap(True)
            item_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI';")
            block_layout.addWidget(item_lbl)

        layout.addWidget(block)

    def _make_row_strip(self, height: int) -> tuple:
        """A single-row strip (cards never wrap): no scrollbar, no
        wheel-scroll, just hover-revealed arrow buttons — see _CarouselStrip."""
        strip = _CarouselStrip(height, self)
        return strip, strip.row

    # ── Data ─────────────────────────────────────────────────────────────────

    def load_library(self, library: list[dict], history: list | None = None):
        """Full reload — call whenever the page is (re)opened so the pool of
        candidates reflects the current library before rerolling. `history`
        is the account's recent-albums-played list (most recent first); the
        "Продолжить слушать" row is rebuilt after reshuffle() so its cover
        loads don't get cancelled by _fill_albums' _stop_runners()."""
        self._library = library or []
        self.reshuffle()
        self._fill_continue_listening(history or [])

    def reshuffle(self):
        albums_pool = []
        for artist in self._library:
            if not isinstance(artist, dict):
                continue
            for album in artist.get("albums", []) or []:
                if isinstance(album, dict):
                    albums_pool.append((album, artist))

        picked_albums = (
            random.sample(albums_pool, min(self.ALBUM_COUNT, len(albums_pool)))
            if albums_pool else []
        )
        artist_pool = [a for a in self._library if isinstance(a, dict)]
        picked_artists = (
            random.sample(artist_pool, min(self.ARTIST_COUNT, len(artist_pool)))
            if artist_pool else []
        )

        self._fill_albums(picked_albums)
        self._fill_artists(picked_artists)

    # ── Layout helpers ───────────────────────────────────────────────────────

    def _clear_row(self, row: QHBoxLayout, tracked: list):
        for w in tracked:
            row.removeWidget(w)
            w.deleteLater()
        tracked.clear()
        while row.count():
            item = row.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    # ── Albums section ───────────────────────────────────────────────────────

    def _fill_albums(self, picked: list):
        _stop_runners(self._runners)
        self._clear_row(self._album_row, self._album_cards)

        if not picked:
            placeholder = QLabel("Библиотека пуста")
            placeholder.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
            self._album_row.addWidget(placeholder, 0, Qt.AlignmentFlag.AlignTop)
            self._album_row.addStretch(1)
            return

        cover_urls = []
        for album, artist in picked:
            cover_url = resolve_media_url(album["cover"]) if album.get("cover") else ""
            card = AlbumWidget(album, cover_url, widget_size=self._ALBUM_CARD_W, cover_size=150)
            card.clicked.connect(partial(self._on_album_clicked, artist=artist))
            self._album_row.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)
            self._album_cards.append(card)
            cover_urls.append(cover_url)
        self._album_row.addStretch(1)

        self._load_covers_into(cover_urls, self._album_cards)

    def _on_album_clicked(self, album: dict, artist: dict):
        self.album_clicked.emit(album, artist)

    def _load_covers_into(self, urls: list, cards: list):
        valid = [(i, u) for i, u in enumerate(urls) if u]
        if not valid:
            return
        to_load = []
        for i, url in valid:
            key = cache_key(url, 150, 14)
            cached = cover_cache.get(key)
            if cached and not cached.isNull() and i < len(cards):
                cards[i].set_cover(cached)
            else:
                to_load.append((i, url))
        if not to_load:
            return
        indices = [i for i, _ in to_load]
        load_urls = [u for _, u in to_load]

        def on_loaded(url, img, size, radius):
            if img is None:
                return
            try:
                pm = QPixmap.fromImage(img)
                if pm.isNull():
                    return
                cover_cache.set(cache_key(url, size, radius), pm)
                card_idx = indices[load_urls.index(url)]
                if card_idx < len(cards):
                    cards[card_idx].set_cover(pm)
            except Exception:
                pass

        _start_image_loader(load_urls, 150, 14, on_loaded, self._runners)

    # ── Continue listening section ──────────────────────────────────────────

    def _fill_continue_listening(self, history: list):
        self._clear_row(self._continue_row, self._continue_cards)

        pairs = self._resolve_history_albums(history)
        has_history = bool(pairs)
        self._continue_label.setVisible(has_history)
        self._continue_scroll.setVisible(has_history)
        if not has_history:
            return

        cover_urls = []
        for album, artist in pairs:
            cover_url = resolve_media_url(album["cover"]) if album.get("cover") else ""
            card = AlbumWidget(album, cover_url, widget_size=self._ALBUM_CARD_W, cover_size=150)
            card.clicked.connect(partial(self._on_album_clicked, artist=artist))
            self._continue_row.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)
            self._continue_cards.append(card)
            cover_urls.append(cover_url)
        self._continue_row.addStretch(1)

        self._load_covers_into(cover_urls, self._continue_cards)

    def _resolve_history_albums(self, history: list) -> list:
        """Match stored (artist_name, album_title/album_id) history entries
        against the current library — server-side album removals/renames
        just drop that entry instead of showing something broken."""
        resolved = []
        seen_keys = set()
        for entry in history:
            if not isinstance(entry, dict):
                continue
            pair = self._find_album(
                entry.get("artist_name", ""),
                entry.get("album_title", ""),
                str(entry.get("album_id") or "").strip(),
            )
            if not pair:
                continue
            album, artist = pair
            key = (artist.get("artist", ""), album.get("title", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            resolved.append(pair)
        return resolved

    def _find_album(self, artist_name: str, album_title: str, album_id: str):
        target_artist = clean_artist_name(artist_name)
        target_title = clean_title(album_title)
        for artist in self._library:
            if not isinstance(artist, dict):
                continue
            if target_artist and clean_artist_name(artist.get("artist", "")) != target_artist:
                continue
            for album in artist.get("albums", []) or []:
                if not isinstance(album, dict):
                    continue
                if album_id and str(album.get("album_id") or "").strip() == album_id:
                    return album, artist
                if target_title and clean_title(album.get("title", "")) == target_title:
                    return album, artist
        return None

    # ── Artists section ──────────────────────────────────────────────────────

    def _fill_artists(self, picked: list):
        self._clear_row(self._artist_row, self._artist_tiles)

        if not picked:
            placeholder = QLabel("Нет исполнителей")
            placeholder.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
            self._artist_row.addWidget(placeholder, 0, Qt.AlignmentFlag.AlignTop)
            self._artist_row.addStretch(1)
            return

        for artist in picked:
            tile = self._make_artist_tile(artist)
            self._artist_row.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)
            self._artist_tiles.append(tile)
        self._artist_row.addStretch(1)

    def _make_artist_tile(self, artist: dict) -> QWidget:
        tile = QWidget()
        tile.setFixedWidth(self._ARTIST_TILE_W)
        tile.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(tile)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        avatar = QLabel()
        avatar.setFixedSize(90, 90)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"background: {COLORS['COVER_BG']}; border-radius: 45px;")
        lay.addWidget(avatar, 0, Qt.AlignmentFlag.AlignHCenter)

        name_lbl = QLabel(clean_artist_name(artist.get("artist", "")))
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 9pt 'Segoe UI';")
        lay.addWidget(name_lbl)

        cover_rel = artist.get("cover", "")
        if cover_rel:
            full = resolve_media_url(cover_rel)
            key = cache_key(full, 90, 45)
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
                _start_image_loader([full], 90, 45, _cb, self._runners)

        def on_click(event, a=artist):
            if event.button() == Qt.MouseButton.LeftButton:
                self.artist_selected.emit(a)
        tile.mousePressEvent = on_click
        return tile


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────

_LIKED_ROW_ROLE = Qt.ItemDataRole.UserRole + 1
_ARTIST_ROW_ROLE = Qt.ItemDataRole.UserRole + 2

# Custom data roles for the reorderable artist/album QListWidgetItems (see
# _SidebarItemDelegate) — offset well clear of the two roles above.
_SR_KEY = Qt.ItemDataRole.UserRole              # "artist::Name" / "album::Artist||Title"
_SR_SUBTITLE = Qt.ItemDataRole.UserRole + 10
_SR_COVER = Qt.ItemDataRole.UserRole + 11
_SR_RADIUS = Qt.ItemDataRole.UserRole + 12
_SR_FALLBACK = Qt.ItemDataRole.UserRole + 13
_SR_CLICK_DATA = Qt.ItemDataRole.UserRole + 14
_SR_KIND = Qt.ItemDataRole.UserRole + 15
_SR_FALLBACK_BG = Qt.ItemDataRole.UserRole + 16  # fallback cover background color override


class _SidebarItemDelegate(QStyledItemDelegate):
    """Paints artist/album rows directly instead of using setItemWidget().

    A widget embedded via setItemWidget() sits on top of the viewport and
    swallows mouse press/move events for its whole area before the
    QListWidget itself ever sees them — which is exactly why the built-in
    InternalMove drag-to-reorder silently did nothing when rows were real
    child widgets (dragging never even started). Owner-drawing keeps the
    same cover+name+subtitle look while letting the view's native
    drag-and-drop receive the events it needs to actually work.
    """

    ROW_HEIGHT = 54
    COVER_SIZE = 40

    def sizeHint(self, option, index):
        width = option.rect.width() if option.rect.width() > 0 else 240
        return QSize(width, self.ROW_HEIGHT)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = COLORS
        rect = option.rect
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if hovered or selected:
            bg_path = QPainterPath()
            bg_path.addRoundedRect(QRectF(rect.adjusted(4, 1, -4, -1)), 8, 8)
            painter.fillPath(bg_path, QColor(c["SURFACE_LIGHT"]))

        radius = index.data(_SR_RADIUS) or 6
        cover_size = self.COVER_SIZE
        cover_rect = QRectF(
            rect.x() + 10, rect.y() + (rect.height() - cover_size) / 2, cover_size, cover_size
        )
        cover_path = QPainterPath()
        cover_path.addRoundedRect(cover_rect, radius, radius)
        pm = index.data(_SR_COVER)
        if isinstance(pm, QPixmap) and not pm.isNull():
            painter.save()
            painter.setClipPath(cover_path)
            painter.drawPixmap(cover_rect.toRect(), pm)
            painter.restore()
        else:
            fallback_bg = index.data(_SR_FALLBACK_BG) or c["SURFACE_LIGHT"]
            painter.fillPath(cover_path, QColor(fallback_bg))
            fallback = index.data(_SR_FALLBACK) or ""
            if fallback:
                painter.setPen(QColor(c["TEXT_PRIMARY"]))
                painter.setFont(QFont("Segoe UI", 12))
                painter.drawText(cover_rect, Qt.AlignmentFlag.AlignCenter, fallback)

        text_x = cover_rect.right() + 10
        text_w = max(10.0, rect.right() - text_x - 8)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        subtitle = index.data(_SR_SUBTITLE) or ""

        name_font = QFont("Segoe UI", 10)
        painter.setPen(QColor(c["TEXT_PRIMARY"] if (hovered or selected) else c["TEXT_SECONDARY"]))
        painter.setFont(name_font)
        fm = QFontMetrics(name_font)
        elided_name = fm.elidedText(name, Qt.TextElideMode.ElideRight, int(text_w))
        name_rect = (
            QRectF(text_x, rect.y() + 8, text_w, 18) if subtitle
            else QRectF(text_x, rect.y(), text_w, rect.height())
        )
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)

        if subtitle:
            sub_font = QFont("Segoe UI", 8)
            sub_fm = QFontMetrics(sub_font)
            painter.setFont(sub_font)
            painter.setPen(QColor(c["TEXT_SECONDARY"]))
            elided_sub = sub_fm.elidedText(subtitle, Qt.TextElideMode.ElideRight, int(text_w))
            sub_rect = QRectF(text_x, rect.y() + rect.height() - 24, text_w, 18)
            painter.drawText(sub_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_sub)

        painter.restore()


class _SidebarItem(QWidget):
    """Single item in the sidebar with a 40x40 cover + label."""
    clicked = pyqtSignal(object)

    COVER_SIZE = 40

    def __init__(self, text: str, data, radius: int = 6, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarItem")
        self._data = data
        self._radius = radius
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build_ui(text, subtitle)
        self._set_style(hover=False, selected=False)

    def _build_ui(self, text: str, subtitle: str = ""):
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

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)

        self._name_label = QLabel(text)
        self._name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._name_label.setWordWrap(False)
        text_col.addWidget(self._name_label)

        self._subtitle_label = None
        if subtitle:
            self._subtitle_label = QLabel(subtitle)
            self._subtitle_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self._subtitle_label.setWordWrap(False)
            text_col.addWidget(self._subtitle_label)

        lay.addLayout(text_col, 1)

    def _set_style(self, hover: bool, selected: bool):
        c = COLORS
        active = hover or selected
        bg = c["SURFACE_LIGHT"] if active else "transparent"
        color = c["TEXT_PRIMARY"] if active else c["TEXT_SECONDARY"]
        self.setStyleSheet(f"QWidget#sidebarItem {{ background: {bg}; border-radius: 8px; }}")
        self._name_label.setStyleSheet(f"color: {color}; font: 10pt 'Segoe UI';")
        if self._subtitle_label is not None:
            self._subtitle_label.setStyleSheet(f"color: {c['TEXT_SECONDARY']}; font: 8pt 'Segoe UI';")

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


class _AvatarButton(QWidget):
    """Circular account avatar. Shows the uploaded picture, or — with none
    set — a solid accent-colored circle with the first letter of the
    account's name, so there's always something to click."""
    clicked = pyqtSignal()

    def __init__(self, size: int = 34, parent=None):
        super().__init__(parent)
        self._size = size
        self._has_avatar = False
        self._letter = "?"
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._label = QLabel(self)
        self._label.setGeometry(0, 0, size, size)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_fallback_style()

    def _apply_fallback_style(self):
        radius = self._size // 2
        font_size = max(10, int(self._size * 0.42))
        self._label.setPixmap(QPixmap())
        self._label.setText(self._letter)
        self._label.setStyleSheet(
            f"background: {COLORS['PRIMARY']}; color: white; border-radius: {radius}px; "
            f"font: 600 {font_size}px 'Segoe UI';"
        )

    def set_fallback_letter(self, text: str):
        self._letter = (text or "?").strip()[:1].upper() or "?"
        if not self._has_avatar:
            self._label.setText(self._letter)

    def set_avatar_pixmap(self, pixmap: QPixmap | None):
        if pixmap is None or pixmap.isNull():
            self._has_avatar = False
            self._apply_fallback_style()
            return
        self._has_avatar = True
        pm = make_rounded_pixmap(pixmap, self._size, self._size // 2)
        self._label.setStyleSheet("background: transparent;")
        self._label.setText("")
        self._label.setPixmap(pm)

    def apply_accent(self):
        if not self._has_avatar:
            self._apply_fallback_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _ChevronButton(QPushButton):
    """Small hand-drawn disclosure triangle for collapsing a sidebar
    section — points right when collapsed, down when expanded. Checkable
    QPushButton so the inherited `toggled(bool)` signal already carries
    the new expanded state, no custom signal needed."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setCheckable(True)
        self.setChecked(True)  # expanded by default
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent; border: none;")

    def setExpanded(self, expanded: bool):
        self.setChecked(bool(expanded))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rf = QRectF(self.rect())
        cx, cy = rf.center().x(), rf.center().y()
        s = 4.5
        path = QPainterPath()
        if self.isChecked():
            # Expanded — pointing down.
            path.moveTo(cx - s, cy - s * 0.55)
            path.lineTo(cx + s, cy - s * 0.55)
            path.lineTo(cx, cy + s * 0.65)
        else:
            # Collapsed — pointing right.
            path.moveTo(cx - s * 0.55, cy - s)
            path.lineTo(cx - s * 0.55, cy + s)
            path.lineTo(cx + s * 0.65, cy)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["TEXT_SECONDARY"]))
        p.drawPath(path)
        p.end()


class _NoWheelListWidget(QListWidget):
    """Same reasoning as _NoWheelScrollArea: this list is always sized to
    its full content height (see _resize_list_to_content) precisely so it
    never needs to scroll internally — the sidebar's own outer QScrollArea
    is meant to be the only thing that scrolls. But QListWidget still
    handles wheel events on its own regardless of scrollbar visibility, so
    without this override, scrolling over it nudges its *internal* viewport
    instead of the page — invisibly, since the scrollbar is hidden — which
    clips rows like "Понравившиеся" out of view without any visual cue that
    anything scrolled at all. Ignoring the event here lets Qt bubble it up
    to the outer QScrollArea instead."""

    def wheelEvent(self, event):
        event.ignore()


class Sidebar(QWidget):
    artist_selected = pyqtSignal(dict)
    album_selected = pyqtSignal(dict, dict)   # (album, artist)
    liked_tracks_selected = pyqtSignal()
    playlist_selected = pyqtSignal(str)  # own playlist id
    playlist_subscription_selected = pyqtSignal(dict)  # {owner_login, playlist_id, name, cover_data}
    # Emitted after the user drags an artist/album row to a new position —
    # carries the section ("server"/"local") plus the full new order as
    # stable "artist::Name" / "album::Artist||Title" keys, ready to hand
    # straight to follow_order (server) or local_library_order (local).
    order_changed = pyqtSignal(str, list)
    # Emitted when the user clicks a section's chevron — carries the
    # section ("server"/"local") and the new *collapsed* state, for
    # persisting to settings.
    section_collapsed_changed = pyqtSignal(str, bool)
    # "Открыть папку music" clicked from the empty-state hint (see
    # _local_hint) — same destination as the identical button in Settings.
    open_local_folder_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(260)
        self.setStyleSheet(
            f"QWidget#sidebar {{ background-color: {COLORS['SURFACE']}; "
            f"border-right: 1px solid {COLORS['SURFACE_LIGHT']}; }}"
        )
        # Own account state — cached so _rebuild_server_list() can redraw
        # the combined server list on demand.
        self._server_liked: bool = False
        self._server_entries: list = []
        self._runners: list = []
        # True while set_server_collapsed()/set_local_collapsed() is
        # applying a saved setting programmatically — suppresses
        # section_collapsed_changed so loading settings doesn't
        # immediately re-save/re-sync the same value right back out.
        self._applying_saved_collapse = False
        # True while load_account_content()/load_local_content() is
        # rebuilding a list — the rebuild itself re-adds every row (a
        # "move" as far as the model is concerned), which would otherwise
        # be misread as a user drag and echo a bogus order_changed.
        self._rebuilding = False
        self._build_ui()

    def _build_section(self, title: str, section: str):
        """Builds one collapsible section (header + chevron + list) and
        returns (container_widget, list_widget, chevron_button)."""
        container = QWidget()
        section_layout = QVBoxLayout(container)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(6, 0, 0, 0)
        header_lbl = QLabel(title)
        header_lbl.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        header_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        header_row.addWidget(header_lbl)
        header_row.addStretch(1)
        chevron = _ChevronButton()
        header_row.addWidget(chevron)
        section_layout.addLayout(header_row)


        # Reorderable artist/album list. Rows are owner-drawn (see
        # _SidebarItemDelegate) rather than embedded widgets — a widget set
        # via setItemWidget() would intercept every mouse press itself,
        # leaving the view's built-in InternalMove drag-and-drop with
        # nothing to react to.
        list_widget = _NoWheelListWidget()
        list_widget.setObjectName("sidebarList")
        list_widget.setItemDelegate(_SidebarItemDelegate(list_widget))
        list_widget.setFrameShape(QFrame.Shape.NoFrame)
        list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # No internal scrolling — the list is always sized to its full
        # content height (see _resize_list_to_content) so it never clips;
        # the whole sidebar scrolls as one unit via the QScrollArea in
        # _build_ui instead.
        list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        list_widget.setMouseTracking(True)   # needed for State_MouseOver per-row hover
        list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        # Explicit alongside DragDropMode (which should already imply these)
        # — belt and suspenders, since the previous setItemWidget-based
        # attempt looked correctly configured too and still didn't drag.
        list_widget.setDragEnabled(True)
        list_widget.setAcceptDrops(True)
        list_widget.setDropIndicatorShown(True)
        list_widget.setSpacing(2)
        list_widget.setStyleSheet(
            get_scrollbar_style() +
            "QListWidget#sidebarList { background: transparent; border: none; }"
        )
        list_widget.itemClicked.connect(self._on_item_clicked)
        section_layout.addWidget(list_widget)

        def _on_chevron_toggled(expanded, lw=list_widget):
            lw.setVisible(expanded)
            if not self._applying_saved_collapse:
                self.section_collapsed_changed.emit(section, not expanded)
        chevron.toggled.connect(_on_chevron_toggled)

        return container, list_widget, chevron

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # The whole sidebar (username row + both library sections, however
        # tall they get) scrolls together as one unit — each section's own
        # list is sized to its full content height (_resize_list_to_content)
        # rather than clipping internally, so there's exactly one scrollbar
        # for the entire sidebar instead of one per list.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(get_scrollbar_style())
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        # Username row
        self._user_row = QHBoxLayout()
        self._user_row.setContentsMargins(6, 0, 0, 0)
        self._user_label = QLabel("")
        self._user_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._user_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        self._user_row.addWidget(self._user_label)
        self._user_row.addStretch(1)
        layout.addLayout(self._user_row)

        self._server_container, self._list_server, self._chevron_server = self._build_section(
            "Библиотека", "server"
        )
        layout.addWidget(self._server_container)
        self._list_server.model().rowsMoved.connect(lambda *_: self._on_rows_moved("server"))

        self._local_container, self._list_local, self._chevron_local = self._build_section(
            "Локальная библиотека", "local"
        )
        layout.addWidget(self._local_container)
        self._list_local.model().rowsMoved.connect(lambda *_: self._on_rows_moved("local"))
        self._local_container.setVisible(False)  # hidden until enabled in settings

        # Empty-state hint — shown instead of the (then-empty) list when the
        # music/ folder has no Artist/Album subfolders yet, so a freshly
        # created folder doesn't just look broken/blank in the sidebar.
        self._local_hint = QFrame()
        self._local_hint.setObjectName("localLibraryHint")
        self._local_hint.setStyleSheet(
            f"QFrame#localLibraryHint {{ background: {COLORS['SURFACE_LIGHT']}; "
            f"border: 1px dashed {COLORS['BORDER']}; border-radius: 8px; }}"
        )
        hint_layout = QVBoxLayout(self._local_hint)
        hint_layout.setContentsMargins(10, 10, 10, 10)
        hint_layout.setSpacing(6)

        hint_title = QLabel("Как добавить музыку")
        hint_title.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: bold 9.5pt 'Segoe UI';")
        hint_layout.addWidget(hint_title)

        hint_text = QLabel(
            "Разложите треки по папкам:\n"
            "music / Исполнитель / Альбом / трек.mp3\n\n"
            "Например:\n"
            "music / Imagine Dragons / Evolve / 01 - Believer.mp3\n\n"
            "Каждый исполнитель — отдельная папка, внутри неё — папка "
            "каждого альбома, а треки (mp3, flac, m4a, ogg, wav, opus) — "
            "прямо в папке альбома.\n\n"
            "Обложка — необязательно: положите файл cover.png (или jpg/png/webp) "
            "рядом, в папку исполнителя и/или альбома."
        )
        hint_text.setWordWrap(True)
        hint_text.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        hint_layout.addWidget(hint_text)

        hint_open_btn = QPushButton("Открыть папку music")
        hint_open_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hint_open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hint_open_btn.setFixedHeight(28)
        hint_open_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI'; padding: 0 10px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        hint_open_btn.clicked.connect(self.open_local_folder_requested.emit)
        hint_layout.addWidget(hint_open_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._local_container.layout().addWidget(self._local_hint)
        self._local_hint.setVisible(False)  # toggled in load_local_content()

        layout.addStretch(1)

    def set_username(self, login: str):
        self._user_label.setText(login or "")
        self._user_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")

    def set_offline_mode(self):
        """No server reachable at startup — shown instead of a username;
        see MusicApp's offline path (no in-app way back to online without
        restarting, so this is a one-shot, not a toggle)."""
        self._user_label.setText("ОФФЛАЙН")
        self._user_label.setStyleSheet("color: #ff7a7a;")

    def set_local_section_visible(self, visible: bool):
        self._local_container.setVisible(bool(visible))

    def set_server_section_visible(self, visible: bool):
        self._server_container.setVisible(bool(visible))

    def set_server_collapsed(self, collapsed: bool):
        # Applying a saved setting, not a user click — don't echo it back
        # out via section_collapsed_changed (would just re-save the same
        # value, and schedule a pointless settings sync, on every launch).
        self._applying_saved_collapse = True
        try:
            self._chevron_server.setExpanded(not collapsed)
        finally:
            self._applying_saved_collapse = False

    def set_local_collapsed(self, collapsed: bool):
        self._applying_saved_collapse = True
        try:
            self._chevron_local.setExpanded(not collapsed)
        finally:
            self._applying_saved_collapse = False

    def load_account_content(self, liked: bool, entries: list | None = None):
        """entries: ordered list of ("artist", artist_dict) /
        ("album", (album_dict, artist_dict)) / ("playlist", playlist_dict)
        tuples, already in the desired display order (caller resolves
        follow_order into this — playlists are freely draggable among
        artists/albums, same as either of those; only "liked" is pinned)."""
        self._server_liked = liked
        self._server_entries = entries or []
        self._rebuild_server_list()

    def _rebuild_server_list(self):
        """Rebuilds the "Библиотека" list from cached state: "Понравившиеся
        треки" always first (pinned — see _make_liked_item), then
        artists/albums/playlists in whatever order the caller supplied
        (their own relative drag order)."""
        _stop_runners(self._runners)
        self._rebuilding = True
        try:
            self._list_server.clear()
            if self._server_liked:
                self._list_server.addItem(self._make_liked_item())
            for kind, payload in self._server_entries:
                if kind == "artist":
                    list_item = self._make_artist_row(payload)
                elif kind == "album":
                    album, artist = payload
                    list_item = self._make_album_row(album, artist)
                elif kind == "playlist":
                    list_item = self._make_playlist_row(payload)
                elif kind == "playlist_sub":
                    list_item = self._make_playlist_sub_row(payload)
                else:
                    continue
                self._list_server.addItem(list_item)
        finally:
            self._rebuilding = False
        self._resize_list_to_content(self._list_server)

    def load_local_content(self, entries: list | None = None):
        """Same shape as load_account_content's entries, for the local
        library section — no "liked"/playlists concept there."""
        self._fill_list(self._list_local, entries)
        self._local_hint.setVisible(not entries)

    def _fill_list(self, list_widget: QListWidget, entries: list | None):
        self._rebuilding = True
        try:
            list_widget.clear()
            for kind, payload in (entries or []):
                if kind == "artist":
                    list_item = self._make_artist_row(payload)
                elif kind == "album":
                    album, artist = payload
                    list_item = self._make_album_row(album, artist)
                else:
                    continue
                list_widget.addItem(list_item)
        finally:
            self._rebuilding = False
        self._resize_list_to_content(list_widget)

    @staticmethod
    def _resize_list_to_content(list_widget: QListWidget):
        """No internal scrollbar (see _build_section) — the list's height
        must track its own row count exactly, so the whole sidebar's single
        outer scroll area sees the real total content height.

        Each item carries `spacing()` on every side of its own layout box —
        so besides the first item's top and the last item's bottom (one
        `spacing` each), every *pair* of consecutive items contributes two
        (its bottom + the next one's top). The previous formula counted a
        single `spacing` per gap instead of double, undercounting the total
        by one `spacing` per item — invisible for a short list, but on a
        longer one (e.g. once playlists made "Библиотека" grow) it added up
        to clipping the last row's subtitle against the section below it.
        Verified empirically against sizeHintForRow via visualItemRect for
        1/2/3/11-item lists — exact match, no leftover slack either way."""
        count = list_widget.count()
        if count == 0:
            list_widget.setFixedHeight(0)
            return
        row_h = list_widget.sizeHintForRow(0)
        spacing = list_widget.spacing()
        total = row_h * count + spacing * (2 * count - 1) + list_widget.frameWidth() * 2
        list_widget.setFixedHeight(max(total, 0))

    def _make_liked_item(self) -> QListWidgetItem:
        item = QListWidgetItem("Понравившиеся треки")
        item.setData(_SR_KEY, "_liked_tracks_")
        item.setData(_SR_KIND, "liked")
        item.setData(_SR_RADIUS, 6)
        item.setData(_SR_FALLBACK, "♥")
        item.setData(_SR_FALLBACK_BG, COLORS["PRIMARY_GRADIENT"])
        # Pinned — the user can't pick this row up and drag it elsewhere.
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        icon_path = os.path.join(ICONS_DIR, "liked_icon.png")
        if os.path.exists(icon_path):
            pm = QPixmap(icon_path)
            if not pm.isNull():
                item.setData(_SR_COVER, pm)
        return item

    def _make_playlist_row(self, playlist: dict) -> QListWidgetItem:
        item = QListWidgetItem(playlist.get("name") or "Без названия")
        item.setData(_SR_KEY, f"playlist::{playlist.get('id', '')}")
        item.setData(_SR_KIND, "playlist")
        # The full dict (not just the id) — needed to rebuild this same row
        # after a drag (see _on_rows_moved); _on_item_clicked pulls the id
        # back out of it before emitting playlist_selected.
        item.setData(_SR_CLICK_DATA, playlist)
        item.setData(_SR_SUBTITLE, "Плейлист")
        item.setData(_SR_RADIUS, 6)
        item.setData(_SR_FALLBACK, "♪")
        # Draggable, same as artists/albums — only "liked" is pinned.
        cover_pm = _decode_base64_pixmap(playlist.get("cover_data") or "")
        if cover_pm is not None:
            item.setData(_SR_COVER, cover_pm)
        return item

    def _make_playlist_sub_row(self, sub: dict) -> QListWidgetItem:
        """A "+"-ed playlist belonging to another account — same row style
        as an owned playlist, draggable the same way."""
        item = QListWidgetItem(sub.get("name") or "Без названия")
        item.setData(_SR_KEY, f"playlistsub::{sub.get('owner_login', '')}::{sub.get('playlist_id', '')}")
        item.setData(_SR_KIND, "playlist_sub")
        item.setData(_SR_CLICK_DATA, sub)
        item.setData(_SR_SUBTITLE, "Плейлист")
        item.setData(_SR_RADIUS, 6)
        item.setData(_SR_FALLBACK, "♪")
        cover_pm = _decode_base64_pixmap(sub.get("cover_data") or "")
        if cover_pm is not None:
            item.setData(_SR_COVER, cover_pm)
        return item

    def _make_artist_row(self, artist: dict) -> QListWidgetItem:
        name = clean_artist_name(artist.get("artist", "")) or "Неизвестно"
        list_item = QListWidgetItem(name)
        list_item.setData(_SR_KEY, f"artist::{(artist.get('artist') or '').strip()}")
        list_item.setData(_SR_KIND, "artist")
        list_item.setData(_SR_CLICK_DATA, artist)
        list_item.setData(_SR_SUBTITLE, "Исполнитель")
        list_item.setData(_SR_RADIUS, 20)
        list_item.setData(_SR_FALLBACK, "♪")

        cover_rel = artist.get("cover", "")
        if cover_rel:
            url = resolve_media_url(cover_rel)
            self._load_cover(list_item, url, 40, 20)
        return list_item

    def _make_album_row(self, album: dict, artist: dict) -> QListWidgetItem:
        name = clean_title(album.get("title", "")) or "Неизвестно"
        list_item = QListWidgetItem(name)
        artist_name = (artist.get("artist") or "").strip()
        album_title = (album.get("title") or "").strip()
        list_item.setData(_SR_KEY, f"album::{artist_name}||{album_title}")
        list_item.setData(_SR_KIND, "album")
        list_item.setData(_SR_CLICK_DATA, (album, artist))
        list_item.setData(_SR_SUBTITLE, "Альбом")
        list_item.setData(_SR_RADIUS, 6)
        list_item.setData(_SR_FALLBACK, "♪")

        cover_rel = album.get("cover", "")
        if cover_rel:
            url = resolve_media_url(cover_rel)
            self._load_cover(list_item, url, 40, 6)
        return list_item

    def _load_cover(self, list_item: QListWidgetItem, url: str, size: int, radius: int):
        key = cache_key(url, size, radius)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            list_item.setData(_SR_COVER, cached)
            return

        def on_loaded(loaded_url, img, s, r):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, s, r), pm)
                    # list_item may have been deleted by a load_account_content()/
                    # load_local_content() rebuild that happened while this was
                    # in flight. Repainting both lists is cheap and avoids
                    # having to track which one this item belongs to.
                    list_item.setData(_SR_COVER, pm)
                    self._list_server.viewport().update()
                    self._list_local.viewport().update()
            except RuntimeError:
                pass

        _start_image_loader([url], size, radius, on_loaded, self._runners)

    def select_artist(self, artist_name: str):
        for list_widget in (self._list_server, self._list_local):
            for row in range(list_widget.count()):
                list_item = list_widget.item(row)
                is_match = (
                    list_item.data(_SR_KIND) == "artist"
                    and (list_item.data(_SR_CLICK_DATA) or {}).get("artist") == artist_name
                )
                list_item.setSelected(is_match)

    def _on_item_clicked(self, list_item: QListWidgetItem):
        kind = list_item.data(_SR_KIND)
        data = list_item.data(_SR_CLICK_DATA)
        if kind == "artist":
            self.artist_selected.emit(data)
        elif kind == "album":
            album, artist = data
            self.album_selected.emit(album, artist)
        elif kind == "liked":
            self.liked_tracks_selected.emit()
        elif kind == "playlist":
            self.playlist_selected.emit((data or {}).get("id", ""))
        elif kind == "playlist_sub":
            self.playlist_subscription_selected.emit(data)

    def _on_rows_moved(self, section: str, *_args):
        if self._rebuilding:
            return
        list_widget = self._list_server if section == "server" else self._list_local
        keys = []
        for row in range(list_widget.count()):
            item = list_widget.item(row)
            if item.data(_SR_KIND) == "liked":
                continue
            key = item.data(_SR_KEY)
            if key:
                keys.append(key)
        if section == "server":
            # Re-derive self._server_entries from what's now actually on
            # screen post-drag (each row already carries its resolved
            # artist/album/playlist payload via _SR_CLICK_DATA — no need to
            # look anything back up), then do a full, deterministic rebuild
            # next tick. That's more robust than patching the existing
            # QListWidgetItems in place: a drag that grazed "liked" used to
            # occasionally leave it (or another row) missing, because that
            # patch-up ran while Qt's own drag-and-drop bookkeeping for this
            # same drop was still finishing up on the *existing* items —
            # rebuilding from scratch with fresh items sidesteps whatever
            # state Qt's internals left those in.
            new_entries = []
            for row in range(list_widget.count()):
                item = list_widget.item(row)
                kind = item.data(_SR_KIND)
                if kind in ("artist", "album", "playlist", "playlist_sub"):
                    new_entries.append((kind, item.data(_SR_CLICK_DATA)))
            self._server_entries = new_entries
            QTimer.singleShot(0, self._rebuild_server_list)
        self.order_changed.emit(section, keys)


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
        # Mid-flip the track is a plain off->on color blend (the animation is
        # short enough that a gradient interpolation wouldn't read anyway);
        # once fully on, switch to the real accent brush so a gradient
        # accent actually shows on a resting toggle.
        if t >= 0.999:
            p.setBrush(styles_module.accent_brush(rect.left(), 0, rect.right(), 0))
        else:
            p.setBrush(QBrush(track))
        p.drawRoundedRect(rect, radius, radius)

        knob_d = rect.height() - 4
        knob_x = rect.x() + 2 + (rect.width() - knob_d - 4) * t
        knob_y = rect.y() + 2
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(QRectF(knob_x, knob_y, knob_d, knob_d))
        p.end()


def _format_eq_freq(hz: float) -> str:
    """31.25 -> '31Hz', 1000.0 -> '1K', 16000.0 -> '16K' — matches how EQ
    bands are usually labeled (foobar2000/Winamp-style), compact enough to
    fit under a narrow vertical slider."""
    if hz >= 1000:
        val = hz / 1000
        return f"{val:g}K"
    return f"{hz:.0f}Hz"


class _EqCurveWidget(QWidget):
    """Graphic-EQ curve: draggable dots (one per band) joined by a smooth
    line with a gradient fill underneath — the "мостик" the reference photo
    showed, instead of a row of plain vertical sliders. Frequencies are
    fixed (libVLC's 10 bands aren't configurable), so dragging only ever
    moves a dot vertically; horizontal position is purely cosmetic/labeling.
    """

    bandChanged = pyqtSignal(int, float)

    _MARGIN_LEFT = 40
    _MARGIN_RIGHT = 14
    _MARGIN_TOP = 16
    _MARGIN_BOTTOM = 22
    _DOT_RADIUS = 5.5
    _HIT_RADIUS = 14  # px, generous click/touch target beyond the visible dot

    def __init__(self, band_freqs: list[float], min_db: float = -12.0, max_db: float = 12.0, parent=None):
        super().__init__(parent)
        self._freqs = list(band_freqs)
        self._min_db = min_db
        self._max_db = max_db
        self._values = [0.0] * len(self._freqs)
        self._drag_index: int | None = None
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def values(self) -> list[float]:
        return list(self._values)

    def set_values(self, values: list[float]):
        n = len(self._values)
        vals = (list(values) + [0.0] * n)[:n]
        self._values = [max(self._min_db, min(self._max_db, v)) for v in vals]
        self.update()

    def set_value(self, index: int, db: float):
        if 0 <= index < len(self._values):
            self._values[index] = max(self._min_db, min(self._max_db, db))
            self.update()

    # ── Geometry helpers ─────────────────────────────────────────────────

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            max(1, self.width() - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, self.height() - self._MARGIN_TOP - self._MARGIN_BOTTOM),
        )

    def _x_for_index(self, index: int) -> float:
        rect = self._plot_rect()
        n = len(self._freqs)
        if n <= 1:
            return rect.center().x()
        return rect.left() + (index / (n - 1)) * rect.width()

    def _y_for_db(self, db: float) -> float:
        rect = self._plot_rect()
        span = self._max_db - self._min_db
        ratio = (self._max_db - db) / span if span else 0.5
        return rect.top() + ratio * rect.height()

    def _db_for_y(self, y: float) -> float:
        rect = self._plot_rect()
        ratio = (y - rect.top()) / rect.height() if rect.height() else 0.0
        ratio = max(0.0, min(1.0, ratio))
        return self._max_db - ratio * (self._max_db - self._min_db)

    def _points(self) -> list[QPointF]:
        return [QPointF(self._x_for_index(i), self._y_for_db(v)) for i, v in enumerate(self._values)]

    @staticmethod
    def _smooth_path(points: list[QPointF]) -> QPainterPath:
        """Catmull-Rom through the band points, converted to cubic Bezier
        segments — gives the gently curved line from the reference photo
        instead of sharp straight-line joins between bands."""
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        if len(points) == 1:
            return path
        pts = [points[0]] + points + [points[-1]]
        for i in range(1, len(pts) - 2):
            p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
            c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0, p1.y() + (p2.y() - p0.y()) / 6.0)
            c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0, p2.y() - (p3.y() - p1.y()) / 6.0)
            path.cubicTo(c1, c2, p2)
        return path

    # ── Painting ─────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()
        border = QColor(COLORS["BORDER"])
        text_secondary = QColor(COLORS["TEXT_SECONDARY"])

        # dB gridlines + labels (top/zero/bottom)
        grid_pen = QPen(border)
        grid_pen.setWidthF(1.0)
        p.setFont(QFont("Segoe UI", 8))
        for db, text in ((self._max_db, f"+{int(self._max_db)}dB"), (0.0, ""), (self._min_db, f"{int(self._min_db)}dB")):
            y = self._y_for_db(db)
            pen = QPen(border if db != 0 else border.lighter(130))
            pen.setWidthF(1.0)
            if db == 0:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            if text:
                p.setPen(QPen(text_secondary))
                p.drawText(QRectF(0, y - 8, self._MARGIN_LEFT - 8, 16),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

        # Vertical gridlines + frequency labels per band
        p.setPen(QPen(border))
        for i, freq in enumerate(self._freqs):
            x = self._x_for_index(i)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.setPen(QPen(text_secondary))
            p.drawText(QRectF(x - 24, rect.bottom() + 4, 48, 16),
                       Qt.AlignmentFlag.AlignCenter, _format_eq_freq(freq))
            p.setPen(QPen(border))

        if not self._freqs:
            p.end()
            return

        points = self._points()
        line_path = self._smooth_path(points)

        # Gradient fill under the curve, down to the bottom of the plot.
        fill_path = QPainterPath(line_path)
        fill_path.lineTo(points[-1].x(), rect.bottom())
        fill_path.lineTo(points[0].x(), rect.bottom())
        fill_path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(styles_module.accent_fade_brush(0, rect.top(), 0, rect.bottom(), alpha_start=110, alpha_end=0))
        p.drawPath(fill_path)

        # The curve line itself.
        line_pen = QPen(QColor(COLORS["TEXT_PRIMARY"]))
        line_pen.setWidthF(2.0)
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(line_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(line_path)

        # Draggable dots.
        for i, pt in enumerate(points):
            active = i == self._drag_index
            radius = self._DOT_RADIUS + (1.5 if active else 0)
            p.setPen(QPen(QColor(COLORS["TEXT_PRIMARY"]), 1.5))
            if active:
                p.setBrush(styles_module.accent_brush(pt.x() - radius, 0, pt.x() + radius, 0))
            else:
                p.setBrush(QBrush(QColor(COLORS["TEXT_PRIMARY"])))
            p.drawEllipse(pt, radius, radius)

        p.end()

    # ── Interaction ──────────────────────────────────────────────────────

    def _nearest_index(self, x: float) -> int | None:
        if not self._freqs:
            return None
        best_i, best_dist = None, None
        for i in range(len(self._freqs)):
            dist = abs(self._x_for_index(i) - x)
            if best_dist is None or dist < best_dist:
                best_i, best_dist = i, dist
        rect = self._plot_rect()
        col_width = rect.width() / max(1, len(self._freqs) - 1) if len(self._freqs) > 1 else rect.width()
        if best_dist is not None and best_dist <= max(self._HIT_RADIUS, col_width / 2):
            return best_i
        return None

    def _set_from_mouse(self, index: int, y: float):
        db = self._db_for_y(y)
        db = round(db)  # целые dB — как и раньше, не точный микшер
        if db != self._values[index]:
            self._values[index] = db
            self.update()
            self.bandChanged.emit(index, float(db))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        pos = event.position()
        index = self._nearest_index(pos.x())
        if index is None:
            return
        self._drag_index = index
        self._set_from_mouse(index, pos.y())
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_index is None:
            return
        self._set_from_mouse(self._drag_index, event.position().y())

    def mouseReleaseEvent(self, event):
        if self._drag_index is not None:
            self._drag_index = None
            self.update()


class SettingsPage(QWidget):
    logout_clicked = pyqtSignal()
    accent_changed = pyqtSignal(str, str)  # (color1, color2-or-"")
    theme_changed = pyqtSignal(str)
    scale_changed = pyqtSignal(float)
    discord_rpc_toggled = pyqtSignal(bool)
    cover_cache_cleared = pyqtSignal()
    library_cache_cleared = pyqtSignal()
    player_data_cache_cleared = pyqtSignal()
    local_library_toggled = pyqtSignal(bool)
    open_local_folder_clicked = pyqtSignal()
    eq_enabled_toggled = pyqtSignal(bool)
    eq_band_changed = pyqtSignal(int, float)
    eq_preamp_changed = pyqtSignal(float)
    eq_reset_clicked = pyqtSignal()

    SCALE_PRESETS = [("75%", 0.75), ("100%", 1.0), ("125%", 1.25), ("150%", 1.5)]

    def __init__(self, eq_band_freqs: list[float] | None = None, parent=None):
        super().__init__(parent)
        self._discord_toggle = None
        self._local_lib_toggle: _ToggleSwitch | None = None
        self._accent_btns: list[QPushButton] = []
        self._current_accent = ""
        self._current_accent2: str | None = None
        self._gradient_toggle: _ToggleSwitch | None = None
        self._gradient_swatch1: QPushButton | None = None
        self._gradient_swatch2: QPushButton | None = None
        self._theme_btns: dict[str, QPushButton] = {}
        self._current_theme = "dark"
        self._scale_btns: dict[float, QPushButton] = {}
        self._current_scale = 1.0
        self._eq_band_freqs = eq_band_freqs or []
        self._eq_toggle: _ToggleSwitch | None = None
        self._eq_preamp_slider: QSlider | None = None
        self._eq_preamp_value_lbl: QLabel | None = None
        self._eq_curve: _EqCurveWidget | None = None
        self._eq_controls_widget: QWidget | None = None
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

    @staticmethod
    def _add_clear_row(card_layout: QVBoxLayout, label_text: str) -> QPushButton:
        """One "<label> ... [Очистить]" row for the Data card — returns the
        button so the caller can wire up its own clear signal."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        lbl = QLabel(label_text)
        lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        lbl.setWordWrap(True)
        row.addWidget(lbl, 1)

        btn = QPushButton("Очистить")
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 12px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        row.addWidget(btn)
        card_layout.addLayout(row)
        return btn

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(get_scrollbar_style())
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)

        # Cards below can require more height than the window's minimum size
        # allows — without the QScrollArea above, a short window used to
        # squeeze every fixed-height row into whatever space was left,
        # rendering them stacked on top of each other instead of scrolling.
        layout = QVBoxLayout(container)
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

        custom_btn = QPushButton("+")
        custom_btn.setFixedSize(32, 32)
        custom_btn.setToolTip("Свой цвет")
        custom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        custom_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        custom_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 2px dashed {COLORS['BORDER']}; "
            f"border-radius: 16px; color: {COLORS['TEXT_SECONDARY']}; font-size: 15px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        custom_btn.clicked.connect(self._on_custom_accent_clicked)
        palette_row.addWidget(custom_btn)

        palette_row.addStretch(1)
        accent_card.addLayout(palette_row)
        self._restyle_accent_buttons()

        # ── Gradient accent row ──────────────────────────────────────────────
        gradient_row = QHBoxLayout()
        gradient_row.setSpacing(10)
        gradient_row.setContentsMargins(0, 4, 0, 0)

        gradient_lbl = QLabel("Градиент из двух цветов")
        gradient_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        gradient_row.addWidget(gradient_lbl)
        gradient_row.addStretch(1)

        self._gradient_swatch1 = self._make_gradient_swatch_btn()
        self._gradient_swatch1.setToolTip("Первый цвет градиента")
        self._gradient_swatch1.clicked.connect(partial(self._on_gradient_swatch_clicked, 0))
        gradient_row.addWidget(self._gradient_swatch1)

        self._gradient_swatch2 = self._make_gradient_swatch_btn()
        self._gradient_swatch2.setToolTip("Второй цвет градиента")
        self._gradient_swatch2.clicked.connect(partial(self._on_gradient_swatch_clicked, 1))
        gradient_row.addWidget(self._gradient_swatch2)

        self._gradient_toggle = _ToggleSwitch()
        self._gradient_toggle.toggled.connect(self._on_gradient_toggled)
        gradient_row.addWidget(self._gradient_toggle)

        accent_card.addLayout(gradient_row)
        self._restyle_gradient_swatches()

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

        # ── Equalizer card ───────────────────────────────────────────────────
        self._build_eq_card(layout)

        # ── Data card ────────────────────────────────────────────────────────
        # Three independent "Очистить" rows rather than one button that wipes
        # everything — clearing the library cache used to also blow away the
        # cover cache (and vice versa), which is surprising if you only
        # wanted one of them gone.
        data_card = self._make_card(layout)
        data_card.addWidget(self._section_label("Данные"))

        cover_btn = self._add_clear_row(data_card, "Кеш обложек")
        cover_btn.clicked.connect(self.cover_cache_cleared.emit)

        library_btn = self._add_clear_row(data_card, "Кеш библиотеки")
        library_btn.clicked.connect(self.library_cache_cleared.emit)

        player_data_btn = self._add_clear_row(data_card, "Локальные данные аккаунта (лайки, подписки)")
        player_data_btn.clicked.connect(self.player_data_cache_cleared.emit)

        # ── Local library card ──────────────────────────────────────────────
        local_lib_card = self._make_card(layout)
        local_lib_card.addWidget(self._section_label("Локальная библиотека"))

        local_lib_row = QHBoxLayout()
        local_lib_row.setContentsMargins(0, 0, 0, 0)
        local_lib_row.setSpacing(10)
        local_lib_lbl = QLabel("Показывать папку music в сайдбаре")
        local_lib_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']}; font: 10pt 'Segoe UI';")
        local_lib_row.addWidget(local_lib_lbl)
        local_lib_row.addStretch(1)

        self._local_lib_toggle = _ToggleSwitch()
        self._local_lib_toggle.toggled.connect(self.local_library_toggled.emit)
        local_lib_row.addWidget(self._local_lib_toggle)
        local_lib_card.addLayout(local_lib_row)

        open_folder_row = QHBoxLayout()
        open_folder_row.setContentsMargins(0, 0, 0, 0)
        open_folder_row.setSpacing(10)
        open_folder_lbl = QLabel("Папка с исполнителями")
        open_folder_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI';")
        open_folder_row.addWidget(open_folder_lbl)
        open_folder_row.addStretch(1)

        open_folder_btn = QPushButton("Открыть папку")
        open_folder_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        open_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_folder_btn.setFixedHeight(30)
        open_folder_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 12px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        open_folder_btn.clicked.connect(self.open_local_folder_clicked.emit)
        open_folder_row.addWidget(open_folder_btn)
        local_lib_card.addLayout(open_folder_row)

        # ── About card ───────────────────────────────────────────────────────
        about_card = self._make_card(layout)
        about_card.addWidget(self._section_label("О приложении"))

        version_lbl = QLabel(f"Версия {APP_VERSION}")
        version_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        about_card.addWidget(version_lbl)

        links_row = QHBoxLayout()
        links_row.setContentsMargins(0, 8, 0, 0)
        links_row.setSpacing(12)
        for icon_name, tooltip, url in (
            ("discord_icon.png", "Discord", "https://discord.gg/m3eX7JBBwE"),
            ("telegram_icon.png", "Telegram", "https://t.me/memifyapp"),
            ("memify_link_icon.png", "memify.memiras.net", "https://memify.memiras.net"),
        ):
            link_lbl = ClickableLabel("")
            link_lbl.setFixedSize(36, 36)
            link_lbl.setToolTip(tooltip)
            icon_path = os.path.join(ICONS_DIR, icon_name)
            if os.path.exists(icon_path):
                pm = QPixmap(icon_path)
                if not pm.isNull():
                    link_lbl.setPixmap(make_rounded_pixmap(pm, 36, 18))
            link_lbl.clicked.connect(partial(QDesktopServices.openUrl, QUrl(url)))
            links_row.addWidget(link_lbl)
        links_row.addStretch(1)
        about_card.addLayout(links_row)

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

    def _build_eq_card(self, layout: QVBoxLayout):
        eq_card = self._make_card(layout)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)
        header_row.addWidget(self._section_label("Эквалайзер"))
        header_row.addStretch(1)

        reset_btn = QPushButton("Сбросить")
        reset_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setFixedHeight(26)
        reset_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI'; padding: 0 10px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        reset_btn.clicked.connect(self._on_eq_reset_clicked)
        header_row.addWidget(reset_btn)

        self._eq_toggle = _ToggleSwitch()
        self._eq_toggle.toggled.connect(self._on_eq_toggle_changed)
        header_row.addWidget(self._eq_toggle)
        eq_card.addLayout(header_row)

        # Полосы у libVLC фиксированы (10 штук, не настраиваются) — если
        # плеер не на VLC-бэкенде (см. player_vlc.py), список частот будет
        # пустым и вместо графика показываем поясняющую подпись, а не
        # пустую карточку без объяснений.
        if not self._eq_band_freqs:
            unavailable_lbl = QLabel("Недоступно с текущим бэкендом воспроизведения.")
            unavailable_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
            eq_card.addWidget(unavailable_lbl)
            return

        self._eq_controls_widget = QWidget()
        controls_col = QVBoxLayout(self._eq_controls_widget)
        controls_col.setContentsMargins(0, 4, 0, 0)
        controls_col.setSpacing(10)

        preamp_row = QHBoxLayout()
        preamp_row.setContentsMargins(0, 0, 0, 0)
        preamp_row.setSpacing(10)
        preamp_lbl = QLabel("Preamp")
        preamp_lbl.setFixedWidth(52)
        preamp_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        preamp_row.addWidget(preamp_lbl)

        self._eq_preamp_slider = QSlider(Qt.Orientation.Horizontal)
        self._eq_preamp_slider.setRange(-12, 12)
        self._eq_preamp_slider.setValue(0)
        self._eq_preamp_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eq_preamp_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {COLORS['BORDER']}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {COLORS['PRIMARY_GRADIENT']}; width: 14px; height: 14px; "
            f"margin: -5px 0; border-radius: 7px; }}"
            f"QSlider::sub-page:horizontal {{ background: {COLORS['PRIMARY_GRADIENT']}; border-radius: 2px; }}"
            f"QSlider::add-page:horizontal {{ background: {COLORS['BORDER']}; border-radius: 2px; }}"
        )
        self._eq_preamp_slider.valueChanged.connect(self._on_eq_preamp_slider_changed)
        preamp_row.addWidget(self._eq_preamp_slider, stretch=1)

        self._eq_preamp_value_lbl = QLabel("0dB")
        self._eq_preamp_value_lbl.setFixedWidth(36)
        self._eq_preamp_value_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        preamp_row.addWidget(self._eq_preamp_value_lbl)
        controls_col.addLayout(preamp_row)

        self._eq_curve = _EqCurveWidget(self._eq_band_freqs)
        self._eq_curve.bandChanged.connect(self.eq_band_changed.emit)
        controls_col.addWidget(self._eq_curve)

        eq_card.addWidget(self._eq_controls_widget)
        self._eq_controls_widget.setEnabled(False)  # выключен, пока не включат тумблером

    def _on_eq_toggle_changed(self, enabled: bool):
        if self._eq_controls_widget is not None:
            self._eq_controls_widget.setEnabled(enabled)
        self.eq_enabled_toggled.emit(enabled)

    def _on_eq_preamp_slider_changed(self, value: int):
        self._eq_preamp_value_lbl.setText(f"{value:+d}dB" if value else "0dB")
        self.eq_preamp_changed.emit(float(value))

    def _on_eq_reset_clicked(self):
        if self._eq_preamp_slider is not None:
            self._eq_preamp_slider.blockSignals(True)
            self._eq_preamp_slider.setValue(0)
            self._eq_preamp_slider.blockSignals(False)
            self._eq_preamp_value_lbl.setText("0dB")
        if self._eq_curve is not None:
            self._eq_curve.set_values([0.0] * len(self._eq_band_freqs))
        self.eq_reset_clicked.emit()

    def set_eq_enabled(self, enabled: bool):
        if self._eq_toggle is None:
            return
        self._eq_toggle.blockSignals(True)
        self._eq_toggle.setChecked(enabled)
        self._eq_toggle.blockSignals(False)
        if self._eq_controls_widget is not None:
            self._eq_controls_widget.setEnabled(enabled)

    def set_eq_values(self, preamp: float, bands: list[float]):
        if self._eq_preamp_slider is not None:
            self._eq_preamp_slider.blockSignals(True)
            self._eq_preamp_slider.setValue(int(round(preamp)))
            self._eq_preamp_slider.blockSignals(False)
            self._eq_preamp_value_lbl.setText(f"{int(round(preamp)):+d}dB" if preamp else "0dB")
        if self._eq_curve is not None:
            self._eq_curve.set_values(bands)

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

    def set_selected_accent(self, color: str, color2: str | None = None):
        """Reflect the currently active accent in the swatch row + gradient
        controls (call on load and after changes)."""
        self._current_accent = color or ""
        self._current_accent2 = color2 or None
        self._restyle_accent_buttons()
        self._restyle_gradient_swatches()
        if self._gradient_toggle is not None:
            self._gradient_toggle.blockSignals(True)
            self._gradient_toggle.setChecked(bool(self._current_accent2))
            self._gradient_toggle.blockSignals(False)

    def _make_gradient_swatch_btn(self) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _restyle_gradient_swatches(self):
        if self._gradient_swatch1 is None or self._gradient_swatch2 is None:
            return
        c1 = self._current_accent or COLORS["PRIMARY"]
        c2 = self._current_accent2 or c1
        for btn, color in ((self._gradient_swatch1, c1), (self._gradient_swatch2, c2)):
            btn.setStyleSheet(
                f"QPushButton {{ background: {color}; border: 2px solid {COLORS['BORDER']}; "
                f"border-radius: 14px; }}"
                f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; }}"
            )

    def _on_custom_accent_clicked(self):
        initial = QColor(self._current_accent or COLORS["PRIMARY"])
        picked = QColorDialog.getColor(initial, self, "Свой цвет акцента")
        if picked.isValid():
            self._on_accent_clicked(picked.name())

    def _on_gradient_swatch_clicked(self, index: int):
        current = QColor((self._current_accent2 if index else self._current_accent) or COLORS["PRIMARY"])
        picked = QColorDialog.getColor(current, self, "Цвет градиента")
        if not picked.isValid():
            return
        if index == 0:
            self._current_accent = picked.name()
        else:
            self._current_accent2 = picked.name()
        if self._gradient_toggle is not None and self._gradient_toggle.isChecked():
            self._apply_accent(self._current_accent, self._current_accent2)
        else:
            self._restyle_gradient_swatches()

    def _on_gradient_toggled(self, checked: bool):
        if checked:
            color2 = self._current_accent2 or self._auto_second_color(self._current_accent)
            self._apply_accent(self._current_accent, color2)
        else:
            self._apply_accent(self._current_accent, None)

    @staticmethod
    def _auto_second_color(color: str) -> str:
        """A reasonable default 2nd stop when gradient mode is turned on
        without one picked yet — same hue, shifted lighter/darker so the
        gradient reads clearly instead of looking almost solid."""
        c = QColor(color or COLORS["PRIMARY"])
        h, s, v, a = c.getHsvF()
        shifted = QColor.fromHsvF(h, s, max(0.0, v - 0.35) if v > 0.5 else min(1.0, v + 0.45), a)
        return shifted.name()

    def _apply_accent(self, color1: str, color2: str | None):
        set_accent_color(color1, color2)
        self.set_selected_accent(color1, color2)
        self.accent_changed.emit(color1, color2 or "")

    def _style_choice_button(self, btn: QPushButton, selected: bool):
        c = COLORS
        if selected:
            btn.setStyleSheet(
                f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 16px; "
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

    def set_local_library_enabled(self, enabled: bool):
        if self._local_lib_toggle is None:
            return
        self._local_lib_toggle.blockSignals(True)
        self._local_lib_toggle.setChecked(enabled)
        self._local_lib_toggle.blockSignals(False)

    def _on_accent_clicked(self, color: str):
        """A preset/custom single-color pick always switches to solid —
        gradient stays an opt-in via its own toggle."""
        self._apply_accent(color, None)

    def _on_theme_clicked(self, mode: str):
        self.set_selected_theme(mode)
        self.theme_changed.emit(mode)

    def _on_scale_clicked(self, scale: float):
        self.set_selected_scale(scale)
        self.scale_changed.emit(scale)


# ──────────────────────────────────────────────────────────────────────────────
# Main application window
# ──────────────────────────────────────────────────────────────────────────────

class _LibraryLoadSignal(QObject):
    """Bridges a background library-refresh back to the main thread — see
    _DiscordConnectSignal below for why this is a plain daemon thread
    rather than a QThread/moveToThread worker: in this environment, a
    QThread's started->run() connection has intermittently just never
    fired (observed directly — the thread object exists, but its run()
    slot is silently never called), stalling library/player-data loading
    indefinitely with no error. A daemon thread's target function always
    runs; emitting a signal from it marshals delivery to the main thread
    the same way a QThread's queued connection would."""
    finished = pyqtSignal()


class _PlayerDataLoadSignal(QObject):
    # object, not dict — a failed fetch with nothing cached yet to fall back
    # on carries None through here (see AccountManager.fetch_player_data),
    # which a dict-typed signal can't transport.
    finished = pyqtSignal(object)


class _UserSearchSignal(QObject):
    # (generation, results) — generation lets the receiver drop stale
    # responses that finish out of order relative to a newer query.
    finished = pyqtSignal(int, list)


class _YoutubeSearchSignal(QObject):
    finished = pyqtSignal(int, list, str)  # (generation, results, error message — "" on success)


class _YoutubeStreamSignal(QObject):
    finished = pyqtSignal(str, str, str)  # (webpage_url, resolved_stream_url, error message)


class _UserProfileSignal(QObject):
    finished = pyqtSignal(dict)


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


def _card_widget(parent_layout: QVBoxLayout) -> QVBoxLayout:
    """Same rounded-surface card SettingsPage uses, as a free function so
    ProfilePage/UserProfilePage can share it without inheriting SettingsPage."""
    card = QWidget()
    card.setStyleSheet(f"QWidget {{ background: {COLORS['SURFACE']}; border-radius: 12px; }}")
    inner = QVBoxLayout(card)
    inner.setContentsMargins(18, 16, 18, 16)
    inner.setSpacing(12)
    parent_layout.addWidget(card)
    return inner


def _card_section_label(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 600 8.5pt 'Segoe UI'; letter-spacing: 1px;")
    return lbl


def _line_edit_style() -> str:
    return (
        f"QLineEdit {{ background: {COLORS['SURFACE_LIGHT']}; color: {COLORS['TEXT_PRIMARY']}; "
        f"border: 1px solid {COLORS['BORDER']}; border-radius: 8px; padding: 0 10px; font: 10.5pt 'Segoe UI'; }}"
        f"QLineEdit:focus {{ border-color: {COLORS['PRIMARY']}; }}"
    )


def _menu_style() -> str:
    """Shared QMenu look for every popup/context menu in the app, in place
    of the native OS menu style. QMenu::indicator is left untouched so
    Fusion still draws its own checkmark glyph for checkable actions."""
    c = COLORS
    return (
        f"QMenu {{ background: {c['SURFACE']}; border: 1px solid {c['BORDER']}; "
        f"border-radius: 8px; padding: 4px; }}"
        f"QMenu::item {{ background: transparent; color: {c['TEXT_PRIMARY']}; "
        f"padding: 7px 24px 7px 10px; border-radius: 6px; font: 10pt 'Segoe UI'; }}"
        f"QMenu::item:selected {{ background: {c['SURFACE_HOVER']}; color: {c['TEXT_PRIMARY']}; }}"
        f"QMenu::item:disabled {{ color: {c['TEXT_SECONDARY']}; }}"
        f"QMenu::separator {{ height: 1px; background: {c['BORDER']}; margin: 4px 8px; }}"
        f"QMenu::icon {{ padding-left: 4px; }}"
    )


_RU_MONTHS_SHORT = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


class _ListenHeatmap(QWidget):
    """GitHub-contributions-style calendar: one column per week (Monday-start),
    one row per weekday, cell shade = hours actually played that day.

    `_stats` maps "YYYY-MM-DD" -> seconds played, mirroring MainWindow's
    listen_stats (see _accumulate_listen_time)."""

    CELL = 11
    GAP = 3
    ROWS = 7
    WEEKS = 53
    _LABEL_GUTTER = 24
    _TOP_GUTTER = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stats: dict = {}
        self._cells: list = []  # [(QRectF, date, seconds), ...] for hit-testing
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")
        w = self._LABEL_GUTTER + self.WEEKS * (self.CELL + self.GAP)
        h = self._TOP_GUTTER + self.ROWS * (self.CELL + self.GAP)
        self.setFixedSize(w, h)

    def set_stats(self, stats: dict):
        parsed = {}
        for k, v in (stats or {}).items():
            try:
                parsed[k] = float(v or 0)
            except Exception:
                continue
        self._stats = parsed
        self.update()

    def _level_color(self, seconds: float) -> QColor:
        hours = seconds / 3600.0
        empty = QColor(COLORS["SURFACE_LIGHT"])
        if hours <= 0:
            return empty
        base = QColor(COLORS["PRIMARY"])
        if hours < 0.5:
            t = 0.30
        elif hours < 1.5:
            t = 0.55
        elif hours < 3:
            t = 0.78
        else:
            t = 1.0
        return QColor(
            int(empty.red() + (base.red() - empty.red()) * t),
            int(empty.green() + (base.green() - empty.green()) * t),
            int(empty.blue() + (base.blue() - empty.blue()) * t),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        today = date.today()
        end_monday = today - timedelta(days=today.weekday())
        start_monday = end_monday - timedelta(weeks=self.WEEKS - 1)

        painter.setPen(QColor(COLORS["TEXT_SECONDARY"]))
        painter.setFont(QFont("Segoe UI", 8))
        last_month = None
        for col in range(self.WEEKS):
            week_monday = start_monday + timedelta(weeks=col)
            if week_monday.day <= 7 and week_monday.month != last_month:
                last_month = week_monday.month
                x = self._LABEL_GUTTER + col * (self.CELL + self.GAP)
                painter.drawText(x, self._TOP_GUTTER - 4, _RU_MONTHS_SHORT[week_monday.month - 1])

        for row, text in ((0, "Пн"), (5, "Сб")):
            y = self._TOP_GUTTER + row * (self.CELL + self.GAP) + self.CELL
            painter.drawText(0, y, text)

        self._cells = []
        painter.setPen(Qt.PenStyle.NoPen)
        for col in range(self.WEEKS):
            for row in range(self.ROWS):
                d = start_monday + timedelta(weeks=col, days=row)
                if d > today:
                    continue
                seconds = self._stats.get(d.isoformat(), 0.0)
                x = self._LABEL_GUTTER + col * (self.CELL + self.GAP)
                y = self._TOP_GUTTER + row * (self.CELL + self.GAP)
                rect = QRectF(x, y, self.CELL, self.CELL)
                painter.setBrush(self._level_color(seconds))
                painter.drawRoundedRect(rect, 2, 2)
                self._cells.append((rect, d, seconds))

    def mouseMoveEvent(self, event):
        pos = event.position()
        for rect, d, seconds in self._cells:
            if rect.contains(pos):
                hours = seconds / 3600.0
                if seconds > 0:
                    txt = f"{d.strftime('%d.%m.%Y')} — {hours:.1f} ч"
                else:
                    txt = f"{d.strftime('%d.%m.%Y')} — нет прослушиваний"
                QToolTip.showText(event.globalPosition().toPoint(), txt, self)
                return
        QToolTip.hideText()

    def leaveEvent(self, event):
        QToolTip.hideText()


class ProfilePage(QWidget):
    """Own-account page: avatar + display name editing, plus a search box
    to find other users by login/display name."""
    display_name_save_requested = pyqtSignal(str)
    avatar_file_selected = pyqtSignal(str)   # local path chosen via file dialog
    user_search_changed = pyqtSignal(str)
    user_result_clicked = pyqtSignal(dict)
    playlist_clicked = pyqtSignal(str)               # playlist id
    playlist_create_requested = pyqtSignal()
    playlist_rename_requested = pyqtSignal(str, str)  # (id, new name)
    playlist_delete_requested = pyqtSignal(str)       # id
    playlist_visibility_toggled = pyqtSignal(str, bool)  # (id, public)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runners: list = []
        self._result_rows: list = []
        self._playlist_rows: list = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(280)
        self._search_timer.timeout.connect(self._emit_search)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(get_scrollbar_style())
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(20)

        hdr = QLabel("Профиль")
        hdr.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        layout.addWidget(hdr)

        # ── Identity card ────────────────────────────────────────────────
        card = _card_widget(layout)
        card.addWidget(_card_section_label("Аккаунт"))

        id_row = QHBoxLayout()
        id_row.setSpacing(16)

        self._avatar_btn = _AvatarButton(size=88)
        self._avatar_btn.setToolTip("Изменить аватар")
        self._avatar_btn.clicked.connect(self._pick_avatar_file)
        id_row.addWidget(self._avatar_btn)

        fields_col = QVBoxLayout()
        fields_col.setSpacing(8)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Отображаемое имя")
        self._name_edit.setFixedHeight(34)
        self._name_edit.setStyleSheet(_line_edit_style())
        name_row.addWidget(self._name_edit, 1)

        save_btn = QPushButton("Сохранить")
        save_btn.setFixedHeight(34)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        save_btn.setStyleSheet(
            f"QPushButton {{ background: {COLORS['PRIMARY']}; color: white; border: none; "
            f"border-radius: 8px; padding: 0 16px; font: 600 9.5pt 'Segoe UI'; }}"
            f"QPushButton:hover {{ background: {COLORS['PRIMARY_HOVER']}; }}"
        )
        save_btn.clicked.connect(self._on_save_name_clicked)
        name_row.addWidget(save_btn)
        fields_col.addLayout(name_row)

        self._identity_lbl = QLabel("")
        self._identity_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI';")
        self._identity_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fields_col.addWidget(self._identity_lbl)

        id_row.addLayout(fields_col, 1)
        card.addLayout(id_row)

        # ── Listening activity heatmap ──────────────────────────────────
        activity_card = _card_widget(layout)
        activity_card.addWidget(_card_section_label("Активность прослушивания"))

        self._heatmap = _ListenHeatmap()
        heatmap_scroll = QScrollArea()
        heatmap_scroll.setWidgetResizable(False)
        heatmap_scroll.setFrameShape(QFrame.Shape.NoFrame)
        heatmap_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        heatmap_scroll.setFixedHeight(self._heatmap.height() + 4)
        heatmap_scroll.setStyleSheet(
            get_scrollbar_style()
            + "QScrollArea { background: transparent; border: none; }"
            + "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        heatmap_scroll.setWidget(self._heatmap)
        activity_card.addWidget(heatmap_scroll)

        self._heatmap_caption = QLabel("")
        self._heatmap_caption.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        activity_card.addWidget(self._heatmap_caption)

        # ── Playlists card ──────────────────────────────────────────────
        playlists_card = _card_widget(layout)
        playlists_hdr_row = QHBoxLayout()
        playlists_hdr_row.setContentsMargins(0, 0, 0, 0)
        playlists_hdr_row.addWidget(_card_section_label("Мои плейлисты"))
        playlists_hdr_row.addStretch(1)

        create_playlist_btn = QPushButton("+ Новый плейлист")
        create_playlist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        create_playlist_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        create_playlist_btn.setFixedHeight(28)
        create_playlist_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI'; padding: 0 12px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        create_playlist_btn.clicked.connect(self.playlist_create_requested.emit)
        playlists_hdr_row.addWidget(create_playlist_btn)
        playlists_card.addLayout(playlists_hdr_row)

        self._playlists_col = QVBoxLayout()
        self._playlists_col.setSpacing(2)
        playlists_card.addLayout(self._playlists_col)

        self._playlists_empty_lbl = QLabel("У вас пока нет плейлистов")
        self._playlists_empty_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI';")
        playlists_card.addWidget(self._playlists_empty_lbl)

        # ── User search card ────────────────────────────────────────────
        search_card = _card_widget(layout)
        search_card.addWidget(_card_section_label("Найти пользователей"))

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Логин или отображаемое имя...")
        self._search_edit.setFixedHeight(34)
        self._search_edit.setStyleSheet(_line_edit_style())
        self._search_edit.textChanged.connect(lambda _: self._search_timer.start())
        search_card.addWidget(self._search_edit)

        self._results_col = QVBoxLayout()
        self._results_col.setSpacing(2)
        search_card.addLayout(self._results_col)

        self._results_empty_lbl = QLabel("")
        self._results_empty_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI';")
        self._results_empty_lbl.setVisible(False)
        search_card.addWidget(self._results_empty_lbl)

        layout.addStretch(1)

    def _emit_search(self):
        self.user_search_changed.emit(self._search_edit.text().strip())

    def _pick_avatar_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите аватар", "", "Изображения (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.avatar_file_selected.emit(path)

    def _on_save_name_clicked(self):
        self.display_name_save_requested.emit(self._name_edit.text().strip())

    def set_identity(self, login: str, account_id: str, display_name: str):
        self._name_edit.setText(display_name or "")
        self._avatar_btn.set_fallback_letter(display_name or login)
        parts = [f"@{login}"] if login else []
        if account_id:
            parts.append(f"ID: {account_id}")
        self._identity_lbl.setText("  •  ".join(parts))

    def set_avatar_pixmap(self, pixmap: QPixmap | None):
        self._avatar_btn.set_avatar_pixmap(pixmap)

    def apply_accent(self):
        self._avatar_btn.apply_accent()
        self._heatmap.update()

    def set_listen_stats(self, stats: dict):
        self._heatmap.set_stats(stats)
        cutoff = date.today() - timedelta(days=364)
        total_seconds = 0.0
        for k, v in (stats or {}).items():
            try:
                d = date.fromisoformat(k)
            except Exception:
                continue
            if d < cutoff:
                continue
            try:
                total_seconds += float(v or 0)
            except Exception:
                pass
        total_hours = total_seconds / 3600.0
        self._heatmap_caption.setText(f"{total_hours:.1f} ч прослушано за последние 365 дней")

    def set_playlists(self, playlists: list):
        for row in self._playlist_rows:
            self._playlists_col.removeWidget(row)
            row.deleteLater()
        self._playlist_rows.clear()

        playlists = playlists or []
        self._playlists_empty_lbl.setVisible(not playlists)

        for pl in playlists:
            if not isinstance(pl, dict):
                continue
            pid = pl.get("id", "")
            name = pl.get("name") or "Без названия"
            count = len(pl.get("tracks") or [])
            count_txt = (
                f"{count} трек" if count == 1 else
                f"{count} трека" if 2 <= count <= 4 else
                f"{count} треков"
            )
            visibility_txt = "Публичный" if pl.get("public") else "Приватный"
            subtitle = f"{count_txt} • {visibility_txt}"
            row = _SidebarItem(name, pid, radius=8, subtitle=subtitle)
            cover_pm = _decode_base64_pixmap(pl.get("cover_data") or "")
            if cover_pm is not None:
                row.set_cover(cover_pm)
            else:
                row.set_cover_text("♪", COLORS["SURFACE_LIGHT"])
            row.clicked.connect(lambda data: self.playlist_clicked.emit(data))
            row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            row.customContextMenuRequested.connect(lambda _pos, r=row, p=pl: self._show_playlist_menu(r, p))
            self._playlists_col.addWidget(row)
            self._playlist_rows.append(row)

    def _show_playlist_menu(self, row: '_SidebarItem', playlist: dict):
        menu = QMenu(self)
        menu.setStyleSheet(_menu_style())
        rename_act = menu.addAction("Переименовать")
        public_act = menu.addAction("Публичный плейлист")
        public_act.setCheckable(True)
        public_act.setChecked(bool(playlist.get("public")))
        menu.addSeparator()
        delete_act = menu.addAction("Удалить")
        action = menu.exec(QCursor.pos())
        if action == rename_act:
            name, ok = QInputDialog.getText(
                self, "Переименовать плейлист", "Новое название:", text=playlist.get("name", "")
            )
            name = (name or "").strip()
            if ok and name:
                self.playlist_rename_requested.emit(playlist.get("id", ""), name)
        elif action == public_act:
            self.playlist_visibility_toggled.emit(playlist.get("id", ""), not bool(playlist.get("public")))
        elif action == delete_act:
            self.playlist_delete_requested.emit(playlist.get("id", ""))

    def set_search_results(self, items: list):
        _stop_runners(self._runners)
        for row in self._result_rows:
            self._results_col.removeWidget(row)
            row.deleteLater()
        self._result_rows.clear()

        if not items:
            has_query = bool(self._search_edit.text().strip())
            self._results_empty_lbl.setText("Ничего не найдено" if has_query else "")
            self._results_empty_lbl.setVisible(has_query)
            return
        self._results_empty_lbl.setVisible(False)

        for item in items:
            login = item.get("login", "")
            display_name = item.get("display_name") or login
            row = _SidebarItem(display_name, item, radius=20, subtitle=f"@{login}")
            row.set_cover_text((display_name or "?")[:1].upper(), COLORS["PRIMARY"])
            row.clicked.connect(lambda data: self.user_result_clicked.emit(data))
            self._results_col.addWidget(row)
            self._result_rows.append(row)
            avatar_url = item.get("avatar_url")
            if avatar_url:
                self._load_result_avatar(row, avatar_url)

    def _load_result_avatar(self, row: '_SidebarItem', avatar_rel_url: str):
        url = resolve_media_url(avatar_rel_url)
        key = cache_key(url, 40, 20)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            row.set_cover(cached)
            return

        def on_loaded(loaded_url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, size, radius), pm)
                    row.set_cover(pm)
            except Exception:
                pass
        _start_image_loader([url], 40, 20, on_loaded, self._runners)


class UserProfilePage(QWidget):
    """Read-only view of another account's public profile."""
    back_clicked = pyqtSignal()
    playlist_clicked = pyqtSignal(dict)  # raw playlist dict from the profile payload

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runners: list = []
        self._playlist_rows: list = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 20, 30, 20)
        root.setSpacing(16)

        back_btn = QPushButton("←  Назад")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        back_btn.setFixedHeight(30)
        back_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {COLORS['BORDER']}; "
            f"border-radius: 6px; color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI'; padding: 0 12px; }}"
            f"QPushButton:hover {{ border-color: {COLORS['TEXT_PRIMARY']}; color: {COLORS['TEXT_PRIMARY']}; }}"
        )
        back_btn.clicked.connect(self.back_clicked.emit)
        root.addWidget(back_btn, 0, Qt.AlignmentFlag.AlignLeft)

        card = _card_widget(root)
        head_row = QHBoxLayout()
        head_row.setSpacing(16)

        self._avatar = _AvatarButton(size=88)
        self._avatar.setCursor(Qt.CursorShape.ArrowCursor)
        head_row.addWidget(self._avatar)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        self._name_lbl = QLabel("")
        self._name_lbl.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        self._name_lbl.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        text_col.addWidget(self._name_lbl)

        self._sub_lbl = QLabel("")
        self._sub_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 10pt 'Segoe UI';")
        self._sub_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_col.addWidget(self._sub_lbl)

        head_row.addLayout(text_col, 1)
        card.addLayout(head_row)

        playlists_card = _card_widget(root)
        playlists_card.addWidget(_card_section_label("Плейлисты"))
        self._playlists_col = QVBoxLayout()
        self._playlists_col.setSpacing(2)
        playlists_card.addLayout(self._playlists_col)
        self._playlists_empty_lbl = QLabel("Нет плейлистов")
        self._playlists_empty_lbl.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9.5pt 'Segoe UI';")
        playlists_card.addWidget(self._playlists_empty_lbl)

        root.addStretch(1)

    def set_profile(self, profile: dict):
        if not isinstance(profile, dict):
            return
        login = profile.get("login", "")
        display_name = profile.get("display_name") or login
        account_id = profile.get("account_id") or ""

        self._name_lbl.setText(display_name or f"@{login}")
        parts = [f"@{login}"] if login else []
        if account_id:
            parts.append(f"ID: {account_id}")
        if profile.get("is_artist"):
            parts.append("Исполнитель")
        self._sub_lbl.setText("  •  ".join(parts))
        self._avatar.set_fallback_letter(display_name or login)

        # The initial "partial" render (from a search result, before the full
        # /users/profile fetch lands) has no "playlists" key at all — leave
        # whatever's already shown alone rather than clearing it to empty.
        if "playlists" in profile:
            self._set_playlists(profile.get("playlists") or [], display_name or login)

        avatar_url = profile.get("avatar_url")
        if not avatar_url:
            self._avatar.set_avatar_pixmap(None)
            return

        _stop_runners(self._runners)
        url = resolve_media_url(avatar_url)
        key = cache_key(url, 88, 44)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._avatar.set_avatar_pixmap(cached)
            return

        def on_loaded(loaded_url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, size, radius), pm)
                    self._avatar.set_avatar_pixmap(pm)
            except Exception:
                pass
        _start_image_loader([url], 88, 44, on_loaded, self._runners)

    def _set_playlists(self, playlists: list, creator_name: str):
        for row in self._playlist_rows:
            self._playlists_col.removeWidget(row)
            row.deleteLater()
        self._playlist_rows.clear()

        playlists = [p for p in playlists if isinstance(p, dict)]
        self._playlists_empty_lbl.setVisible(not playlists)

        for pl in playlists:
            name = pl.get("name") or "Без названия"
            count = len(pl.get("tracks") or [])
            subtitle = (
                f"{count} трек" if count == 1 else
                f"{count} трека" if 2 <= count <= 4 else
                f"{count} треков"
            )
            row = _SidebarItem(name, pl, radius=8, subtitle=subtitle)
            cover_pm = _decode_base64_pixmap(pl.get("cover_data") or "")
            if cover_pm is not None:
                row.set_cover(cover_pm)
            else:
                row.set_cover_text("♪", COLORS["SURFACE_LIGHT"])
            row.clicked.connect(lambda data: self.playlist_clicked.emit(data))
            self._playlists_col.addWidget(row)
            self._playlist_rows.append(row)


class _PanelToggleHandle(QPushButton):
    """Thin always-visible strip docked at the window's right edge — click
    to slide NowPlayingSidePanel open/closed. Hand-drawn triangle (same
    technique as _ChevronButton) points left while collapsed (invites
    opening) and flips to point right once expanded (invites closing)."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setCheckable(True)
        self.setChecked(False)  # collapsed by default
        self.setFixedWidth(20)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Сейчас играет")
        self.setStyleSheet(
            f"QPushButton {{ background: {COLORS['SURFACE']}; border: none; "
            f"border-left: 1px solid {COLORS['SURFACE_LIGHT']}; }}"
            f"QPushButton:hover {{ background: {COLORS['SURFACE_LIGHT']}; }}"
        )

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rf = QRectF(self.rect())
        cx, cy = rf.center().x(), rf.center().y()
        s = 4.5
        path = QPainterPath()
        if self.isChecked():
            # Expanded — pointing right (click collapses).
            path.moveTo(cx - s * 0.55, cy - s)
            path.lineTo(cx + s * 0.65, cy)
            path.lineTo(cx - s * 0.55, cy + s)
        else:
            # Collapsed — pointing left (click expands).
            path.moveTo(cx + s * 0.55, cy - s)
            path.lineTo(cx - s * 0.65, cy)
            path.lineTo(cx + s * 0.55, cy + s)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["TEXT_SECONDARY"]))
        p.drawPath(path)
        p.end()


class NowPlayingSidePanel(QWidget):
    """Collapsible right-hand panel, mirroring the left Sidebar's presence
    but toggled via _PanelToggleHandle instead of always shown: the
    currently playing track (cover/title/artist, all clickable through to
    the album/artist), an "Об исполнителе" card with a subscribe toggle
    for the *playing* artist (independent of whatever artist the user
    happens to be browsing — see MusicApp._on_now_playing_subscribe_clicked),
    and "Далее в очереди" showing what plays next given the current
    shuffle/repeat state. Closed (0px wide) by default."""

    cover_clicked = pyqtSignal()
    title_clicked = pyqtSignal()
    artist_clicked = pyqtSignal()
    subscribe_clicked = pyqtSignal()
    queue_track_clicked = pyqtSignal()

    PANEL_WIDTH = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panel_width = 0
        self._is_subscribed = False
        self._anim: QPropertyAnimation | None = None
        self._dominant_color: QColor | None = None
        self.setObjectName("nowPlayingPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(0)
        self._build_ui()
        self._apply_panel_background()

    # Animated via QPropertyAnimation like _RotatingSpinner's `angle` /
    # SettingsPage's `knobPos` elsewhere in this file — QWidget itself has
    # no single animatable "width" property, so setFixedWidth() (pins both
    # min and max) is driven from a custom one instead.
    def _get_panel_width(self):
        return self._panel_width

    def _set_panel_width(self, w):
        self._panel_width = w
        self.setFixedWidth(max(0, int(w)))

    panelWidth = pyqtProperty(int, _get_panel_width, _set_panel_width)

    def set_expanded(self, expanded: bool):
        self._anim = QPropertyAnimation(self, b"panelWidth", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(self.PANEL_WIDTH if expanded else 0)
        self._anim.start()

    def _build_ui(self):
        # Wrapped in a scroll area (rather than a plain QVBoxLayout directly
        # on self, like before the artist bio was added) since the bio text
        # is variable-length and, combined with everything else already in
        # this panel, can now exceed the window's minimum height — without
        # this, content would just get clipped instead of scrolling.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        panel_scroll = QScrollArea()
        panel_scroll.setWidgetResizable(True)
        panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel_scroll.setStyleSheet(get_scrollbar_style())

        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(16)

        # ── Now playing ──────────────────────────────────────────────────
        self._cover_label = QLabel()
        self._cover_label.setFixedSize(180, 180)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setStyleSheet(f"background: {COLORS['SURFACE_LIGHT']}; border-radius: 8px;")
        self._cover_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cover_label.mousePressEvent = lambda e: self.cover_clicked.emit()
        cover_row = QHBoxLayout()
        cover_row.addStretch(1)
        cover_row.addWidget(self._cover_label)
        cover_row.addStretch(1)
        outer.addLayout(cover_row)

        self._title_label = QLabel("Ничего не играет")
        self._title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self._title_label.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        self._title_label.setWordWrap(True)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title_label.mousePressEvent = lambda e: self.title_clicked.emit()
        outer.addWidget(self._title_label)

        self._now_artist_label = QLabel("")
        self._now_artist_label.setFont(QFont("Segoe UI", 10))
        self._now_artist_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
        self._now_artist_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._now_artist_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._now_artist_label.mousePressEvent = lambda e: self.artist_clicked.emit()
        outer.addWidget(self._now_artist_label)

        # ── Об исполнителе ───────────────────────────────────────────────
        self._about_section = QWidget()
        about_col = QVBoxLayout(self._about_section)
        about_col.setContentsMargins(0, 0, 0, 0)
        about_col.setSpacing(10)

        divider1 = QFrame()
        divider1.setFrameShape(QFrame.Shape.HLine)
        divider1.setStyleSheet(f"color: {COLORS['BORDER']};")
        about_col.addWidget(divider1)

        about_hdr = QLabel("Об исполнителе")
        about_hdr.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        about_hdr.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        about_col.addWidget(about_hdr)

        self._artist_avatar = QLabel()
        self._artist_avatar.setFixedSize(72, 72)
        self._artist_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._artist_avatar.setStyleSheet(f"background: {COLORS['SURFACE_LIGHT']}; border-radius: 36px;")
        self._artist_avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self._artist_avatar.mousePressEvent = lambda e: self.artist_clicked.emit()
        avatar_row = QHBoxLayout()
        avatar_row.addStretch(1)
        avatar_row.addWidget(self._artist_avatar)
        avatar_row.addStretch(1)
        about_col.addLayout(avatar_row)

        self._about_artist_name = QLabel("")
        self._about_artist_name.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self._about_artist_name.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        self._about_artist_name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._about_artist_name.setWordWrap(True)
        self._about_artist_name.setCursor(Qt.CursorShape.PointingHandCursor)
        self._about_artist_name.mousePressEvent = lambda e: self.artist_clicked.emit()
        about_col.addWidget(self._about_artist_name)

        self._subscribe_btn = QPushButton("Подписаться")
        self._subscribe_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._subscribe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._subscribe_btn.setFixedHeight(32)
        self._subscribe_btn.clicked.connect(self.subscribe_clicked.emit)
        sub_row = QHBoxLayout()
        sub_row.addStretch(1)
        sub_row.addWidget(self._subscribe_btn)
        sub_row.addStretch(1)
        about_col.addLayout(sub_row)

        self._about_bio_label = QLabel("")
        self._about_bio_label.setFont(QFont("Segoe UI", 10))
        self._about_bio_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
        self._about_bio_label.setWordWrap(True)
        self._about_bio_label.setVisible(False)
        about_col.addWidget(self._about_bio_label)

        outer.addWidget(self._about_section)
        self._apply_subscribe_style()

        # ── Далее в очереди ──────────────────────────────────────────────
        self._queue_section = QWidget()
        queue_col = QVBoxLayout(self._queue_section)
        queue_col.setContentsMargins(0, 0, 0, 0)
        queue_col.setSpacing(10)

        divider2 = QFrame()
        divider2.setFrameShape(QFrame.Shape.HLine)
        divider2.setStyleSheet(f"color: {COLORS['BORDER']};")
        queue_col.addWidget(divider2)

        queue_hdr = QLabel("Далее в очереди")
        queue_hdr.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        queue_hdr.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        queue_col.addWidget(queue_hdr)

        self._queue_track_widget = QWidget()
        self._queue_track_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self._queue_track_widget.mousePressEvent = lambda e: self.queue_track_clicked.emit()
        queue_row = QHBoxLayout(self._queue_track_widget)
        queue_row.setContentsMargins(0, 0, 0, 0)
        queue_row.setSpacing(10)
        self._queue_cover = QLabel()
        self._queue_cover.setFixedSize(44, 44)
        self._queue_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._queue_cover.setStyleSheet(f"background: {COLORS['SURFACE_LIGHT']}; border-radius: 4px;")
        queue_row.addWidget(self._queue_cover)
        queue_text_col = QVBoxLayout()
        queue_text_col.setSpacing(2)
        self._queue_title = QLabel("")
        self._queue_title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._queue_title.setStyleSheet(f"color: {COLORS['TEXT_PRIMARY']};")
        self._queue_title.setWordWrap(True)
        self._queue_artist = QLabel("")
        self._queue_artist.setFont(QFont("Segoe UI", 9))
        self._queue_artist.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']};")
        self._queue_artist.setWordWrap(True)
        queue_text_col.addWidget(self._queue_title)
        queue_text_col.addWidget(self._queue_artist)
        queue_row.addLayout(queue_text_col, 1)
        queue_col.addWidget(self._queue_track_widget)

        self._queue_empty_label = QLabel("Треков в очереди больше нет")
        self._queue_empty_label.setStyleSheet(f"color: {COLORS['TEXT_SECONDARY']}; font: 9pt 'Segoe UI';")
        self._queue_empty_label.setWordWrap(True)
        queue_col.addWidget(self._queue_empty_label)

        outer.addWidget(self._queue_section)
        outer.addStretch(1)

        panel_scroll.setWidget(content)
        root.addWidget(panel_scroll)

        self._about_section.setVisible(False)
        self._queue_section.setVisible(False)

    def _apply_subscribe_style(self):
        c = COLORS
        if self._is_subscribed:
            self._subscribe_btn.setText("Вы подписаны")
            self._subscribe_btn.setToolTip("Отписаться от исполнителя")
            self._subscribe_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1.5px solid {c['PRIMARY']}; border-radius: 16px; "
                f"color: {c['PRIMARY']}; font: 9.5pt 'Segoe UI'; font-weight: 600; padding: 0 16px; }}"
                f"QPushButton:hover {{ color: {c['PRIMARY_HOVER']}; border-color: {c['PRIMARY_HOVER']}; }}"
            )
        else:
            self._subscribe_btn.setText("Подписаться")
            self._subscribe_btn.setToolTip("Подписаться на исполнителя")
            self._subscribe_btn.setStyleSheet(
                f"QPushButton {{ background: {c['PRIMARY_GRADIENT']}; border: none; border-radius: 16px; "
                f"color: #000; font: 9.5pt 'Segoe UI'; font-weight: 600; padding: 0 16px; }}"
                f"QPushButton:hover {{ background: {c['PRIMARY_HOVER']}; }}"
            )

    def set_track(self, title: str, artist_name: str):
        self._title_label.setText(title or "Неизвестно")
        self._now_artist_label.setText(artist_name)
        self._now_artist_label.setVisible(bool(artist_name))

    def set_cover_pixmap(self, pixmap: QPixmap | None):
        has_cover = bool(pixmap and not pixmap.isNull())
        self._cover_label.setPixmap(pixmap if has_cover else QPixmap())
        self._dominant_color = _dominant_cover_color(pixmap) if has_cover else None
        self._apply_panel_background()

    def _apply_panel_background(self):
        """Tints the panel's background toward the current cover's dominant
        color, fading to the plain surface color further down — the same
        blend ratio in both light/dark themes keeps it subtle enough that
        text contrast never needs special-casing per theme. Falls back to
        the old flat surface color with nothing playing / no cover."""
        surface = QColor(COLORS['SURFACE'])
        if self._dominant_color is None:
            bg = f"background-color: {COLORS['SURFACE']};"
        else:
            top = _blend_color(surface, self._dominant_color, 0.24)
            bg = (
                f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                f"stop:0 {top.name()}, stop:0.6 {COLORS['SURFACE']}, stop:1 {COLORS['SURFACE']});"
            )
        self.setStyleSheet(
            f"QWidget#nowPlayingPanel {{ {bg} "
            f"border-left: 1px solid {COLORS['BORDER']}; }}"
        )

    def set_about_artist(self, artist_name: str, subscribable: bool):
        has_artist = bool(artist_name)
        self._about_section.setVisible(has_artist)
        # Cleared unconditionally here (not just on the no-artist branch) —
        # the bio itself arrives later via a separate async set_bio() call
        # (see MusicApp._refresh_now_playing_bio), and without this a stale
        # bio from the *previous* artist would stay on screen under the new
        # artist's name until that lookup resolves.
        self.set_bio("")
        if not has_artist:
            return
        self._about_artist_name.setText(artist_name)
        self._artist_avatar.setPixmap(QPixmap())
        self._subscribe_btn.setVisible(subscribable)

    def set_bio(self, text: str):
        text = (text or "").strip()
        self._about_bio_label.setText(text)
        self._about_bio_label.setVisible(bool(text))

    def set_artist_avatar_pixmap(self, pixmap: QPixmap | None):
        self._artist_avatar.setPixmap(pixmap if pixmap and not pixmap.isNull() else QPixmap())

    def set_subscribed(self, subscribed: bool):
        self._is_subscribed = subscribed
        self._apply_subscribe_style()

    def set_queue_track(self, title: str | None, artist_name: str | None):
        self._queue_section.setVisible(True)
        has_track = bool(title)
        self._queue_track_widget.setVisible(has_track)
        self._queue_empty_label.setVisible(not has_track)
        if has_track:
            self._queue_title.setText(title)
            self._queue_artist.setText(artist_name or "")
            self._queue_artist.setVisible(bool(artist_name))

    def set_queue_cover_pixmap(self, pixmap: QPixmap | None):
        self._queue_cover.setPixmap(pixmap if pixmap and not pixmap.isNull() else QPixmap())

    def clear(self):
        self._title_label.setText("Ничего не играет")
        self._now_artist_label.setText("")
        self._now_artist_label.setVisible(False)
        self._cover_label.setPixmap(QPixmap())
        self._about_section.setVisible(False)
        self._queue_section.setVisible(False)
        self.set_bio("")
        self._dominant_color = None
        self._apply_panel_background()

    def apply_accent(self):
        self._apply_subscribe_style()
        # Theme switches change COLORS['SURFACE']/['BORDER'] under us — redo
        # the blend against the new surface color, not just leave the old
        # theme's gradient sitting there.
        self._apply_panel_background()


class MusicApp(QWidget):
    logout_requested = pyqtSignal()

    def __init__(self, account_manager=None, offline: bool = False):
        super().__init__()
        self.setWindowTitle("Memify")
        self.resize(1280, 800)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))

        # No server reachable at startup (see main.py) — server library/
        # account features are skipped entirely; only the local library
        # works. One-shot for this process, no in-app way back online.
        self._offline = offline
        self._account_manager = account_manager
        self.library_manager = LibraryManager()
        # Fully separate from library_manager — populated from disk (not
        # network) via _init_local_library(), never merged into the server
        # catalog so it can't leak into "browse all artists to subscribe"
        # or similar server-catalog-only listings.
        self.local_library_manager = LibraryManager()
        self._last_search_query: str = ""
        self.player = PlayerController()

        self._current_artist: dict | None = None
        self._current_album: dict | None = None
        self._search_generation = 0
        self._search_thread: QThread | None = None
        self._search_worker: SearchWorker | None = None
        self._download_threads: list[QThread] = []
        self._img_runners: list = []   # [thread, worker] entries from _start_image_loader
        self._lyrics_runners: list = []   # [thread, worker] entries from _start_lyrics_worker
        # (artist, title) -> {"plain": str, "synced": list[(ms, text)]} — present-but-empty "plain" means looked up, not found
        self._lyrics_cache: dict[tuple[str, str], dict] = {}
        self._lyrics_request_id = 0   # staleness guard — see _refresh_now_playing_lyrics
        self._lyrics_viewer_key: tuple[str, str] | None = None   # (artist, title) LyricsViewerOverlay is showing
        self._now_playing_bio_runners: list = []   # [thread, worker] entries from _start_artist_bio_worker
        self._now_playing_bio_request_id = 0   # staleness guard — see _refresh_now_playing_bio
        self._prev_page_before_search: QWidget | None = None
        self._loading = False
        self._closing = False  # set once closeEvent starts, so scheduled/async
        # callbacks don't spawn more background threads after teardown began.
        self._media_keys = None
        self._mpris_service = None

        # Account state
        self._liked_tracks: list = []        # list of track dicts from server
        self._playlists: list = []           # list of {id, name, created_at, public, tracks: [track-ref...]}
        # Other accounts' public playlists this account has "+"-ed — a live
        # reference (owner_login, playlist_id), not a snapshot; re-fetched from
        # the owner's public profile whenever opened. name/cover_data cached
        # here too so the sidebar row can render without a network round trip.
        self._playlist_subscriptions: list = []
        self._subscriptions: list = []       # list of artist names
        self._album_subscriptions: list = [] # list of "artist||album" strings
        # User's custom sidebar order — "artist::Name" / "album::Artist||Title"
        # keys, letting artists and albums be freely interleaved instead of
        # always grouped as all-artists-then-all-albums.
        self._follow_order: list = []
        # False until _on_player_data_loaded has actually populated the
        # fields above at least once (from local cache or network) — see
        # its guard in _save_player_data_async.
        self._player_data_ready = False
        self._display_name: str = ""
        self._account_id: str = ""
        self._user_search_generation: int = 0
        self._viewed_profile: dict = {}  # last user profile shown on UserProfilePage
        # Keep-alive lists for in-flight background-load signal bridges (see
        # _LibraryLoadSignal) — daemon threads, so nothing to join/quit() on
        # close; entries are removed as each one finishes.
        self._library_load_signals: list = []
        self._player_data_load_signals: list = []
        self._user_search_signals: list = []
        self._user_profile_signals: list = []
        self._youtube_search_signals: list = []
        self._youtube_stream_signals: list = []
        # id(track) -> [continue_playback, ...] queued while a resolve for
        # that exact track is already in flight — de-dupes the network call
        # when play_track lands on the same still-unresolved YouTube track
        # twice in quick succession (e.g. a manual click racing an
        # auto-advance onto it), see _resolve_track_url_for_player.
        self._youtube_resolve_pending: dict = {}
        # Player-data *saves* (likes, playlists, follow_order, settings...)
        # all funnel through one queue processed by a single worker thread —
        # serialized, so two saves fired close together can never complete
        # out of order and have the older one's snapshot silently win over
        # the newer one (that used to intermittently revert a like/playlist
        # edit that raced another save in flight at the same time).
        self._player_data_save_queue: "queue.Queue" = queue.Queue()
        self._pending_player_data_saves = 0
        threading.Thread(target=self._player_data_save_worker, daemon=True).start()

        self._discord_rpc = None
        self._discord_connecting = False
        self._discord_connect_signal = None
        self._discord_refresh_timer: QTimer | None = None
        self._state_restored = False
        self._nav_restored = False   # True once page navigation succeeded
        self._playing_url: str = ""
        self._playing_track: dict | None = None
        self._player_warmed_up = False
        self._settings_sync_timer: QTimer | None = None
        # Daily listening-time heatmap (ProfilePage) — "YYYY-MM-DD" -> seconds
        # actually played that day, accumulated from real playback ticks (see
        # _accumulate_listen_time), flushed to the account periodically (see
        # _listen_stats_flush_timer) rather than on every tick.
        self._listen_stats: dict = {}
        self._listen_stats_dirty = False
        self._listen_last_tick: float | None = None
        self._listen_stats_flush_timer = QTimer(self)
        self._listen_stats_flush_timer.setInterval(20000)
        self._listen_stats_flush_timer.timeout.connect(self._flush_listen_stats)
        self._listen_stats_flush_timer.start()

        self._load_settings()
        self._setup_ui()
        self._setup_player()
        self._setup_media_keys()
        self._setup_mpris()
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
        accent2 = self._settings.get("accent_color2") or None
        set_accent_color(accent, accent2)
        set_theme(self._settings.get("theme", "dark"))
        app = QApplication.instance()
        if app:
            styles_module.apply_palette(app)
        self._audio_device_id = self._settings.get("audio_output_device_id")

    def _apply_loaded_settings(self):
        """Apply settings that require UI to already be built (called after _setup_ui)."""
        self._settings_page.set_selected_accent(
            self._settings.get("accent_color", COLORS["PRIMARY"]),
            self._settings.get("accent_color2") or None,
        )
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

        self._sidebar.set_server_collapsed(bool(self._settings.get("library_collapsed", False)))
        self._sidebar.set_local_collapsed(bool(self._settings.get("local_library_collapsed", False)))

        self._now_playing_toggle.setChecked(bool(self._settings.get("now_playing_panel_open", False)))

        local_lib_enabled = bool(self._settings.get("local_library_enabled", False))
        self._settings_page.set_local_library_enabled(local_lib_enabled)
        if self._offline:
            # Nothing else can work without a connection — show the local
            # library regardless of the saved toggle. Not persisted (no
            # _save_ui_state call): the real preference is untouched for
            # the next, hopefully-online, launch.
            self._sidebar.set_server_section_visible(False)
            self._sidebar.set_local_section_visible(True)
            self._init_local_library()
        else:
            self._sidebar.set_local_section_visible(local_lib_enabled)
            if local_lib_enabled:
                self._init_local_library()

        band_count = len(get_eq_band_frequencies())
        eq_enabled = bool(self._settings.get("eq_enabled", False))
        eq_preamp = float(self._settings.get("eq_preamp", 0.0) or 0.0)
        eq_bands = list(self._settings.get("eq_bands") or [0.0] * band_count)
        if len(eq_bands) != band_count:
            eq_bands = (eq_bands + [0.0] * band_count)[:band_count]
        self._settings_page.set_eq_values(eq_preamp, eq_bands)
        self._settings_page.set_eq_enabled(eq_enabled)
        self.player.set_eq_preamp(eq_preamp)
        for index, db in enumerate(eq_bands):
            self.player.set_eq_band(index, db)
        self.player.set_eq_enabled(eq_enabled)

    def _save_settings(self):
        try:
            self._settings["audio_output_device_id"] = self.player._preferred_audio_output_id
            with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self._schedule_settings_sync()

    def _save_ui_state(self, **kwargs):
        self._settings.update(kwargs)
        try:
            with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self._schedule_settings_sync()

    def _schedule_settings_sync(self):
        """Debounced push of _settings to the server (via app_settings in
        player data), so settings/likes/state carry over when logging in on
        another device. Debounced because some callers (volume slider drag)
        fire on every tick — without this, dragging the slider would fire a
        network request and spin up a QThread dozens of times a second."""
        if not self._account_manager or self._closing:
            return
        if self._settings_sync_timer is None:
            self._settings_sync_timer = QTimer(self)
            self._settings_sync_timer.setSingleShot(True)
            self._settings_sync_timer.timeout.connect(lambda: self._save_player_data_async({}))
        self._settings_sync_timer.start(800)

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
        self._sidebar.playlist_selected.connect(self._open_playlist)
        self._sidebar.playlist_subscription_selected.connect(self._on_playlist_subscription_clicked)
        self._sidebar.order_changed.connect(self._on_sidebar_order_changed)
        self._sidebar.section_collapsed_changed.connect(self._on_sidebar_section_collapsed_changed)
        self._sidebar.open_local_folder_requested.connect(self._on_open_local_folder_clicked)
        if self._offline:
            self._sidebar.set_offline_mode()
        elif self._account_manager and self._account_manager.active_login:
            self._sidebar.set_username(self._display_name or self._account_manager.active_login)
        body_row.addWidget(self._sidebar)

        # Page stack — pre-built, never recreated
        self._page_stack = QStackedWidget()
        self._welcome_page = WelcomePage()
        self._home_page = HomePage()
        self._artist_page = ArtistPage()
        self._artist_all_albums_page = ArtistAllAlbumsPage()
        self._album_page = AlbumPage()
        self._search_page = SearchPage()
        self._all_artists_page = AllArtistsPage()
        self._settings_page = SettingsPage(eq_band_freqs=get_eq_band_frequencies())
        self._profile_page = ProfilePage()
        self._user_profile_page = UserProfilePage()

        for page in [self._welcome_page, self._home_page, self._artist_page, self._artist_all_albums_page,
                     self._album_page, self._search_page, self._all_artists_page, self._settings_page,
                     self._profile_page, self._user_profile_page]:
            self._page_stack.addWidget(page)

        self._page_stack.setCurrentWidget(self._welcome_page)
        self._page_stack.setStyleSheet(
            f"QStackedWidget {{ background: {COLORS['BACKGROUND']}; }}"
        )
        body_row.addWidget(self._page_stack, 1)

        # Now-playing side panel — collapsed by default, opened via the
        # always-visible toggle handle docked at the far right edge. Lives
        # in body_row itself (not a floating overlay) so opening it yields
        # page_stack width, same as the sidebar on the other side.
        self._now_playing_panel = NowPlayingSidePanel()
        self._now_playing_panel.cover_clicked.connect(self._on_now_playing_cover_clicked)
        self._now_playing_panel.title_clicked.connect(self._on_now_playing_album_clicked)
        self._now_playing_panel.artist_clicked.connect(self._on_now_playing_artist_clicked)
        self._now_playing_panel.subscribe_clicked.connect(self._on_now_playing_subscribe_clicked)
        # Same slot the physical "next" transport button uses — the queue
        # panel's "next up" row is exactly what play_next() would play.
        self._now_playing_panel.queue_track_clicked.connect(self.player.play_next)
        self._now_playing_panel.clear()
        body_row.addWidget(self._now_playing_panel)

        self._now_playing_toggle = _PanelToggleHandle()
        self._now_playing_toggle.toggled.connect(self._now_playing_panel.set_expanded)
        self._now_playing_toggle.toggled.connect(self._on_now_playing_panel_toggled)
        body_row.addWidget(self._now_playing_toggle)

        main.addWidget(body, 1)

        # Full-window cover viewer overlay (parented to self so it covers everything)
        self._cover_viewer = CoverViewerOverlay(self)
        self._disc_overlay = NowPlayingDiscOverlay(self)
        self._avatar_crop_overlay = AvatarCropOverlay(self)
        self._avatar_crop_overlay.avatar_confirmed.connect(self._on_avatar_cropped)
        self._lyrics_viewer = LyricsViewerOverlay(self)

        # Playback controls
        self._controls = PlaybackControls(self)
        self._controls.artist_clicked.connect(self._on_controls_artist_clicked)
        self._controls.album_clicked.connect(self._on_controls_album_clicked)
        self._controls.cover_clicked.connect(self._open_now_playing_disc)
        self._controls.lyrics_clicked.connect(self._on_lyrics_button_clicked)
        self._lyrics_viewer.line_clicked.connect(self._on_lyrics_seek)
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
        self._artist_page.show_all_albums_clicked.connect(self._show_all_albums_for_artist)
        self._artist_page.track_play_requested.connect(self._on_track_play_requested)
        self._artist_page.track_like_clicked.connect(self._on_artist_random_track_add_clicked)
        self._artist_page.playlist_clicked.connect(self._on_artist_public_playlist_clicked)
        self._artist_all_albums_page.album_clicked.connect(self._on_album_selected)
        self._artist_all_albums_page.back_clicked.connect(
            lambda: self._page_stack.setCurrentWidget(self._artist_page)
        )
        self._album_page.track_play_requested.connect(self._on_track_play_requested)
        self._album_page.artist_name_clicked.connect(self._on_controls_artist_clicked)
        self._album_page.download_album_requested.connect(self._download_album)
        self._album_page.download_track_requested.connect(self._download_track)
        self._album_page.track_like_clicked.connect(self._on_album_track_add_clicked)
        self._album_page.album_like_clicked.connect(self._on_album_like_clicked)
        self._album_page.playlist_cover_edit_requested.connect(self._on_playlist_cover_edit_requested)
        self._album_page.playlist_creator_clicked.connect(self._on_playlist_creator_clicked)
        self._album_page.play_pause_toggle_requested.connect(self.player.toggle_playback)
        self._album_page.cover_clicked.connect(self._cover_viewer.show_for)
        self._search_page.result_selected.connect(self._on_search_result_selected)
        self._all_artists_page.artist_selected.connect(self._navigate_to_artist)
        self._home_page.album_clicked.connect(self._on_album_selected)
        self._home_page.artist_selected.connect(self._navigate_to_artist)
        self._settings_page.logout_clicked.connect(self._on_logout)
        self._settings_page.accent_changed.connect(self._on_accent_changed)
        self._settings_page.theme_changed.connect(self._on_theme_changed)
        self._settings_page.scale_changed.connect(self._on_scale_changed)
        self._settings_page.discord_rpc_toggled.connect(self._on_discord_rpc_toggled)
        self._settings_page.cover_cache_cleared.connect(self._on_cover_cache_cleared)
        self._settings_page.library_cache_cleared.connect(self._on_library_cache_cleared)
        self._settings_page.player_data_cache_cleared.connect(self._on_player_data_cache_cleared)
        self._settings_page.local_library_toggled.connect(self._on_local_library_toggled)
        self._settings_page.open_local_folder_clicked.connect(self._on_open_local_folder_clicked)
        self._settings_page.eq_enabled_toggled.connect(self._on_eq_enabled_toggled)
        self._settings_page.eq_band_changed.connect(self._on_eq_band_changed)
        self._settings_page.eq_preamp_changed.connect(self._on_eq_preamp_changed)
        self._settings_page.eq_reset_clicked.connect(self._on_eq_reset)

        self._profile_page.display_name_save_requested.connect(self._on_display_name_save_requested)
        self._profile_page.avatar_file_selected.connect(self._on_avatar_file_selected)
        self._profile_page.user_search_changed.connect(self._on_user_search_changed)
        self._profile_page.user_result_clicked.connect(self._on_user_result_clicked)
        self._user_profile_page.back_clicked.connect(lambda: self._page_stack.setCurrentWidget(self._profile_page))
        self._user_profile_page.playlist_clicked.connect(self._on_remote_playlist_clicked)
        self._profile_page.playlist_clicked.connect(self._open_playlist)
        self._profile_page.playlist_create_requested.connect(self._on_playlist_create_requested)
        self._profile_page.playlist_rename_requested.connect(self._on_playlist_rename_requested)
        self._profile_page.playlist_delete_requested.connect(self._on_playlist_delete_requested)
        self._profile_page.playlist_visibility_toggled.connect(self._on_playlist_visibility_toggled)

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

        self._logo_text_lbl = _AccentGradientLabel("Memify")
        self._logo_text_lbl.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._logo_text_lbl.setStyleSheet(f"color: {COLORS['PRIMARY']}; background: transparent;")
        logo_layout.addWidget(self._logo_text_lbl)

        logo_widget.mousePressEvent = lambda _e: self._open_home_menu()
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

        # Search-source toggle: app icon (library search, default) <-> drawn
        # YouTube glyph (see _make_youtube_icon_pixmap) — click flips
        # _search_source and re-runs the current query (_toggle_search_source).
        self._search_source = "library"
        self._search_source_icon_app = (
            QIcon(APP_ICON).pixmap(26, 26) if APP_ICON and os.path.exists(APP_ICON) else QPixmap()
        )
        self._search_source_icon_youtube = _make_youtube_icon_pixmap(26)
        self._search_source_btn = QPushButton()
        self._search_source_btn.setFixedSize(26, 26)
        self._search_source_btn.setIconSize(QSize(26, 26))
        self._search_source_btn.setIcon(QIcon(self._search_source_icon_app))
        self._search_source_btn.setFlat(True)
        self._search_source_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._search_source_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_source_btn.setToolTip("Искать на YouTube")
        self._search_source_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 13px; }"
            f"QPushButton:hover {{ background: {COLORS['SURFACE_HOVER']}; }}"
        )
        self._search_source_btn.clicked.connect(self._toggle_search_source)
        sw_layout.addWidget(self._search_source_btn)

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

        self._avatar_btn = _AvatarButton(size=34)
        self._avatar_btn.setToolTip("Профиль")
        if self._account_manager and self._account_manager.active_login:
            self._avatar_btn.set_fallback_letter(self._account_manager.active_login)
        self._avatar_btn.clicked.connect(self._open_profile)
        rp_layout.addWidget(self._avatar_btn)
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
        settings_btn.clicked.connect(self._open_settings)
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

        if self._offline:
            # No account, no server reachable — local library only (already
            # initialized in _apply_loaded_settings), nothing here should
            # touch the network or even a stale server-library cache, since
            # the whole "Библиотека" section is hidden anyway.
            self._sidebar.load_account_content(liked=False, entries=[])
            self._loading = False
            return

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
                self._sidebar.load_account_content(liked=False, entries=[])
            # Fetch the real, current player data right away — don't wait for
            # the library refresh below (_on_library_loaded used to be the
            # only place this ran). That refresh can easily take longer than
            # the 800ms settings-sync debounce that boot's own navigation-
            # restore triggers (_restore_ui_state -> _save_ui_state ->
            # _schedule_settings_sync) — confirmed: that debounce fired
            # first, using whatever was in the *local cache* (self._playlists
            # etc. straight from _on_player_data_loaded(local) above, not
            # yet corrected by the server), and _save_player_data_async
            # resending that stale snapshot silently overwrote newer
            # server-side state — e.g. a playlist cover.png that had been
            # set after this local cache was last written just disappeared,
            # every time, well before the "real" fetch could ever correct it.
            self._fetch_player_data()

        # Step 3: Refresh library from server in background (won't block UI)
        signal = _LibraryLoadSignal(self)
        self._library_load_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._library_load_signals.remove(s)
            except ValueError:
                pass

        signal.finished.connect(self._on_library_loaded)
        signal.finished.connect(_cleanup)

        library_manager = self.library_manager

        def _worker():
            try:
                library_manager.refresh_from_network()
            except Exception as e:
                print(f"Library refresh error: {e}")
            signal.finished.emit()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_library_loaded(self):
        self._loading = False
        # Retry the player warm-up now that real track URLs may be available
        # — on a fresh login (no local library cache yet) the earlier
        # timer-based attempt had nothing to warm the network path up with.
        self._try_warm_up_player()
        # Refresh sidebar now that library is available (artists/albums can be resolved)
        if self._account_manager:
            self._update_sidebar_from_account()
            # Retry navigation if it failed earlier because library was empty
            if self._state_restored and not self._nav_restored:
                self._retry_nav_restore()
        else:
            self._sidebar.load_account_content(liked=False, entries=[])

    def _fetch_player_data(self):
        if self._closing:
            return

        signal = _PlayerDataLoadSignal(self)
        self._player_data_load_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._player_data_load_signals.remove(s)
            except ValueError:
                pass

        signal.finished.connect(self._on_player_data_loaded)
        signal.finished.connect(_cleanup)

        account_manager = self._account_manager

        def _worker():
            try:
                data = account_manager.fetch_player_data()
            except Exception:
                data = None
            signal.finished.emit(data)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_player_data_loaded(self, data: dict | None):
        if data is None:
            # The network fetch failed (timeout/server error) and there was
            # nothing already cached in this session to fall back on — do
            # NOT treat that as "this account has no likes/playlists/etc.",
            # or the next save (a settings change, a reorder, ...) would
            # persist that emptiness right back to the server, permanently
            # wiping the real data over what was just a transient blip.
            # Leave whatever's already loaded (local cache, or still
            # __init__'s empty defaults if there wasn't one) alone and try
            # again shortly.
            if not self._closing:
                QTimer.singleShot(5000, self._fetch_player_data)
            return
        self._liked_tracks = data.get("liked_tracks", []) or []
        self._playlists = [p for p in (data.get("playlists", []) or []) if isinstance(p, dict)]
        self._playlist_subscriptions = [
            s for s in (data.get("playlist_subscriptions", []) or [])
            if isinstance(s, dict) and s.get("owner_login") and s.get("playlist_id")
        ]
        self._subscriptions = data.get("subscriptions", []) or []
        self._album_subscriptions = data.get("album_subscriptions", []) or []
        self._follow_order = data.get("follow_order", []) or []
        # Max-per-day merge, not overwrite: this fires once for the local
        # cache and again for the network fetch, and either can be behind
        # today's not-yet-flushed local accumulation (see
        # _accumulate_listen_time) — seconds only ever grow within a day.
        incoming_listen_stats = data.get("listen_stats") or {}
        if isinstance(incoming_listen_stats, dict):
            merged_listen_stats = dict(self._listen_stats)
            for day, seconds in incoming_listen_stats.items():
                try:
                    seconds = float(seconds or 0)
                except Exception:
                    continue
                if seconds > merged_listen_stats.get(day, 0.0):
                    merged_listen_stats[day] = seconds
            self._listen_stats = merged_listen_stats
        self._apply_synced_settings(data.get("app_settings") or {})
        self._display_name = data.get("display_name") or ""
        self._account_id = data.get("account_id") or ""
        self._apply_own_identity()
        self._apply_own_avatar(data.get("avatar_data"))
        self._profile_page.set_playlists(self._playlists)
        self._profile_page.set_listen_stats(self._listen_stats)
        self._update_sidebar_from_account()
        self._after_track_collections_changed()
        was_ready = self._player_data_ready
        self._player_data_ready = True
        if not self._state_restored:
            QTimer.singleShot(0, self._restore_ui_state)
        # A settings change that landed before this (first) load — e.g. a
        # setting applied while restoring window state — was skipped by the
        # guard in _save_player_data_async rather than sent with whatever
        # self._subscriptions/_liked_tracks/etc. still held from __init__'s
        # empty defaults. Give it one real chance now that they're populated
        # for real, instead of silently dropping that change until the user
        # happens to touch a setting again.
        if not was_ready:
            self._schedule_settings_sync()

    def _apply_own_identity(self):
        login = (self._account_manager.active_login if self._account_manager else "") or ""
        self._avatar_btn.set_fallback_letter(self._display_name or login)
        self._profile_page.set_identity(login, self._account_id, self._display_name)
        if not self._offline and login:
            self._sidebar.set_username(self._display_name or login)

    def _apply_own_avatar(self, avatar_b64: str | None):
        if not avatar_b64:
            self._avatar_btn.set_avatar_pixmap(None)
            self._profile_page.set_avatar_pixmap(None)
            return
        try:
            raw = base64.b64decode(avatar_b64)
            pm = QPixmap()
            if pm.loadFromData(raw) and not pm.isNull():
                self._avatar_btn.set_avatar_pixmap(pm)
                self._profile_page.set_avatar_pixmap(pm)
        except Exception:
            pass

    def _open_profile(self):
        self._page_stack.setCurrentWidget(self._profile_page)

    def _save_account_field_async(self, updates: dict):
        """Queued partial player-data save for account-identity fields
        (avatar/display name/playlists) — UI is already updated
        optimistically, and /player/save only writes whatever keys are
        present, so this never touches liked_tracks/subscriptions/etc."""
        self._enqueue_player_data_save(updates)

    def _enqueue_player_data_save(self, payload: dict):
        if not self._account_manager:
            return
        self._pending_player_data_saves += 1
        self._player_data_save_queue.put(payload)

    def _player_data_save_worker(self):
        """Single persistent worker — every player-data save (whatever
        triggered it) is processed here, one at a time and strictly in the
        order it was enqueued. See the comment where the queue is created."""
        while True:
            payload = self._player_data_save_queue.get()
            try:
                if self._account_manager:
                    self._account_manager.save_player_data(payload)
            except Exception:
                pass
            finally:
                self._pending_player_data_saves = max(0, self._pending_player_data_saves - 1)

    def _on_display_name_save_requested(self, name: str):
        name = (name or "").strip()[:60]
        self._display_name = name
        self._apply_own_identity()
        self._save_account_field_async({"display_name": name})

    def _on_avatar_file_selected(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            QMessageBox.warning(self, "Аватар", "Не удалось загрузить это изображение.")
            return
        # Let the user pick which region becomes the avatar (Discord-style
        # pan/zoom inside a circular frame) instead of silently center-cropping.
        self._avatar_crop_overlay.show_for(pm)

    def _on_avatar_cropped(self, cropped: QPixmap):
        # Already square from AvatarCropOverlay — make_rounded_pixmap here
        # just normalizes to the stored resolution (radius 0: circular
        # clipping happens at display time, see _AvatarButton).
        square = make_rounded_pixmap(cropped, 256, 0)

        buf = QBuffer()
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        square.save(buf, "PNG")
        b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
        buf.close()

        self._avatar_btn.set_avatar_pixmap(square)
        self._profile_page.set_avatar_pixmap(square)
        self._save_account_field_async({"avatar_data": b64, "avatar_filename": "avatar.png"})

    def _on_user_search_changed(self, query: str):
        query = (query or "").strip()
        self._user_search_generation += 1
        gen = self._user_search_generation
        if not self._account_manager or not query:
            self._profile_page.set_search_results([])
            return

        signal = _UserSearchSignal(self)
        self._user_search_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._user_search_signals.remove(s)
            except ValueError:
                pass

        def _on_results(result_gen, items):
            if result_gen == self._user_search_generation:
                self._profile_page.set_search_results(items)

        signal.finished.connect(_on_results)
        signal.finished.connect(_cleanup)

        account_manager = self._account_manager

        def _worker():
            try:
                items = account_manager.search_users(query)
            except Exception:
                items = []
            signal.finished.emit(gen, items)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_user_result_clicked(self, user: dict):
        self._viewed_profile = dict(user)
        self._user_profile_page.set_profile(user)
        self._page_stack.setCurrentWidget(self._user_profile_page)
        self._fetch_full_user_profile(user)

    def _fetch_full_user_profile(self, user: dict):
        if not self._account_manager:
            return
        login = user.get("login", "")
        account_id = user.get("account_id", "")

        signal = _UserProfileSignal(self)
        self._user_profile_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._user_profile_signals.remove(s)
            except ValueError:
                pass

        def _on_result(profile):
            if profile:
                self._viewed_profile = dict(profile)
                self._user_profile_page.set_profile(profile)

        signal.finished.connect(_on_result)
        signal.finished.connect(_cleanup)

        account_manager = self._account_manager

        def _worker():
            try:
                profile = account_manager.get_public_profile(login=login, account_id=account_id)
            except Exception:
                profile = {}
            signal.finished.emit(profile)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_synced_settings(self, app_settings: dict):
        """Merge settings synced from the server (account-wide, via
        app_settings in player data) into local state — covers another
        device having changed theme/volume/last-played-track/etc. since this
        session's own _load_settings() read the local file. Restart-required
        keys (theme/scale) only prompt if they actually differ from what's
        already running here; everything else applies immediately (or, for
        last_view_*/last_played_track, is simply picked up by the normal
        _restore_ui_state() flow that runs right after this)."""
        if not isinstance(app_settings, dict) or not app_settings:
            return

        needs_restart = any(
            key in app_settings and app_settings[key] != self._settings.get(key)
            for key in _RESTART_REQUIRED_SETTINGS_KEYS
        )
        new_accent = app_settings.get("accent_color")
        new_accent2 = app_settings.get("accent_color2") if "accent_color2" in app_settings else self._settings.get("accent_color2")
        accent_changed = (
            (bool(new_accent) and new_accent != self._settings.get("accent_color"))
            or new_accent2 != self._settings.get("accent_color2")
        )

        self._settings.update(
            {k: v for k, v in app_settings.items() if k not in _LOCAL_ONLY_SETTINGS_KEYS}
        )
        try:
            with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        if accent_changed:
            set_accent_color(new_accent or COLORS["PRIMARY"], new_accent2 or None)
            self._settings_page.set_selected_accent(new_accent or COLORS["PRIMARY"], new_accent2 or None)
            self._refresh_accent_widgets()

        if "volume" in app_settings:
            vol = app_settings["volume"]
            self._controls.set_volume(vol)
            self.player.set_volume(vol)
        if "shuffle" in app_settings:
            enabled = bool(app_settings["shuffle"])
            self.player.shuffle_enabled = enabled
            self._controls.set_shuffle(enabled)
        if app_settings.get("repeat") in ("off", "track", "album"):
            self.player.repeat_mode = app_settings["repeat"]
            self._controls.set_repeat(app_settings["repeat"])
        if "discord_rpc" in app_settings:
            enabled = bool(app_settings["discord_rpc"])
            self._settings_page.set_discord_rpc_enabled(enabled)
            if enabled and not self._discord_rpc and not self._discord_connecting:
                QTimer.singleShot(200, self._init_discord_rpc)
            elif not enabled and self._discord_rpc:
                self._dispose_discord_rpc()

        if needs_restart:
            self._offer_restart()

    def _apply_last_played_display(self, last_played: dict):
        """Rebuilds the bottom bar's AND the side panel's "now playing"
        display from a saved last_played_track entry, without starting
        playback. Shared by _restore_ui_state (first attempt, right after
        launch) and _retry_nav_restore (retried once the library's actually
        loaded, for whichever parts needed it and came up empty the first
        time)."""
        track = {
            "title": last_played.get("title", ""),
            "artist_name": last_played.get("artist_name", ""),
            "_real_album_title": last_played.get("album_title", ""),
            "album_id": last_played.get("album_id", ""),
        }
        if last_played.get("_is_youtube"):
            track["_is_youtube"] = True
            track["_youtube_channel_url"] = last_played.get("_youtube_channel_url", "")
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
        self._refresh_now_playing_panel(track, last_played.get("artist_name", ""), last_played.get("album_cover", ""))

    def _restore_ui_state(self):
        if self._state_restored:
            return
        self._state_restored = True

        # Restore bottom bar + side panel track display, and prime the player
        # with the real album/track so the play button can actually resume
        # it (not just show it).
        last_played = self._settings.get("last_played_track")
        if isinstance(last_played, dict) and last_played.get("title"):
            # Priming first: _apply_last_played_display's panel refresh peeks
            # at self.player.current_playing_album/current_track for "Далее
            # в очереди", which only _prime_player_for_resume populates.
            self._prime_player_for_resume(last_played)
            self._apply_last_played_display(last_played)

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

        artist = self._find_artist_any(artist_name)
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
        actually resumes the last-played track instead of doing nothing.

        A track last played from a playlist or "Понравившиеся" gets its
        actual queue rebuilt here too (see _on_track_changed's
        last_played_payload) — resuming straight into the track's real
        album instead would silently swap the queue out from under the
        user, so next/prev would walk through the wrong tracklist after
        a restart."""
        if last_played.get("_is_youtube"):
            self._prime_youtube_resume(last_played)
            return
        track_title = last_played.get("title", "")
        if not track_title:
            return

        playlist_id = last_played.get("_playlist_id", "")
        if playlist_id:
            pl = next((p for p in self._playlists if p.get("id") == playlist_id), None)
            if pl is not None:
                login = (self._account_manager.active_login if self._account_manager else "") or ""
                creator_name = self._display_name or login
                virtual_album = self._build_playlist_virtual_album(pl, creator_name, True, login)
                if self._prime_from_virtual_album(virtual_album, {"artist": ""}, track_title):
                    return
            # Playlist deleted/renamed since, or the track's gone from it —
            # fall through to the real-album resume below instead of
            # resuming into nothing.

        if last_played.get("_is_liked_album"):
            virtual_album, virtual_artist = self._build_liked_virtual_album()
            if self._prime_from_virtual_album(virtual_album, virtual_artist, track_title):
                return

        artist_name = last_played.get("artist_name", "")
        album_title = last_played.get("album_title", "")
        if not artist_name or not album_title:
            return

        artist = self._find_artist_any(artist_name)
        if not artist:
            return

        album = None
        for al in artist.get("albums", []):
            if clean_title(al.get("title", "")) == clean_title(album_title):
                album = al
                break
        if not album:
            return

        self._prime_from_virtual_album(album, artist, track_title)

    def _prime_from_virtual_album(self, album: dict, artist: dict, track_title: str) -> bool:
        idx = None
        for i, t in enumerate(album.get("tracks", [])):
            if clean_title(t.get("title", "")) == clean_title(track_title):
                idx = i
                break
        if idx is None:
            return False

        self.player.set_album(album, artist)
        try:
            pos = self.player.shuffled_indices.index(idx)
        except ValueError:
            pos = idx
        self.player.current_track = pos
        self.player.current_track_idx = idx
        return True

    def _prime_youtube_resume(self, last_played: dict):
        """_prime_player_for_resume's counterpart for a YouTube track — no
        library lookup possible (or needed): rebuilds the same permanent-
        link single-track virtual album/track/artist _play_youtube_result
        builds from a fresh search result, from the saved youtube_url.
        Playback doesn't start here either; pressing play resolves a fresh
        stream at that point, same as picking it from search would."""
        webpage_url = last_played.get("youtube_url", "")
        track_title = last_played.get("title", "")
        if not webpage_url or not track_title:
            return
        artist_name = last_played.get("artist_name", "") or "YouTube"
        channel_url = last_played.get("_youtube_channel_url", "")
        track = {
            "title": track_title, "url": webpage_url, "artist_name": artist_name,
            "_is_youtube": True, "_youtube_channel_url": channel_url,
            "_youtube_thumbnail": last_played.get("album_cover", ""),
        }
        artist = {"artist": artist_name, "_is_youtube": True, "_youtube_channel_url": channel_url}
        album = {
            "title": last_played.get("album_title", "") or track_title,
            "cover": last_played.get("album_cover", ""),
            "tracks": [track], "_is_youtube": True,
        }
        self.player.set_album(album, artist)
        self.player.current_track = 0
        self.player.current_track_idx = 0

    def _retry_nav_restore(self):
        """Retry page navigation after library loaded from network (first attempt failed with empty library)."""
        if self.player.current_playing_album is None:
            last_played = self._settings.get("last_played_track")
            if isinstance(last_played, dict) and last_played.get("title"):
                self._prime_player_for_resume(last_played)
                if self.player.current_playing_album is not None:
                    # Bottom bar/panel text was already showing correctly
                    # (it comes straight from last_played, not the library)
                    # — only "Далее в очереди" needed priming to actually work.
                    self._refresh_now_playing_queue()

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

        artist = self._find_artist_any(artist_name)
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
        playlist_index = {p.get("id"): p for p in self._playlists if isinstance(p, dict) and p.get("id")}
        playlist_sub_index = {
            f"{s.get('owner_login')}::{s.get('playlist_id')}": s for s in self._playlist_subscriptions
        }

        def resolve_album(album_key: str):
            parts = album_key.split("||", 1)
            if len(parts) != 2:
                return None
            artist_name, album_title = parts[0].strip(), parts[1].strip()
            artist_obj = artist_index.get(artist_name)
            if not artist_obj:
                return None
            for al in artist_obj.get("albums", []):
                if (al.get("title") or "").strip() == album_title:
                    return (al, artist_obj)
            return None

        # follow_order is the user's own custom drag-and-drop arrangement
        # (mixing artists, albums and playlists freely), persisted via player
        # data — filter it down to what's still actually subscribed/owned,
        # then append anything missing from it (older data from before this
        # existed, or a playlist created elsewhere, newest last) so nothing
        # silently disappears.
        subscribed_keys = {f"artist::{n}" for n in self._subscriptions}
        subscribed_keys |= {f"album::{k}" for k in self._album_subscriptions}
        subscribed_keys |= {f"playlist::{pid}" for pid in playlist_index}
        subscribed_keys |= {f"playlistsub::{k}" for k in playlist_sub_index}

        ordered_keys = [k for k in self._follow_order if k in subscribed_keys]
        known = set(ordered_keys)
        for name in self._subscriptions:
            k = f"artist::{name}"
            if k not in known:
                ordered_keys.append(k)
                known.add(k)
        for album_key in self._album_subscriptions:
            k = f"album::{album_key}"
            if k not in known:
                ordered_keys.append(k)
                known.add(k)
        for pid in playlist_index:
            k = f"playlist::{pid}"
            if k not in known:
                ordered_keys.append(k)
                known.add(k)
        for sub_key in playlist_sub_index:
            k = f"playlistsub::{sub_key}"
            if k not in known:
                ordered_keys.append(k)
                known.add(k)
        self._follow_order = ordered_keys

        entries = []
        for key in ordered_keys:
            if key.startswith("artist::"):
                artist_obj = artist_index.get(key[len("artist::"):])
                if artist_obj:
                    entries.append(("artist", artist_obj))
            elif key.startswith("album::"):
                resolved = resolve_album(key[len("album::"):])
                if resolved:
                    entries.append(("album", resolved))
            elif key.startswith("playlistsub::"):
                sub = playlist_sub_index.get(key[len("playlistsub::"):])
                if sub:
                    entries.append(("playlist_sub", sub))
            elif key.startswith("playlist::"):
                pl = playlist_index.get(key[len("playlist::"):])
                if pl:
                    entries.append(("playlist", pl))

        has_liked = bool(self._liked_tracks)
        self._sidebar.load_account_content(liked=has_liked, entries=entries)

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
        if not self._player_data_ready:
            # self._liked_tracks/_subscriptions/_album_subscriptions/
            # _follow_order are still whatever __init__ set them to (empty)
            # — _on_player_data_loaded hasn't run yet, from local cache or
            # network. Sending "full" now wouldn't just skip an update, it
            # would overwrite the real local cache *and* server copies with
            # these empty placeholders — which is exactly what happened when
            # a settings-sync (_schedule_settings_sync, 800ms debounce) won
            # the race against the first load: real subscriptions/likes/
            # follow_order, gone, both locally and on the server, replaced
            # by nothing. _on_player_data_loaded flips the guard and retries
            # this once real data is in, so nothing here is lost for good.
            return
        # Always send full current state to avoid partial overwrites on server.
        # "playlists" belongs here too, not just in explicit playlist-mutation
        # calls — _write_local_player_data below *replaces* the whole local
        # cache file with this dict, so any save missing "playlists" (a plain
        # settings/volume change, the listen-stats flush, ...) silently wiped
        # it from the cache. On next launch that empty-playlists snapshot is
        # what _on_player_data_loaded sees first (before the network re-fetch
        # corrects it), and _update_sidebar_from_account — reading an
        # apparently-playlist-less account at that exact moment — strips
        # every "playlist::<id>" entry out of follow_order right then, which
        # is what actually got persisted going forward: every reorder/drag a
        # user ever did to a playlist's sidebar position was thrown away,
        # and it reappeared at the bottom (freshly re-appended, newest-last)
        # from then on.
        full = {
            "liked_tracks": self._liked_tracks,
            "subscriptions": self._subscriptions,
            "album_subscriptions": self._album_subscriptions,
            "follow_order": self._follow_order,
            "playlists": self._playlists,
            "app_settings": {
                k: v for k, v in self._settings.items() if k not in _LOCAL_ONLY_SETTINGS_KEYS
            },
            "listen_stats": self._listen_stats,
        }
        full.update(updates)
        # Write to local cache immediately so next startup sees it right away
        self._write_local_player_data(full)
        self._enqueue_player_data_save(full)

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
            resolve_track_url=self._resolve_track_url_for_player,
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
                            return resolve_media_url(url)
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

    def _setup_mpris(self):
        # Registers Memify as an MPRIS2 player on the session D-Bus (Linux
        # only) so Bluetooth headset play/pause/next/prev buttons — routed
        # via AVRCP into MPRIS by the desktop, not into synthetic media
        # keys — actually reach it. See utils/mpris_service.py.
        if not _MPRIS_AVAILABLE or not _mpris_is_supported():
            return
        try:
            service = MPRISService(self.player, self)
            if service.start():
                self._mpris_service = service
        except Exception:
            self._mpris_service = None

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

    def _toggle_search_source(self):
        self._search_source = "youtube" if self._search_source == "library" else "library"
        is_yt = self._search_source == "youtube"
        self._search_source_btn.setIcon(
            QIcon(self._search_source_icon_youtube if is_yt else self._search_source_icon_app)
        )
        self._search_source_btn.setToolTip("Искать в библиотеке Memify" if is_yt else "Искать на YouTube")
        self._search_bar.setPlaceholderText(
            "Поиск на YouTube..." if is_yt else "Поиск исполнителей, альбомов, треков..."
        )
        if self._search_bar.text().strip():
            self._perform_search()

    def _perform_search(self):
        query = self._search_bar.text().strip()
        if not query:
            return
        self._last_search_query = query
        self._search_generation += 1
        if self._page_stack.currentWidget() != self._search_page:
            self._prev_page_before_search = self._page_stack.currentWidget()
        self._page_stack.setCurrentWidget(self._search_page)
        self._search_page.set_loading(True)
        if self._search_source == "youtube":
            self._perform_youtube_search(query, self._search_generation)
        elif self._search_worker:
            self._search_worker.request.emit(query, self._search_generation)

    def _perform_youtube_search(self, query: str, generation: int):
        if not _search_youtube:
            self._search_page.show_message(
                "Поиск по YouTube недоступен: не установлен модуль yt-dlp."
            )
            return

        signal = _YoutubeSearchSignal(self)
        self._youtube_search_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._youtube_search_signals.remove(s)
            except ValueError:
                pass

        def _on_results(gen, items, error):
            if gen != self._search_generation:
                return
            if error:
                self._search_page.show_message(f"Ошибка поиска на YouTube: {error}")
                return
            results = [SearchResult("youtube", "", youtube_obj=item) for item in items]
            self._search_page.update_results(results)

        signal.finished.connect(_on_results)
        signal.finished.connect(_cleanup)

        def _worker():
            items: list = []
            error = ""
            try:
                items = _search_youtube(query, limit=15)
            except Exception as ex:
                error = str(ex) or ex.__class__.__name__
                print(f"YouTube search failed: {ex}")
            signal.finished.emit(generation, items, error)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_search_results(self, results: list, generation: int):
        if generation != self._search_generation:
            return
        if self._last_search_query and self.local_library_manager.library:
            # Local scan is a small in-memory list — cheap enough to
            # search synchronously on the GUI thread rather than routing
            # through the background SearchWorker (which owns its own,
            # separate LibraryManager instance for the server catalog).
            results = results + self.local_library_manager.fast_search(self._last_search_query)
        if self._last_search_query:
            results = results + self._search_playlists(self._last_search_query)
        self._search_page.update_results(results)

    @staticmethod
    def _playlist_matches_query(playlist: dict, q: str) -> bool:
        if q in (playlist.get("name") or "").lower():
            return True
        # Also match by the name of any artist with a track in the playlist —
        # only meaningful for own playlists, whose track list is held in full
        # locally; a subscription entry is just {owner_login, playlist_id,
        # name, cover_data}, no track data to search without a network fetch.
        for track in playlist.get("tracks", []) or []:
            if isinstance(track, dict) and q in (track.get("artist_name") or "").lower():
                return True
        return False

    def _search_playlists(self, query: str) -> list:
        """Own + "+"-ed playlists aren't part of the shared library, so
        LibraryManager/SearchWorker never see them — matched here instead,
        against the small in-memory lists MusicApp already holds."""
        q = query.strip().lower()
        if not q:
            return []
        my_login = (self._account_manager.active_login if self._account_manager else "") or ""
        out = []
        for pl in self._playlists:
            if not isinstance(pl, dict):
                continue
            if self._playlist_matches_query(pl, q):
                out.append(SearchResult(
                    "playlist", "", playlist_obj=pl, playlist_editable=True,
                    playlist_owner_login=my_login,
                    playlist_cover_pixmap=_decode_base64_pixmap(pl.get("cover_data") or ""),
                ))
        for sub in self._playlist_subscriptions:
            if not isinstance(sub, dict):
                continue
            if q in (sub.get("name") or "").lower():
                out.append(SearchResult(
                    "playlist", "", playlist_obj=sub, playlist_editable=False,
                    playlist_owner_login=sub.get("owner_login", ""),
                    playlist_cover_pixmap=_decode_base64_pixmap(sub.get("cover_data") or ""),
                ))
        return out

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
        elif result.type == "playlist" and result.playlist_obj:
            if result.playlist_editable:
                self._open_playlist(result.playlist_obj.get("id", ""))
            else:
                self._on_playlist_subscription_clicked(result.playlist_obj)
        elif result.type == "youtube" and result.youtube_obj:
            self._play_youtube_result(result.youtube_obj)
            # self._search_bar.clear() above already fired _go_home() (it
            # always does when leaving the search page with an empty query)
            # — for every other branch something afterward lands on a real
            # page (an artist/album/playlist page), but a YouTube pick has
            # no page of its own, so _go_home()'s guess (an arbitrary/stale
            # self._current_artist, once even landing on a blank, never-
            # loaded ArtistPage) was left standing. Just go back to whatever
            # was showing before the search started instead.
            self._page_stack.setCurrentWidget(self._prev_page_before_search or self._welcome_page)

    def _play_youtube_result(self, yt: dict):
        """A picked search result becomes a track whose "url" is the
        permanent youtube.com/watch?v=... link — never the resolved
        googlevideo.com stream, which expires in a few hours. That permanent
        link is what ends up in liked_tracks/playlists if this track gets
        saved (see _build_track_ref); _play_track below is what actually
        resolves a fresh stream right before playback."""
        channel_url = yt.get("channel_url", "")
        track = {
            "title": yt.get("title", ""), "url": yt.get("webpage_url", ""),
            "artist_name": yt.get("uploader", ""), "_is_youtube": True,
            "_youtube_thumbnail": yt.get("thumbnail", ""), "_youtube_channel_url": channel_url,
            # yt-dlp already gave us this at search time — TrackRow shows it
            # straight away and, just as importantly, skips queuing an async
            # probe for it (see AlbumPage.load_album's "if not
            # track.get('duration')" check), which would've tried to read a
            # duration off the permanent youtube.com/watch link — not audio,
            # so that probe could only ever silently fail.
            "duration": int(yt.get("duration") or 0) * 1000,
        }
        artist = {"artist": yt.get("uploader", "") or "YouTube", "_is_youtube": True, "_youtube_channel_url": channel_url}
        album = {
            "title": yt.get("title", ""), "cover": yt.get("thumbnail", ""),
            "tracks": [track], "_is_youtube": True,
        }
        self._play_track(0, album, artist)

    def _resolve_track_url_for_player(self, track: dict, continue_playback):
        """PlayerController's resolve_track_url hook (see set_callbacks
        below) — called before *every* play_track, whichever path reached
        it: a direct click, but also play_next/play_prev/repeat firing
        mid-queue when auto-advancing onto a YouTube track. That's what
        makes a YouTube track mixed in with regular ones "just work" instead
        of only working when clicked directly.

        Non-YouTube tracks (the overwhelming majority of calls) and already-
        resolved YouTube tracks continue synchronously with no extra delay.
        A YouTube track still holding its permanent watch link gets resolved
        to a stream URL in the background and mutated in place (url,
        _permanent_url, _resolved_stream) — same dict instance the player's
        album/tracks list already holds, so no album/track-list rebuilding
        is needed and sibling tracks are untouched."""
        if not track.get("_is_youtube") or track.get("_resolved_stream"):
            continue_playback(track.get("url", ""))
            return
        webpage_url = track.get("url", "")
        if not webpage_url or not _resolve_youtube_stream:
            continue_playback("")
            return

        # De-dupe: play_track can land on this exact still-unresolved track
        # twice before the first resolve finishes (a manual click racing an
        # auto-advance, or rapid skip-skip-back) — queue onto the resolve
        # already in flight instead of firing a second redundant one.
        key = id(track)
        pending = self._youtube_resolve_pending.get(key)
        if pending is not None:
            pending.append(continue_playback)
            return
        self._youtube_resolve_pending[key] = [continue_playback]

        self.setCursor(Qt.CursorShape.BusyCursor)

        signal = _YoutubeStreamSignal(self)
        self._youtube_stream_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._youtube_stream_signals.remove(s)
            except ValueError:
                pass

        def _on_resolved(_url, stream_url, error):
            self.unsetCursor()
            callbacks = self._youtube_resolve_pending.pop(key, [continue_playback])
            if not stream_url:
                detail = f"\n\n{error}" if error else ""
                QMessageBox.warning(self, "YouTube", f"Не удалось получить поток для этого видео.{detail}")
                for cb in callbacks:
                    cb("")
                return
            track["url"] = stream_url
            track["_permanent_url"] = webpage_url
            track["_resolved_stream"] = True
            for cb in callbacks:
                cb(stream_url)

        signal.finished.connect(_on_resolved)
        signal.finished.connect(_cleanup)

        def _worker():
            stream_url = ""
            error = ""
            try:
                stream_url = _resolve_youtube_stream(webpage_url) or ""
            except Exception as ex:
                error = str(ex) or ex.__class__.__name__
                print(f"YouTube stream resolve failed: {ex}")
            signal.finished.emit(webpage_url, stream_url, error)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _go_home(self):
        self._search_bar.clear()
        self._search_page.set_loading(False)
        if self._current_artist:
            self._page_stack.setCurrentWidget(self._artist_page)
        else:
            self._page_stack.setCurrentWidget(self._welcome_page)

    def _open_home_menu(self):
        """Memify logo click — a main-menu page with a fresh random spread
        of albums/artists, reshuffled every time it's opened."""
        self._search_bar.clear()
        self._search_page.set_loading(False)
        self._home_page.load_library(
            self.library_manager.get_library(),
            history=self._settings.get("album_history"),
        )
        self._page_stack.setCurrentWidget(self._home_page)

    def _open_settings(self):
        self._page_stack.setCurrentWidget(self._settings_page)

    def _show_all_artists(self):
        self._all_artists_page.load_artists(self.library_manager.get_library())
        self._page_stack.setCurrentWidget(self._all_artists_page)

    def _navigate_to_artist(self, artist: dict):
        self._current_artist = artist
        self._sidebar.select_artist(artist.get("artist", ""))
        self._artist_page.load_artist(artist)
        # load_artist() rebuilds the random-tracks rows from scratch, so
        # whatever playing/paused state ArtistPage remembered from before
        # this navigation is stale (still the previous artist's) until
        # pushed again here.
        self._artist_page.mark_random_tracks_playing(self._playing_url, self._playing_track)
        self._artist_page.set_random_tracks_paused(not self.player.is_playing())
        artist_name = (artist.get("artist") or "").strip()
        self._artist_page.set_liked(artist_name in self._subscriptions)
        self._page_stack.setCurrentWidget(self._artist_page)
        self._save_ui_state(
            last_view_page="artist",
            last_view_artist=artist_name,
            last_view_album="",
        )

    def _show_all_albums_for_artist(self, artist: dict):
        self._artist_all_albums_page.load_artist(artist)
        self._page_stack.setCurrentWidget(self._artist_all_albums_page)

    def _navigate_to_album(self, album: dict, artist: dict):
        self._current_album = album
        self._current_artist = artist
        display_artist_names = self._display_artist_names(album.get("album_id"), artist.get("artist", ""))
        self._album_page.load_album(
            album, artist, playing_url=self._playing_url, display_artist_names=display_artist_names,
            playing_track=self._playing_track, is_paused=not self.player.is_playing(),
        )
        self._album_page._album_like_btn.setVisible(True)
        album_key = self._album_key(artist.get("artist", ""), album.get("title", ""))
        self._album_page.set_album_liked(album_key in self._album_subscriptions)
        self._album_page.refresh_track_likes(self._all_collection_keys())
        self._sync_play_all_button()
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

    def _on_sidebar_order_changed(self, section: str, new_order: list):
        """User dragged an artist/album row to a new position in the
        sidebar — persist the custom order per section. The server section
        syncs via player data (same as likes/settings); the local section
        is machine-specific (local_library_order references folders that
        only exist on this device) but still flows through the same
        settings sync as everything else in _save_ui_state — harmless,
        since _update_local_sidebar() already filters it down to whatever
        artists are actually found on THIS machine's scan."""
        if section == "local":
            self._save_ui_state(local_library_order=list(new_order))
            return
        self._follow_order = list(new_order)
        self._save_player_data_async({"follow_order": self._follow_order})

    def _on_sidebar_section_collapsed_changed(self, section: str, collapsed: bool):
        if section == "local":
            self._save_ui_state(local_library_collapsed=collapsed)
        else:
            self._save_ui_state(library_collapsed=collapsed)

    def _init_local_library(self):
        """(Re)scan config.LOCAL_MUSIC_DIR and refresh the local-library
        sidebar section + search index. Called on startup (if enabled) and
        right after the settings toggle is switched on."""
        ensure_local_music_dir()
        self.local_library_manager.library = scan_local_library(LOCAL_MUSIC_DIR)
        self.local_library_manager.build_search_index()
        self._update_local_sidebar()

    def _update_local_sidebar(self):
        # NOT .get_library() — that method (core/library.py) always returns
        # the process-wide server-catalog cache when it's populated,
        # regardless of which LibraryManager instance calls it. The local
        # manager's own scanned-from-disk list is only ever in .library.
        library = self.local_library_manager.library
        artist_index = {(a.get("artist") or "").strip(): a for a in library}
        known_keys = {f"artist::{name}" for name in artist_index}

        saved_order = self._settings.get("local_library_order") or []
        ordered_keys = [k for k in saved_order if k in known_keys]
        seen = set(ordered_keys)
        for name in artist_index:
            key = f"artist::{name}"
            if key not in seen:
                ordered_keys.append(key)
                seen.add(key)

        entries = []
        for key in ordered_keys:
            artist_obj = artist_index.get(key[len("artist::"):])
            if artist_obj:
                entries.append(("artist", artist_obj))
        self._sidebar.load_local_content(entries)

    def _on_local_library_toggled(self, enabled: bool):
        self._save_ui_state(local_library_enabled=enabled)
        self._sidebar.set_local_section_visible(enabled)
        if enabled:
            # This is the "rescan on enable" point — folders dropped in
            # while the feature was off get picked up right now.
            self._init_local_library()

    def _on_open_local_folder_clicked(self):
        ensure_local_music_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(LOCAL_MUSIC_DIR))

    def _find_artist_any(self, artist_name: str) -> dict | None:
        """get_artist_by_name against the server library, falling back to
        the local library — used wherever an artist needs to be resolved
        by name outside of a direct click on a sidebar row (session
        restore, clicking the artist name in the now-playing bar), so
        those keep working for locally-playing tracks too."""
        return (
            self.library_manager.get_artist_by_name(artist_name)
            or self.local_library_manager.get_artist_by_name(artist_name)
        )

    def _on_album_selected(self, album: dict, artist: dict):
        self._navigate_to_album(album, artist)

    def _resolve_liked_track(self, lt: dict, library: list) -> dict:
        """Return the real track dict from library if found; otherwise construct one from stored fields."""
        url = lt.get("url", "")
        artist_name = lt.get("artist_name", "")
        album_title = lt.get("album_title", "")
        if lt.get("_is_youtube"):
            # Never in the library — url is the permanent youtube.com/watch
            # link (see _build_track_ref), resolved to a fresh stream only
            # when actually played (see MusicApp._resolve_track_url_for_player).
            return {
                "title": lt.get("track_title") or lt.get("title", ""),
                "url": url,
                "artist_name": artist_name,
                "_is_youtube": True,
                "_youtube_thumbnail": lt.get("_youtube_thumbnail", ""),
                "_youtube_channel_url": lt.get("_youtube_channel_url", ""),
                "_real_album_title": album_title,
                "_real_album_cover": lt.get("_youtube_thumbnail") or lt.get("album_cover", ""),
                "duration": lt.get("duration") or 0,
            }
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
                    full_url = resolve_media_url(t_url)
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

    def _build_liked_virtual_album(self) -> tuple[dict, dict]:
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
        return virtual_album, virtual_artist

    def _on_liked_tracks_selected(self):
        """Show liked tracks as a virtual album in the album page."""
        virtual_album, virtual_artist = self._build_liked_virtual_album()
        self._current_album = virtual_album
        self._current_artist = virtual_artist
        self._album_page.load_album(
            virtual_album, virtual_artist, playing_url=self._playing_url, playing_track=self._playing_track,
            is_paused=not self.player.is_playing(),
        )
        self._album_page._album_like_btn.setVisible(False)
        self._album_page.refresh_track_likes(self._all_collection_keys())
        self._sync_play_all_button()
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

    def _on_cover_cache_cleared(self):
        cover_cache.clear()
        cover_cache.clear_disk_cache()

    def _on_library_cache_cleared(self):
        if self._offline:
            return
        try:
            if os.path.exists(LIBRARY_CACHE_FILE):
                os.remove(LIBRARY_CACHE_FILE)
        except Exception:
            pass
        self.library_manager.clear_cache()
        self._loading = False
        self._load_library_then_player_data()

    def _on_player_data_cache_cleared(self):
        try:
            path = self._player_data_cache_path()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        if self._account_manager and not self._offline:
            self._fetch_player_data()

    def _on_controls_artist_clicked(self, artist_name: str):
        # This slot also serves AlbumPage's own artist-name label (see
        # artist_name_clicked wiring), which can point at a different,
        # merely-browsed artist while a YouTube track keeps playing in the
        # background — only redirect to the channel when the clicked name
        # is actually the one currently loaded in the player. Read the
        # *track* (not current_playing_artist, which is one shared virtual
        # artist for the whole liked-tracks/playlist album, not per-track)
        # so this also works replaying a YouTube track saved to liked
        # tracks/a playlist, not just right after searching for it.
        channel_url = ""
        played_name = ""
        playing_album = self.player.current_playing_album
        if playing_album and self.player.current_track_idx is not None:
            try:
                track = playing_album["tracks"][self.player.current_track_idx]
            except (IndexError, KeyError, TypeError):
                track = None
            if track and track.get("_is_youtube"):
                channel_url = track.get("_youtube_channel_url", "")
                played_name = (track.get("artist_name") or "").strip()
        if channel_url and played_name and played_name == (artist_name or "").strip():
            QDesktopServices.openUrl(QUrl(channel_url))
            return
        artist = self._find_artist_any(artist_name)
        if artist:
            self._navigate_to_artist(artist)

    def _on_controls_album_clicked(self, album_title: str, artist_name: str):
        # Prefer the exact album object that's actually loaded in the player —
        # matching by title text alone can land on the wrong album when two
        # albums under the same artist share the same (cleaned) title.
        playing_album = self.player.current_playing_album
        playing_artist = self.player.current_playing_artist

        # Track title and album label both land here (see PlaybackControls)
        # — a YouTube track has no real album to navigate to, just the
        # single-track virtual one it was wrapped in to play; open the
        # actual video instead of that fake album page.
        if playing_album and self.player.current_track_idx is not None:
            try:
                current_track = playing_album["tracks"][self.player.current_track_idx]
            except (IndexError, KeyError, TypeError):
                current_track = None
            if current_track and current_track.get("_is_youtube"):
                video_url = _track_identity_url(current_track)
                if video_url:
                    QDesktopServices.openUrl(QUrl(video_url))
                return

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

        artist = self._find_artist_any(artist_name)
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

    def _on_now_playing_album_clicked(self):
        # Same title/artist text PlaybackControls already tracks for its
        # own cover/title clicks (see PlaybackControls.update_track_info)
        # — reused so cover/title in the side panel land on exactly the
        # same album (or open the same YouTube video) as clicking the
        # bottom bar would.
        self._on_controls_album_clicked(self._controls.current_album_title, self._controls.current_artist_name)

    def _on_now_playing_artist_clicked(self):
        self._on_controls_artist_clicked(self._controls.current_artist_name)

    def _on_now_playing_cover_clicked(self):
        # Same full-size viewer (with download) AlbumPage's own cover opens
        # — see AlbumPage's cover_clicked, gated the exact same way there:
        # only real albums with an actual cover field, not playlists/liked
        # tracks (those don't carry one "album" cover of their own).
        album = self.player.current_playing_album
        artist = self.player.current_playing_artist
        if album and album.get("cover") and not album.get("_is_liked_album"):
            self._cover_viewer.show_for(album, artist)

    def _on_now_playing_panel_toggled(self, expanded: bool):
        self._save_ui_state(now_playing_panel_open=expanded)

    def _on_lyrics_button_clicked(self):
        # current_artist_name/track_title mirror _on_now_playing_album_clicked's
        # reasoning — populated by PlaybackControls.update_track_info on
        # every real track change AND on session resume (_apply_last_played_
        # display), unlike self._playing_track which resume never sets.
        title = self._controls.track_title.text()
        if not title or title == "Нет трека":
            return
        artist_name = clean_artist_name(self._controls.current_artist_name or "")
        cache_key = (artist_name.lower(), title.lower())
        self._lyrics_viewer_key = cache_key
        cover_rel = self._resolve_playing_cover_rel(self.player.current_playing_album)
        self._lyrics_viewer.show_for(title, artist_name, cover_rel)
        cached = self._lyrics_cache.get(cache_key)
        if cached:
            self._lyrics_viewer.set_lyrics_data(cached["plain"], cached["synced"])
        else:
            self._lyrics_viewer.set_lyrics_loading()

    # ── Playback ──────────────────────────────────────────────────────────────

    def _on_track_play_requested(self, track_idx: int, album: dict, artist: dict):
        self._play_track(track_idx, album, artist)

    def _play_track(self, track_idx: int, album: dict, artist: dict):
        # YouTube-track resolution (permanent watch link -> real stream URL)
        # happens inside PlayerController itself now (see
        # _resolve_track_url_for_player / set_callbacks), so this works the
        # same for every track and every trigger — a direct click here, or
        # play_next/play_prev/repeat advancing onto one mid-queue.
        self.player.set_album(album, artist)
        self.player.play_track(track_idx)

    def _on_like_clicked(self):
        """Despite the name, this now opens the "add to collection" menu for
        the currently playing track (the '+' button in the playback bar)."""
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
        self._show_add_to_collections_menu(track_obj, album, artist, self._controls.like_btn)

    def _sync_like_button(self):
        """Update the '+' button state (accent = in some collection) for the current track."""
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
        in_collection = bool(_track_like_keys(track_obj, _track_identity_url(track_obj)) & self._all_collection_keys())
        self._controls.set_like_state(in_collection, enabled=True)

    # ── Like / playlist helpers ─────────────────────────────────────────────────

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

    def _playlist_track_keys(self, playlist: dict) -> set:
        keys = set()
        for ref in (playlist or {}).get("tracks", []) or []:
            if isinstance(ref, dict):
                keys |= _track_like_keys(ref, ref.get("url", ""))
        return keys

    def _all_collection_keys(self) -> set:
        """Union of liked-tracks keys and every playlist's track keys — used
        to decide whether a track's '+' button should be accent-colored and
        always visible (i.e. it belongs to *something*)."""
        keys = self._liked_urls_set()
        for pl in self._playlists:
            keys |= self._playlist_track_keys(pl)
        return keys

    @staticmethod
    def _album_key(artist_name: str, album_title: str) -> str:
        return f"{(artist_name or '').strip()}||{(album_title or '').strip()}"

    def _is_same_playing_target(self, album: dict, artist: dict) -> bool:
        """Is `album` (with `artist`) the exact same album/playlist/liked-
        tracks view that's currently loaded in the player (playing or
        paused) — used to decide whether the "Слушать" button should
        toggle pause/resume instead of restarting from track 0."""
        playing_album = self.player.current_playing_album
        if not playing_album or not album:
            return False
        is_playlist = bool(album.get("_is_playlist"))
        is_liked = bool(album.get("_is_liked_album"))
        playing_is_playlist = bool(playing_album.get("_is_playlist"))
        playing_is_liked = bool(playing_album.get("_is_liked_album"))
        if is_playlist or playing_is_playlist:
            return (
                is_playlist and playing_is_playlist
                and album.get("_playlist_id", "") == playing_album.get("_playlist_id", "")
            )
        if is_liked or playing_is_liked:
            return is_liked and playing_is_liked
        playing_artist = self.player.current_playing_artist or {}
        this_key = self._album_key((artist or {}).get("artist", ""), album.get("title", ""))
        playing_key = self._album_key(playing_artist.get("artist", ""), playing_album.get("title", ""))
        return this_key == playing_key

    def _sync_play_all_button(self, is_playing: bool | None = None):
        """Keep AlbumPage's "Слушать"/"Играет" button in sync with the
        player — called after loading a new album/playlist page and on every
        playback state change while one might be open.

        `is_playing`, when given, is trusted as-is instead of re-querying
        self.player.is_playing() — libVLC's own is_playing() can briefly
        still say False right after play()/on_track_changed fires (playback
        hasn't actually started yet internally), which used to leave the
        button reading "Слушать" for a track started by clicking it directly
        in the tracklist, even though it really was playing (and clicking
        "Слушать" right then correctly paused it — proof the *target* match
        was right all along, just not this *is-it-playing* read)."""
        album = self._current_album
        is_same = bool(album) and self._is_same_playing_target(album, self._current_artist)
        if is_playing is None:
            is_playing = self.player.is_playing()
        self._album_page.set_playback_state(is_same, is_same and is_playing)

    def _build_track_ref(self, track: dict, album: dict, artist: dict | None = None) -> dict:
        """Same track-reference shape used by liked_tracks and playlists —
        {url, artist_name, album_title, track_title, album_id} — resolved
        against the real library later (see _resolve_liked_track).

        For a YouTube track, "url" here is the *permanent* watch link
        (_track_identity_url), never track["url"] as-is — once playing, that
        field holds a resolved googlevideo.com stream that expires in a few
        hours (see _resolve_track_url_for_player), which would leave a liked track or
        playlist entry silently dead after that."""
        album = album or {}
        is_youtube = bool(track.get("_is_youtube"))
        rel_url = _track_identity_url(track) if is_youtube else track.get("url", "")
        if album.get("_is_liked_album"):
            album_title = track.get("_real_album_title", "")
        else:
            album_title = album.get("title", "") or ""
        artist_name = track.get("artist_name") or ((artist or {}).get("artist", "") if artist else "") or ""
        ref = {
            "url": rel_url or resolve_media_url(rel_url),
            "artist_name": artist_name,
            "album_title": album_title,
            "track_title": track.get("title", "") or "",
            "album_id": str(track.get("album_id") or "").strip(),
        }
        if is_youtube:
            ref["_is_youtube"] = True
            thumb = track.get("_youtube_thumbnail") or album.get("cover", "")
            if thumb:
                ref["_youtube_thumbnail"] = thumb
            channel_url = track.get("_youtube_channel_url", "")
            if channel_url:
                ref["_youtube_channel_url"] = channel_url
            duration = track.get("duration") or 0
            if duration:
                ref["duration"] = duration
        return ref

    def _show_add_to_collections_menu(self, track: dict, album: dict, artist: dict | None, anchor: QWidget):
        if not self._account_manager:
            return
        my_keys = _track_like_keys(track, _track_identity_url(track))

        menu = QMenu(anchor)
        menu.setStyleSheet(_menu_style())
        liked_action = menu.addAction("Понравившиеся треки")
        liked_action.setCheckable(True)
        liked_action.setChecked(bool(my_keys & self._liked_urls_set()))
        liked_action.toggled.connect(lambda checked, t=track, a=album, ar=artist: self._toggle_track_liked(t, a, ar, checked))

        if self._playlists:
            menu.addSeparator()
            for pl in self._playlists:
                act = menu.addAction(pl.get("name") or "Без названия")
                act.setCheckable(True)
                act.setChecked(bool(my_keys & self._playlist_track_keys(pl)))
                act.toggled.connect(
                    lambda checked, t=track, a=album, ar=artist, pid=pl.get("id"):
                        self._toggle_track_in_playlist(t, a, ar, pid, checked)
                )

        menu.addSeparator()
        create_action = menu.addAction("Создать плейлист...")
        create_action.triggered.connect(lambda: self._create_playlist_with_track(track, album, artist))

        menu.exec(QCursor.pos())

    def _toggle_track_liked(self, track: dict, album: dict, artist: dict | None, liked: bool):
        my_keys = _track_like_keys(track, _track_identity_url(track))
        already = bool(my_keys & self._liked_urls_set())
        if liked == already:
            return
        if liked:
            self._liked_tracks.insert(0, self._build_track_ref(track, album, artist))
        else:
            self._liked_tracks = [lt for lt in self._liked_tracks if not _liked_entry_matches(lt, my_keys)]
        self._save_player_data_async({"liked_tracks": self._liked_tracks})
        self._update_sidebar_from_account()
        self._after_track_collections_changed()

    def _toggle_track_in_playlist(self, track: dict, album: dict, artist: dict | None, playlist_id: str, add: bool):
        pl = next((p for p in self._playlists if p.get("id") == playlist_id), None)
        if pl is None:
            return
        my_keys = _track_like_keys(track, _track_identity_url(track))
        tracks = pl.setdefault("tracks", [])
        already = any(_liked_entry_matches(t, my_keys) for t in tracks)
        if add == already:
            return
        if add:
            tracks.insert(0, self._build_track_ref(track, album, artist))
        else:
            pl["tracks"] = [t for t in tracks if not _liked_entry_matches(t, my_keys)]
        self._save_playlists_async()
        self._after_track_collections_changed()
        self._refresh_playlists_ui()

    def _create_playlist_with_track(self, track: dict, album: dict, artist: dict | None):
        if not self._account_manager:
            return
        name, ok = QInputDialog.getText(self, "Новый плейлист", "Название плейлиста:")
        name = (name or "").strip()
        if not ok or not name:
            return
        playlist = {
            "id": uuid.uuid4().hex,
            "name": name[:80],
            "created_at": int(time.time()),
            "public": False,
            "tracks": [self._build_track_ref(track, album, artist)],
        }
        self._playlists.insert(0, playlist)
        self._follow_order.insert(0, f"playlist::{playlist['id']}")
        self._save_playlists_async()
        self._save_follow_order_async()
        self._after_track_collections_changed()
        self._refresh_playlists_ui()

    def _save_playlists_async(self):
        self._save_account_field_async({"playlists": self._playlists})

    def _save_follow_order_async(self):
        # follow_order lives in _save_player_data_async's "full" bundle (see
        # its definition) — any call persists whatever's currently in
        # self._follow_order, so this just makes the intent explicit at call sites.
        self._save_player_data_async({"follow_order": self._follow_order})

    def _refresh_playlists_ui(self):
        self._profile_page.set_playlists(self._playlists)
        self._update_sidebar_from_account()

    def _after_track_collections_changed(self):
        """Re-sync every visible '+' button after liked_tracks/playlists change."""
        self._sync_like_button()
        collection_keys = self._all_collection_keys()
        self._album_page.refresh_track_likes(collection_keys)
        if self._current_album and self._current_album.get("_is_liked_album"):
            self._on_liked_tracks_selected()

    def _build_playlist_virtual_album(self, playlist: dict, creator_name: str, editable: bool, owner_login: str = "") -> dict:
        """Same "virtual album" shape liked-tracks uses (see _resolve_liked_track),
        plus playlist-specific fields AlbumPage/TrackRow key off of: a
        creator name shown where the artist would be, an editable custom
        cover, and (unlike liked tracks) each track's own artist name kept
        so it can show a per-track subtitle."""
        library = self.library_manager.get_library()
        tracks = [
            self._resolve_liked_track(ref, library)
            for ref in (playlist.get("tracks", []) or [])
            if isinstance(ref, dict)
        ]
        return {
            "title": playlist.get("name") or "Плейлист",
            "tracks": tracks,
            "cover": "",
            "_cover_data": playlist.get("cover_data", ""),
            "_is_playlist": True,
            "_playlist_id": playlist.get("id", ""),
            "_playlist_editable": editable,
            "_playlist_creator_name": creator_name,
            "_playlist_owner_login": owner_login,
        }

    def _is_playlist_subscribed(self, owner_login: str, playlist_id: str) -> bool:
        return any(
            s.get("owner_login") == owner_login and s.get("playlist_id") == playlist_id
            for s in self._playlist_subscriptions
        )

    def _show_playlist_album(self, playlist: dict, creator_name: str, editable: bool, owner_login: str = ""):
        virtual_album = self._build_playlist_virtual_album(playlist, creator_name, editable, owner_login)
        virtual_artist = {"artist": ""}
        self._current_album = virtual_album
        self._current_artist = virtual_artist
        self._album_page.load_album(
            virtual_album, virtual_artist, playing_url=self._playing_url, playing_track=self._playing_track,
            is_paused=not self.player.is_playing(),
        )
        if editable:
            # Your own playlist — nothing to "+"/subscribe to, you already have it.
            self._album_page._album_like_btn.setVisible(False)
        else:
            self._album_page._album_like_btn.setVisible(True)
            self._album_page.set_album_liked(self._is_playlist_subscribed(owner_login, playlist.get("id", "")))
        self._album_page.refresh_track_likes(self._all_collection_keys())
        self._sync_play_all_button()
        self._page_stack.setCurrentWidget(self._album_page)

    def _open_playlist(self, playlist_id: str):
        """Show one of the current account's own (editable) playlists."""
        pl = next((p for p in self._playlists if p.get("id") == playlist_id), None)
        if pl is None:
            return
        login = (self._account_manager.active_login if self._account_manager else "") or ""
        creator_name = self._display_name or login
        self._show_playlist_album(pl, creator_name, editable=True, owner_login=login)

    def _on_remote_playlist_clicked(self, playlist: dict):
        """A playlist clicked on someone else's UserProfilePage — always
        read-only, even in the edge case of viewing your own profile via search."""
        my_login = (self._account_manager.active_login if self._account_manager else "") or ""
        viewed_login = self._viewed_profile.get("login", "")
        creator_name = self._viewed_profile.get("display_name") or viewed_login
        editable = bool(my_login) and my_login == viewed_login
        self._show_playlist_album(playlist, creator_name, editable=editable, owner_login=viewed_login)

    def _on_playlist_subscription_clicked(self, sub: dict):
        """A "+"-ed playlist clicked in the sidebar — show the cached snapshot
        immediately, then refresh with the owner's current public data (the
        owner may have renamed/re-covered/edited it, or made it private since)."""
        owner_login = sub.get("owner_login", "")
        playlist_id = sub.get("playlist_id", "")
        if not owner_login or not playlist_id or not self._account_manager:
            return
        snapshot = {
            "id": playlist_id, "name": sub.get("name") or "Плейлист",
            "cover_data": sub.get("cover_data", ""), "tracks": [],
        }
        self._show_playlist_album(snapshot, sub.get("name") or owner_login, editable=False, owner_login=owner_login)

        signal = _UserProfileSignal(self)
        self._user_profile_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._user_profile_signals.remove(s)
            except ValueError:
                pass

        def _on_result(payload):
            profile = payload.get("profile") or {}
            pl = payload.get("playlist")
            if not pl:
                return  # deleted or made private since subscribing — keep showing the snapshot
            # Still viewing the same subscribed playlist? (user may have navigated away already)
            if not (self._current_album or {}).get("_is_playlist"):
                return
            if (self._current_album or {}).get("_playlist_owner_login") != owner_login:
                return
            creator_name = profile.get("display_name") or profile.get("login") or owner_login
            self._show_playlist_album(pl, creator_name, editable=False, owner_login=owner_login)

        signal.finished.connect(_on_result)
        signal.finished.connect(_cleanup)

        account_manager = self._account_manager

        def _worker():
            try:
                profile = account_manager.get_public_profile(login=owner_login)
                pl = None
                if profile:
                    pl = next((p for p in (profile.get("playlists") or []) if p.get("id") == playlist_id), None)
                signal.finished.emit({"profile": profile or {}, "playlist": pl})
            except Exception:
                signal.finished.emit({"profile": {}, "playlist": None})

        threading.Thread(target=_worker, daemon=True).start()

    def _on_artist_public_playlist_clicked(self, summary: dict):
        """A card clicked in the artist page's "Плейлисты, в которых есть
        исполнитель" row — same snapshot-then-refresh flow as
        _on_playlist_subscription_clicked just above, since this can be any
        account's playlist, not necessarily one already subscribed to."""
        owner_login = summary.get("owner_login", "")
        playlist_id = summary.get("id", "")
        if not owner_login or not playlist_id or not self._account_manager:
            return
        snapshot = {
            "id": playlist_id, "name": summary.get("name") or "Плейлист",
            "cover_data": summary.get("cover_data", ""), "tracks": [],
        }
        my_login = (self._account_manager.active_login if self._account_manager else "") or ""
        editable = bool(my_login) and my_login == owner_login
        self._show_playlist_album(snapshot, summary.get("name") or owner_login, editable=editable, owner_login=owner_login)

        signal = _UserProfileSignal(self)
        self._user_profile_signals.append(signal)

        def _cleanup(s=signal):
            try:
                self._user_profile_signals.remove(s)
            except ValueError:
                pass

        def _on_result(payload):
            profile = payload.get("profile") or {}
            pl = payload.get("playlist")
            if not pl:
                return  # deleted or made private since this row loaded — keep showing the snapshot
            if not (self._current_album or {}).get("_is_playlist"):
                return
            if (self._current_album or {}).get("_playlist_owner_login") != owner_login:
                return
            creator_name = profile.get("display_name") or profile.get("login") or owner_login
            self._show_playlist_album(pl, creator_name, editable=editable, owner_login=owner_login)

        signal.finished.connect(_on_result)
        signal.finished.connect(_cleanup)

        account_manager = self._account_manager

        def _worker():
            try:
                profile = account_manager.get_public_profile(login=owner_login)
                pl = None
                if profile:
                    pl = next((p for p in (profile.get("playlists") or []) if p.get("id") == playlist_id), None)
                signal.finished.emit({"profile": profile or {}, "playlist": pl})
            except Exception:
                signal.finished.emit({"profile": {}, "playlist": None})

        threading.Thread(target=_worker, daemon=True).start()

    def _on_playlist_subscribe_clicked(self):
        """The '+' library button on someone else's playlist page."""
        album = self._current_album or {}
        owner_login = album.get("_playlist_owner_login", "")
        playlist_id = album.get("_playlist_id", "")
        if not self._account_manager or not owner_login or not playlist_id:
            return
        sub_key = f"{owner_login}::{playlist_id}"
        order_key = f"playlistsub::{sub_key}"
        already = self._is_playlist_subscribed(owner_login, playlist_id)
        if already:
            self._playlist_subscriptions = [
                s for s in self._playlist_subscriptions
                if not (s.get("owner_login") == owner_login and s.get("playlist_id") == playlist_id)
            ]
            if order_key in self._follow_order:
                self._follow_order.remove(order_key)
            self._album_page.set_album_liked(False)
        else:
            self._playlist_subscriptions.insert(0, {
                "owner_login": owner_login,
                "playlist_id": playlist_id,
                "name": album.get("title", ""),
                "cover_data": album.get("_cover_data", ""),
            })
            self._follow_order.insert(0, order_key)
            self._album_page.set_album_liked(True)
        self._save_player_data_async({
            "playlist_subscriptions": self._playlist_subscriptions, "follow_order": self._follow_order,
        })
        self._update_sidebar_from_account()

    def _on_playlist_creator_clicked(self, owner_login: str):
        if not owner_login or not self._account_manager:
            return
        my_login = (self._account_manager.active_login or "") if self._account_manager else ""
        if owner_login == my_login:
            self._open_profile()
            return
        self._on_user_result_clicked({"login": owner_login})

    def _on_playlist_cover_edit_requested(self):
        album = self._current_album or {}
        if not self._account_manager or not album.get("_is_playlist") or not album.get("_playlist_editable"):
            return
        playlist_id = album.get("_playlist_id", "")
        pl = next((p for p in self._playlists if p.get("id") == playlist_id), None)
        if pl is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите обложку плейлиста", "", "Изображения (*.png *.jpg *.jpeg *.webp)"
        )
        if not path:
            return
        pm = QPixmap(path)
        if pm.isNull():
            QMessageBox.warning(self, "Обложка", "Не удалось загрузить это изображение.")
            return
        square = make_rounded_pixmap(pm, 512, 0)
        buf = QBuffer()
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        square.save(buf, "PNG")
        b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
        buf.close()

        pl["cover_data"] = b64
        self._save_playlists_async()
        self._refresh_playlists_ui()
        self._open_playlist(playlist_id)

    def _on_playlist_create_requested(self):
        if not self._account_manager:
            return
        name, ok = QInputDialog.getText(self, "Новый плейлист", "Название плейлиста:")
        name = (name or "").strip()
        if not ok or not name:
            return
        playlist = {"id": uuid.uuid4().hex, "name": name[:80], "created_at": int(time.time()), "public": False, "tracks": []}
        self._playlists.insert(0, playlist)
        self._follow_order.insert(0, f"playlist::{playlist['id']}")
        self._save_playlists_async()
        self._save_follow_order_async()
        self._refresh_playlists_ui()

    def _on_playlist_rename_requested(self, playlist_id: str, new_name: str):
        pl = next((p for p in self._playlists if p.get("id") == playlist_id), None)
        if pl is None:
            return
        pl["name"] = new_name[:80]
        self._save_playlists_async()
        self._refresh_playlists_ui()

    def _on_playlist_visibility_toggled(self, playlist_id: str, public: bool):
        pl = next((p for p in self._playlists if p.get("id") == playlist_id), None)
        if pl is None:
            return
        pl["public"] = bool(public)
        self._save_playlists_async()
        self._refresh_playlists_ui()

    def _on_playlist_delete_requested(self, playlist_id: str):
        reply = QMessageBox.question(
            self, "Удалить плейлист", "Удалить этот плейлист без возможности восстановления?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._playlists = [p for p in self._playlists if p.get("id") != playlist_id]
        key = f"playlist::{playlist_id}"
        if key in self._follow_order:
            self._follow_order.remove(key)
        self._save_playlists_async()
        self._save_follow_order_async()
        self._refresh_playlists_ui()
        self._after_track_collections_changed()

    def _on_album_track_add_clicked(self, track: dict):
        if not self._account_manager:
            return
        self._show_add_to_collections_menu(track, self._current_album or {}, self._current_artist, self._album_page)

    def _on_artist_random_track_add_clicked(self, track: dict, album: dict, artist: dict):
        if not self._account_manager:
            return
        self._show_add_to_collections_menu(track, album, artist, self._artist_page)

    def _on_album_like_clicked(self):
        if not self._account_manager or not self._current_album:
            return
        if self._current_album.get("_is_playlist"):
            self._on_playlist_subscribe_clicked()
            return
        artist_name = (self._current_artist or {}).get("artist", "")
        album_title = self._current_album.get("title", "")
        key = self._album_key(artist_name, album_title)
        order_key = f"album::{key}"
        if key in self._album_subscriptions:
            self._album_subscriptions.remove(key)
            if order_key in self._follow_order:
                self._follow_order.remove(order_key)
            self._album_page.set_album_liked(False)
        else:
            self._album_subscriptions.append(key)
            self._follow_order.insert(0, order_key)  # newest like goes on top, mixed in with artists
            self._album_page.set_album_liked(True)
        self._save_player_data_async({
            "album_subscriptions": self._album_subscriptions, "follow_order": self._follow_order,
        })
        self._update_sidebar_from_account()

    def _on_artist_like_clicked(self):
        if not self._account_manager or not self._current_artist:
            return
        artist_name = (self._current_artist.get("artist") or "").strip()
        if not artist_name:
            return
        order_key = f"artist::{artist_name}"
        if artist_name in self._subscriptions:
            self._subscriptions.remove(artist_name)
            if order_key in self._follow_order:
                self._follow_order.remove(order_key)
            self._artist_page.set_liked(False)
        else:
            self._subscriptions.append(artist_name)
            self._follow_order.insert(0, order_key)  # newest like goes on top, mixed in with albums
            self._artist_page.set_liked(True)
        self._save_player_data_async({
            "subscriptions": self._subscriptions, "follow_order": self._follow_order,
        })
        self._update_sidebar_from_account()

    def _on_now_playing_subscribe_clicked(self):
        # Deliberately independent of _on_artist_like_clicked/self._current_artist,
        # which is whatever artist page the user happens to be *browsing* —
        # the side panel's subscribe button is always about the *playing*
        # artist (see PlaybackControls.update_track_info), which can be a
        # different one entirely.
        if not self._account_manager:
            return
        artist_name = (self._controls.current_artist_name or "").strip()
        if not artist_name:
            return
        order_key = f"artist::{artist_name}"
        if artist_name in self._subscriptions:
            self._subscriptions.remove(artist_name)
            if order_key in self._follow_order:
                self._follow_order.remove(order_key)
            subscribed = False
        else:
            self._subscriptions.append(artist_name)
            self._follow_order.insert(0, order_key)
            subscribed = True
        self._now_playing_panel.set_subscribed(subscribed)
        # Keep ArtistPage's own button in sync if it's showing this same artist.
        if self._current_artist and (self._current_artist.get("artist") or "").strip() == artist_name:
            self._artist_page.set_liked(subscribed)
        self._save_player_data_async({
            "subscriptions": self._subscriptions, "follow_order": self._follow_order,
        })
        self._update_sidebar_from_account()

    def _on_accent_changed(self, color: str, color2: str):
        # Single source of truth for persistence — avoid a second writer
        # (SettingsPage used to write the file itself, which got clobbered
        # by the next _save_ui_state()/_save_settings() call elsewhere).
        self._save_ui_state(accent_color=color, accent_color2=color2 or None)
        self._refresh_accent_widgets()

    def _refresh_accent_widgets(self):
        """Re-apply the (already-set via set_accent_color()) accent color to
        every widget that has its own color baked in — split out from
        _on_accent_changed so a synced accent from another device (see
        _apply_synced_settings) can reuse it without re-triggering a save."""
        self._controls.apply_accent()
        self._album_page.apply_accent()
        self._artist_page.apply_accent()
        self._cover_viewer.apply_accent()
        self._avatar_crop_overlay.apply_accent()
        self._settings_page.apply_accent()
        self._avatar_btn.apply_accent()
        self._profile_page.apply_accent()
        self._now_playing_panel.apply_accent()
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
            palette.setColor(QPalette.ColorRole.Highlight, QColor(c["PRIMARY"]))
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

    # ── Эквалайзер ───────────────────────────────────────────────────────────

    def _on_eq_enabled_toggled(self, enabled: bool):
        self.player.set_eq_enabled(enabled)
        self._save_ui_state(eq_enabled=enabled)

    def _on_eq_band_changed(self, index: int, db: float):
        self.player.set_eq_band(index, db)
        bands = list(self._settings.get("eq_bands") or [0.0] * len(get_eq_band_frequencies()))
        if index < len(bands):
            bands[index] = db
            self._save_ui_state(eq_bands=bands)

    def _on_eq_preamp_changed(self, db: float):
        self.player.set_eq_preamp(db)
        self._save_ui_state(eq_preamp=db)

    def _on_eq_reset(self):
        self.player.reset_eq()
        self._save_ui_state(eq_preamp=0.0, eq_bands=[0.0] * len(get_eq_band_frequencies()))

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
                resolved_cover = resolve_media_url(cover_rel)
                # Discord can only fetch public http(s) images — a local
                # file:// cover isn't reachable from Discord's servers, so
                # just omit the artwork rather than send an unusable URL.
                rpc_cover = resolved_cover if resolved_cover.startswith("http") else None
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
        self._refresh_now_playing_queue()

    def _on_repeat(self):
        mode = self.player.toggle_repeat()
        self._controls.set_repeat(mode)
        self._save_ui_state(repeat=mode)
        self._refresh_now_playing_queue()

    def _on_seek(self):
        value = self._controls.progress_slider.value()
        self.player.seek_position(value / 10.0)
        # Seeking doesn't fire track_changed/playback_state_changed, so
        # Discord's activity timestamps would otherwise stay stuck at the
        # pre-seek position/duration until the next play/pause/track event.
        self._schedule_discord_presence_refresh(90)
        self._schedule_discord_presence_refresh(650)

    def _on_lyrics_seek(self, ms: int):
        self.player.seek_to_ms(ms)
        self._schedule_discord_presence_refresh(90)
        self._schedule_discord_presence_refresh(650)

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
        if self._mpris_service:
            self._mpris_service.update_track(track, artist, album)
        self._sync_like_button()
        self._schedule_discord_presence_refresh(90)
        self._schedule_discord_presence_refresh(650)
        # Highlight playing track everywhere. For a YouTube track this must
        # be the permanent link (_track_identity_url), not track["url"] —
        # AlbumPage.load_album takes a *copy* of each track for the liked-
        # tracks virtual album specifically (to strip artist_name), so the
        # in-place url/_permanent_url/_resolved_stream mutation this track
        # dict just got (see _resolve_track_url_for_player) never reaches
        # that row's copy; matching on the permanent link instead works
        # regardless, since _resolve_liked_track never had anything but the
        # permanent link to give that copy in the first place.
        self._playing_url = _track_identity_url(track) or ""
        self._playing_track = track
        self._album_page.mark_playing_url(self._playing_url, track)
        self._album_page.set_paused(False)  # a freshly-started track is always playing, never paused
        self._artist_page.mark_random_tracks_playing(self._playing_url, track)
        self._artist_page.set_random_tracks_paused(False)
        self._sync_play_all_button(True)
        self._search_page.refresh_playing(self._playing_url, track)
        # Save track info for next-launch restore
        artist_name = track.get("artist_name") or (artist or {}).get("artist", "") or ""
        album_title = track.get("_real_album_title") or (album or {}).get("title", "") or ""
        album_id = str(track.get("album_id") or (album or {}).get("album_id") or "").strip()
        # Same "real per-track cover for a virtual album" resolution used to
        # show the bottom bar's cover live (see _resolve_playing_cover_rel)
        # — reused here so the *saved* last_played_track.album_cover (read
        # back on next launch, before anything is actually playing) is
        # correct too. This used to only check _is_liked_album, not
        # _is_playlist, so a track last played from a playlist always saved
        # an empty cover — showing no artwork at startup until playback was
        # started again (which re-resolved it correctly via this same logic).
        cover = self._resolve_playing_cover_rel(album)
        self._refresh_now_playing_panel(track, artist_name, cover)
        last_played_payload = {
            "title": track.get("title", ""),
            "artist_name": artist_name,
            "album_title": album_title,
            "album_cover": cover,
            "album_id": album_id,
        }
        is_youtube = bool(track.get("_is_youtube"))
        if is_youtube:
            # No library entry to resolve back to on next launch (see
            # _prime_player_for_resume's YouTube branch) — the permanent
            # watch link is what makes resuming possible at all.
            last_played_payload["_is_youtube"] = True
            last_played_payload["youtube_url"] = _track_identity_url(track)
            channel_url = track.get("_youtube_channel_url", "")
            if channel_url:
                last_played_payload["_youtube_channel_url"] = channel_url
        elif album and album.get("_is_playlist"):
            # album_title/album_id above are already the track's *real*
            # album (see _resolve_playing_cover_rel's comment) — that's
            # right for album_history's "Продолжить слушать", but resuming
            # into that real album instead of the playlist would silently
            # swap the queue out from under the user (next/prev would walk
            # through the wrong tracklist). Recorded separately so
            # _prime_player_for_resume can rebuild the actual playlist.
            last_played_payload["_playlist_id"] = album.get("_playlist_id", "")
        elif album and album.get("_is_liked_album"):
            last_played_payload["_is_liked_album"] = True
        self._save_ui_state(
            last_played_track=last_played_payload,
            # A YouTube "album" has nothing a later library lookup could
            # ever resolve — skip it rather than leave a dead entry in the
            # home page's "Продолжить слушать" row.
            album_history=(
                list(self._settings.get("album_history") or []) if is_youtube
                else self._record_album_history(artist_name, album_title, album_id, cover)
            ),
        )

    def _record_album_history(self, artist_name: str, album_title: str, album_id: str, cover: str) -> list:
        """Push (artist, album) onto the "recently listened" list the home
        page's "Продолжить слушать" row reads from — most-recent-first,
        re-listening moves an album back to the front instead of duplicating
        it, capped so it doesn't grow forever."""
        if not album_title:
            return list(self._settings.get("album_history") or [])
        key = self._album_key(artist_name, album_title)
        history = [
            h for h in (self._settings.get("album_history") or [])
            if isinstance(h, dict) and self._album_key(h.get("artist_name", ""), h.get("album_title", "")) != key
        ]
        history.insert(0, {
            "artist_name": artist_name,
            "album_title": album_title,
            "album_id": album_id,
            "album_cover": cover,
        })
        return history[:20]

    def _resolve_playing_cover_rel(self, album: dict) -> str:
        """Cover path for the currently playing album — prefers the real
        album cover from the track when playing a virtual album (liked
        tracks or a playlist), since those don't have one cover of their own."""
        cover_rel = (album or {}).get("cover", "")
        is_virtual = bool(album) and (album.get("_is_liked_album") or album.get("_is_playlist"))
        if is_virtual and self.player.current_track_idx is not None:
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
        cover_url = resolve_media_url(cover_rel)
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

    def _resolve_track_cover_rel(self, album: dict, track: dict) -> str:
        """Same "real per-track cover for a virtual album" resolution as
        _resolve_playing_cover_rel, but for an arbitrary track (the queue
        panel's "next up" track isn't necessarily the currently *playing*
        one, which is all _resolve_playing_cover_rel can look at)."""
        cover_rel = (album or {}).get("cover", "")
        is_virtual = bool(album) and (album.get("_is_liked_album") or album.get("_is_playlist"))
        if is_virtual:
            real = (track or {}).get("_real_album_cover", "")
            if real:
                cover_rel = real
        return cover_rel

    def _refresh_now_playing_panel(self, track: dict, artist_name: str, cover_rel: str):
        """Pushes the just-started track into the side panel — called from
        _on_track_changed, right alongside the same bottom-bar update."""
        panel = self._now_playing_panel
        title = clean_title(track.get("title", "")) or "Неизвестно"
        panel.set_track(title, clean_artist_name(artist_name) if artist_name else "")
        self._load_now_playing_cover(cover_rel)

        real_artist = self._find_artist_any(artist_name) if artist_name else None
        clean_artist = clean_artist_name(artist_name) if artist_name else ""
        panel.set_about_artist(clean_artist, subscribable=bool(real_artist))
        if artist_name:
            panel.set_subscribed(artist_name in self._subscriptions)
        if real_artist and real_artist.get("cover"):
            self._load_now_playing_artist_avatar(resolve_media_url(real_artist["cover"]))
        else:
            panel.set_artist_avatar_pixmap(None)

        self._refresh_now_playing_bio(clean_artist)
        self._refresh_now_playing_queue()
        self._refresh_now_playing_lyrics(track, title, artist_name)

    def _refresh_now_playing_bio(self, clean_artist: str):
        """Fetches the "Об исполнителе" bio for the artist just shown in
        the panel (see set_about_artist above) — shares a process-lifetime
        cache with ArtistPage's own bio lookup (_lookup_artist_bio), so
        this is usually instant once either place has already loaded that
        artist once this session."""
        panel = self._now_playing_panel
        self._now_playing_bio_request_id += 1
        request_id = self._now_playing_bio_request_id

        def on_result(bio: str, _rid=request_id):
            if _rid != self._now_playing_bio_request_id:
                return  # a newer track started playing before this returned
            panel.set_bio(bio)

        _lookup_artist_bio(clean_artist, on_result, self._now_playing_bio_runners)

    def _refresh_now_playing_lyrics(self, track: dict, title: str, artist_name: str):
        """Pre-fetches lyrics (plain + synced, when lrclib has an LRC
        version) for the track that just started, cached in memory per
        (artist, title) for the rest of the session — so by the time the
        user actually opens LyricsViewerOverlay (the button next to the
        volume slider, see _on_lyrics_button_clicked), it's usually already
        there instead of starting the fetch only on click. title is already
        clean_title()'d by the caller (_refresh_now_playing_panel);
        artist_name is not."""
        clean_artist = clean_artist_name(artist_name) if artist_name else ""
        if not title or not clean_artist:
            return

        cache_key = (clean_artist.lower(), title.lower())
        if cache_key in self._lyrics_cache:
            return

        self._lyrics_request_id += 1
        request_id = self._lyrics_request_id
        album_title = clean_title(track.get("_real_album_title", "")) if track.get("_real_album_title") else ""
        duration_sec = int((track.get("duration") or 0) / 1000)

        def on_finished(plain: str, synced: list, _rid=request_id, _key=cache_key):
            if _rid != self._lyrics_request_id:
                return  # a newer track started playing before this returned
            self._lyrics_cache[_key] = {"plain": plain, "synced": synced}
            # Only live-update the viewer if it's open AND still showing
            # this exact track — it doesn't follow later track changes on
            # its own (see _on_lyrics_button_clicked).
            if self._lyrics_viewer.isVisible() and self._lyrics_viewer_key == _key:
                self._lyrics_viewer.set_lyrics_data(plain, synced)

        _start_lyrics_worker(clean_artist, title, album_title, duration_sec, on_finished, self._lyrics_runners)

    def _next_queue_track(self) -> tuple[dict | None, dict | None]:
        """(track, album) that will start once the current one ends, given
        the live shuffle/repeat state — mirrors PlayerController.play_next's
        own branching (core/player_vlc.py) without actually advancing
        anything. Crossing into the *next album* (the on_album_finished
        hand-off) isn't resolved here — that hand-off picks the next
        artist/album via a whole separate lookup or a random pick keyed off
        subscriptions, not a simple queue peek, so it isn't shown."""
        p = self.player
        album = p.current_playing_album
        if not album or p.current_track is None or not p.shuffled_indices:
            return None, None
        tracks = album.get("tracks", []) or []
        if p.repeat_mode == "track":
            idx = p.current_track_idx
        else:
            next_pos = p.current_track + 1
            if next_pos < len(p.shuffled_indices):
                idx = p.shuffled_indices[next_pos]
            elif p.repeat_mode == "album":
                idx = p.shuffled_indices[0]
            else:
                idx = None
        if idx is None:
            return None, None
        try:
            return tracks[idx], album
        except (IndexError, TypeError):
            return None, None

    def _refresh_now_playing_queue(self):
        track, album = self._next_queue_track()
        panel = self._now_playing_panel
        if not track:
            panel.set_queue_track(None, None)
            return
        title = clean_title(track.get("title", "")) or "Неизвестно"
        artist_name = track.get("artist_name") or (self.player.current_playing_artist or {}).get("artist", "") or ""
        panel.set_queue_track(title, clean_artist_name(artist_name) if artist_name else "")
        self._load_now_playing_queue_cover(self._resolve_track_cover_rel(album, track))

    def _load_now_playing_cover(self, cover_rel: str):
        if not cover_rel or (os.path.isabs(cover_rel) and os.path.exists(cover_rel)):
            self._now_playing_panel.set_cover_pixmap(None)
            return
        cover_url = resolve_media_url(cover_rel)
        key = cache_key(cover_url, 180, 8)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._now_playing_panel.set_cover_pixmap(cached)
            return

        def on_loaded(url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(url, size, radius), pm)
                    self._now_playing_panel.set_cover_pixmap(pm)
            except Exception:
                pass

        _start_image_loader([cover_url], 180, 8, on_loaded, self._img_runners)

    def _load_now_playing_artist_avatar(self, url: str):
        key = cache_key(url, 72, 36)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._now_playing_panel.set_artist_avatar_pixmap(cached)
            return

        def on_loaded(loaded_url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(loaded_url, size, radius), pm)
                    self._now_playing_panel.set_artist_avatar_pixmap(pm)
            except Exception:
                pass

        _start_image_loader([url], 72, 36, on_loaded, self._img_runners)

    def _load_now_playing_queue_cover(self, cover_rel: str):
        if not cover_rel or (os.path.isabs(cover_rel) and os.path.exists(cover_rel)):
            self._now_playing_panel.set_queue_cover_pixmap(None)
            return
        cover_url = resolve_media_url(cover_rel)
        key = cache_key(cover_url, 44, 4)
        cached = cover_cache.get(key)
        if cached and not cached.isNull():
            self._now_playing_panel.set_queue_cover_pixmap(cached)
            return

        def on_loaded(url, img, size, radius):
            try:
                pm = QPixmap.fromImage(img) if img else QPixmap()
                if not pm.isNull():
                    cover_cache.set(cache_key(url, size, radius), pm)
                    self._now_playing_panel.set_queue_cover_pixmap(pm)
            except Exception:
                pass

        _start_image_loader([cover_url], 44, 4, on_loaded, self._img_runners)

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
        self._album_page.set_paused(not is_playing)
        self._artist_page.set_random_tracks_paused(not is_playing)
        self._sync_play_all_button(is_playing)
        self._schedule_discord_presence_refresh(90)
        self._schedule_discord_presence_refresh(650)
        if self._mpris_service:
            self._mpris_service.update_playback_state(is_playing)

    def _on_position_changed(self):
        pos = self.player.get_current_position()
        dur = self.player.get_duration()
        self._controls.update_position(pos, dur)
        if self._mpris_service:
            self._mpris_service.update_position(pos, dur)
        if self._lyrics_viewer.isVisible():
            self._lyrics_viewer.set_position(pos)
        self._accumulate_listen_time()

    def _accumulate_listen_time(self):
        """Called on every ~500ms player timer tick (see PlayerController.timer
        in core/player_vlc.py — it keeps running while paused, not just while
        playing), so real elapsed wall-clock time is used rather than assuming
        a fixed tick length, and progress is only credited while actually
        playing."""
        now = time.monotonic()
        last_tick = self._listen_last_tick
        self._listen_last_tick = now
        if last_tick is None or not self.player.is_playing():
            return
        elapsed = now - last_tick
        if elapsed <= 0 or elapsed > 2.0:
            return
        today = date.today().isoformat()
        self._listen_stats[today] = round(self._listen_stats.get(today, 0.0) + elapsed, 1)
        self._listen_stats_dirty = True

    def _flush_listen_stats(self):
        if not self._listen_stats_dirty:
            return
        self._listen_stats_dirty = False
        # Deliberately NOT _save_player_data_async({}) — that resends this
        # window's whole in-memory snapshot (liked_tracks, subscriptions,
        # follow_order, app_settings), and since this timer fires every 20s
        # purely from *playback continuing* (not from the user touching any
        # of those), a snapshot that's gone stale relative to the server
        # (e.g. a track liked from another device/session meanwhile) gets
        # silently overwritten back out — confirmed: a like made elsewhere
        # while this window sits open and playing gets wiped within one
        # flush cycle. A plain per-key save only ever touches listen_stats.
        self._enqueue_player_data_save({"listen_stats": self._listen_stats})
        self._profile_page.set_listen_stats(self._listen_stats)

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
            url = resolve_media_url(rel)
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
        url = resolve_media_url(rel)
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
        if getattr(self, "_lyrics_viewer", None) and self._lyrics_viewer.isVisible():
            self._lyrics_viewer.setGeometry(self.rect())

    # ── Close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        # Flush any not-yet-synced listening seconds before player.stop()
        # below ends the position-tick timer that accumulates them, and
        # before _closing (next line) makes _schedule_settings_sync()
        # a no-op that would otherwise silently drop this save.
        if self._listen_stats_dirty:
            self._listen_stats_dirty = False
            self._enqueue_player_data_save({"listen_stats": self._listen_stats})
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
        # Library/player-data *loads* run on daemon threads (see
        # _LibraryLoadSignal) — safe to just let those go, _closing (set
        # above) stops their callbacks from touching the tearing-down UI.
        #
        # Player-data *saves* (likes, playlists, follow_order, settings —
        # anything that goes through the save queue) run on a daemon worker
        # thread, but unlike loads they're not disposable: a daemon thread is
        # killed outright the instant the process exits, mid-HTTP-request if
        # that's where it is. Closing the app right after unliking something
        # used to lose that unlike silently — it never reached the server,
        # even though the local cache and this session's own UI already
        # showed it gone. Give any in-flight/queued save a brief grace period
        # to actually finish first.
        deadline = time.monotonic() + 2.0
        while self._pending_player_data_saves and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)

        self._album_page._stop_duration_loader()
        self._artist_page._stop_random_duration_loader()
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
        _stop_runners_and_wait(self._lyrics_runners)
        _stop_runners_and_wait(self._artist_page._runners)
        _stop_runners_and_wait(self._artist_page._album_row_runners)
        _stop_runners_and_wait(self._artist_page._bio_runners)
        _stop_runners_and_wait(self._artist_page._random_cover_runners)
        _stop_runners_and_wait(self._artist_page._playlist_runners)
        _stop_runners_and_wait(self._artist_all_albums_page._album_grid._runners)
        _stop_runners_and_wait(self._now_playing_bio_runners)
        _stop_runners_and_wait(self._album_page._runners)
        _stop_runners_and_wait(self._cover_viewer._runners)
        _stop_runners_and_wait(self._disc_overlay._runners)
        _stop_runners_and_wait(self._lyrics_viewer._runners)
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
        if self._mpris_service:
            try:
                self._mpris_service.stop()
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
