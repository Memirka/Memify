import os
import hashlib

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QLinearGradient, QFont
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget, QPushButton, QMessageBox

from config import APP_ICON
from utils.image_utils import load_pixmap_from_url, make_rounded_pixmap
from .styles import ACCENT_COLOR_PRESETS, COLORS

SCROLL_AREA_STYLE = "QScrollArea { border: none; background: transparent; }"


class ServerMonitorWindow(QWidget):
    def __init__(self, fetch_users, request_close=None):
        super().__init__()
        self._fetch_users = fetch_users
        self._request_close = request_close
        self._last_signature = None

        self.setWindowTitle("Memify Server")
        self.resize(920, 620)
        self.setMinimumSize(720, 480)
        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))

        self.setStyleSheet(f"background-color: {COLORS['BACKGROUND']};")
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(3000)
        self._refresh_timer.timeout.connect(self.refresh_users)
        self._refresh_timer.start()
        QTimer.singleShot(0, self.refresh_users)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Активные пользователи")
        title.setStyleSheet("color: #FFFFFF; font: 18pt 'Segoe UI'; font-weight: 800;")
        header.addWidget(title, 1, Qt.AlignmentFlag.AlignLeft)

        self._count_label = QLabel("Онлайн: 0")
        self._count_label.setStyleSheet("color: #B3B3B3; font: 10pt 'Segoe UI';")
        header.addWidget(self._count_label, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(SCROLL_AREA_STYLE)
        self._scroll.viewport().setStyleSheet(f"background-color: {COLORS['BACKGROUND']};")

        self._list_container = QWidget()
        self._list_container.setObjectName("onlineList")
        self._list_container.setStyleSheet(f"QWidget#onlineList {{ background-color: {COLORS['BACKGROUND']}; }}")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(8, 8, 8, 8)
        self._list_layout.setSpacing(10)

        self._scroll.setWidget(self._list_container)
        root.addWidget(self._scroll, 1)

    def refresh_users(self):
        try:
            users = list(self._fetch_users() or [])
        except Exception:
            users = []

        self._count_label.setText(f"Онлайн: {len(users)}")

        signature = [
            (
                (u.get("login") or ""),
                (u.get("ip") or ""),
                (u.get("avatar_path") or ""),
                (u.get("display_name") or ""),
                ((u.get("now_playing") or {}).get("title") or ""),
                ((u.get("now_playing") or {}).get("artist") or ""),
                ((u.get("now_playing") or {}).get("cover_path") or ""),
                ((u.get("now_playing") or {}).get("cover_url") or ""),
            )
            for u in users
        ]
        if signature == self._last_signature:
            return
        self._last_signature = signature

        self._clear_layout(self._list_layout)
        if not users:
            empty = QLabel("Сейчас никто не использует приложение.")
            empty.setStyleSheet("color: #B3B3B3; font: 11pt 'Segoe UI'; padding: 8px;")
            self._list_layout.addWidget(empty)
            self._list_layout.addStretch(1)
            return

        for user in users:
            self._list_layout.addWidget(self._build_user_row(user))
        self._list_layout.addStretch(1)

    def _build_user_row(self, user: dict) -> QWidget:
        login = str(user.get("login") or "").strip()
        ip_value = str(user.get("ip") or "").strip() or "-"
        avatar_path = str(user.get("avatar_path") or "").strip()

        row = QFrame()
        row.setObjectName("onlineRow")
        row.setStyleSheet(
            f"""
            QFrame#onlineRow {{
                background-color: {COLORS['SURFACE']};
                border: 1px solid {COLORS['BORDER']};
                border-radius: 12px;
            }}
            QFrame#onlineRow:hover {{
                background-color: {COLORS['SURFACE_HOVER']};
                border-color: {COLORS['PRIMARY']};
            }}
            """
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)

        avatar = QLabel()
        avatar.setFixedSize(52, 52)
        avatar.setStyleSheet(f"background-color: {COLORS['COVER_BG']}; border-radius: 26px;")
        pixmap = self._load_avatar_pixmap(login, avatar_path, size=52)
        if pixmap is not None and not pixmap.isNull():
            avatar.setPixmap(pixmap)
        layout.addWidget(avatar, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        login_lbl = QLabel(login or "Unknown")
        login_lbl.setStyleSheet("color: #FFFFFF; font: 12pt 'Segoe UI'; font-weight: 700;")
        text_col.addWidget(login_lbl)

        ip_lbl = QLabel(f"IP: {ip_value}")
        ip_lbl.setStyleSheet("color: #B3B3B3; font: 9.5pt 'Segoe UI';")
        text_col.addWidget(ip_lbl)

        layout.addLayout(text_col, 1)

        now_playing_widget = self._build_now_playing_widget(user.get("now_playing"))
        if now_playing_widget is not None:
            layout.addWidget(now_playing_widget, 0, Qt.AlignmentFlag.AlignVCenter)

        if self._request_close:
            btn = QPushButton("Закрыть")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(32)
            btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {COLORS['SURFACE_LIGHT']};
                    color: {COLORS['TEXT_PRIMARY']};
                    border: 1px solid {COLORS['BORDER']};
                    border-radius: 8px;
                    padding: 0 10px;
                    font: 10pt 'Segoe UI';
                }}
                QPushButton:hover {{
                    border-color: {COLORS['PRIMARY']};
                    color: {COLORS['PRIMARY']};
                }}
                QPushButton:pressed {{
                    background-color: {COLORS['SURFACE_HOVER']};
                    border-color: {COLORS['PRIMARY']};
                }}
                """
            )

            def _on_close():
                if not login or not ip_value or ip_value == "-":
                    return
                reply = QMessageBox.question(
                    self,
                    "Закрыть клиент",
                    f"Закрыть клиент {login} ({ip_value})?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                try:
                    self._request_close(login, ip_value)
                except Exception:
                    pass

            btn.clicked.connect(_on_close)
            layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_now_playing_widget(self, payload: dict | None) -> QWidget | None:
        if not isinstance(payload, dict):
            return None
        title = str(payload.get("title") or "").strip()
        artist = str(payload.get("artist") or "").strip()
        if not title or not artist:
            return None

        cover_path = str(payload.get("cover_path") or "").strip()
        cover_url = str(payload.get("cover_url") or "").strip()

        box = QFrame()
        box.setObjectName("nowPlaying")
        box.setStyleSheet(
            f"""
            QFrame#nowPlaying {{
                background-color: {COLORS['SURFACE_LIGHT']};
                border: 1px solid {COLORS['BORDER']};
                border-radius: 10px;
            }}
            """
        )
        layout = QHBoxLayout(box)
        layout.setContentsMargins(8, 6, 10, 6)
        layout.setSpacing(8)

        cover = QLabel()
        cover.setFixedSize(44, 44)
        cover.setStyleSheet(f"background-color: {COLORS['COVER_BG']}; border-radius: 6px;")
        pixmap = self._load_cover_pixmap(cover_path, cover_url, size=44)
        if pixmap is not None and not pixmap.isNull():
            cover.setPixmap(pixmap)
        layout.addWidget(cover, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        title_lbl = QLabel()
        title_lbl.setStyleSheet("color: #FFFFFF; font: 10.5pt 'Segoe UI'; font-weight: 600;")
        self._set_elided_text(title_lbl, title, 220)
        text_col.addWidget(title_lbl)

        artist_lbl = QLabel()
        artist_lbl.setStyleSheet("color: #B3B3B3; font: 9pt 'Segoe UI';")
        self._set_elided_text(artist_lbl, artist, 220)
        text_col.addWidget(artist_lbl)

        layout.addLayout(text_col, 1)
        return box

    def _set_elided_text(self, label: QLabel, text: str, max_width: int) -> None:
        try:
            label.setFixedWidth(int(max_width))
            fm = label.fontMetrics()
            label.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, int(max_width)))
        except Exception:
            label.setText(text)

    def _load_cover_pixmap(self, cover_path: str, cover_url: str, size: int = 44) -> QPixmap | None:
        pixmap = None
        if cover_path and os.path.isfile(cover_path):
            pixmap = QPixmap(cover_path)
        elif cover_url and cover_url.startswith("http"):
            pixmap = load_pixmap_from_url(cover_url)
        if pixmap is None or pixmap.isNull():
            return None
        return make_rounded_pixmap(pixmap, size, 6)

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _avatar_colors_for_login(self, login: str) -> tuple[str, str]:
        try:
            digest = hashlib.sha256((login or "").encode("utf-8")).digest()
            idx = int.from_bytes(digest[:2], "big")
        except Exception:
            idx = 0

        palette = ACCENT_COLOR_PRESETS or []
        if palette:
            colors = palette[idx % len(palette)].get("colors", []) or []
            if len(colors) == 1:
                colors.append(colors[0])
            if len(colors) >= 2:
                return colors[0], colors[1]

        return COLORS["PRIMARY"], COLORS["PRIMARY_HOVER"]

    def _build_avatar_pixmap(self, login: str, size: int = 52) -> QPixmap:
        start_hex, end_hex = self._avatar_colors_for_login(login)
        start_color = QColor(start_hex)
        end_color = QColor(end_hex)

        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0, start_color)
        gradient.setColorAt(1, end_color)
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, size, size)

        letter = (login[:1] if login else "?").upper()
        painter.setPen(QColor("#0E0E0E"))
        painter.setFont(QFont("Segoe UI", max(12, size // 2), QFont.Weight.DemiBold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter)
        painter.end()
        return pixmap

    def _load_avatar_pixmap(self, login: str, avatar_path: str, size: int = 52) -> QPixmap | None:
        if avatar_path and os.path.isfile(avatar_path):
            pixmap = QPixmap(avatar_path)
            if not pixmap.isNull():
                return make_rounded_pixmap(pixmap, size, size // 2)
        return self._build_avatar_pixmap(login, size=size)
