COLORS = {
    "PRIMARY": "#1DB954",
    "PRIMARY_HOVER": "#1ED760",
    "PRIMARY_PRESSED": "#179F47",
    "BACKGROUND": "#121212",
    "SURFACE": "#1E1E1E",
    "SURFACE_LIGHT": "#2A2A2A",
    "SURFACE_HOVER": "#2E2E2E",
    "TEXT_PRIMARY": "#FFFFFF",
    "TEXT_SECONDARY": "#B3B3B3",
    "BORDER": "#404040",
    "COVER_BG": "#282828",
}

ACCENT_COLOR_PRESETS = [
    {"title": "Зелёные",    "colors": ["#1DB954", "#17A64A"]},
    {"title": "Мятные",     "colors": ["#2DD4BF", "#0F766E"]},
    {"title": "Бирюзовые",  "colors": ["#22D3EE", "#0891B2"]},
    {"title": "Голубые",    "colors": ["#3B82F6", "#1D4ED8"]},
    {"title": "Синие",      "colors": ["#6366F1", "#4338CA"]},
    {"title": "Фиолетовые", "colors": ["#A855F7", "#7E22CE"]},
    {"title": "Розовые",    "colors": ["#F472B6", "#DB2777"]},
    {"title": "Красные",    "colors": ["#F87171", "#DC2626"]},
    {"title": "Оранжевые",  "colors": ["#FB923C", "#EA580C"]},
    {"title": "Жёлтые",     "colors": ["#FDE047", "#F59E0B"]},
]

_accent = COLORS["PRIMARY"]


def _normalize_hex(color: str | None) -> str:
    default = "#1DB954"
    if not color:
        return default
    v = color.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return default
    try:
        int(v, 16)
    except ValueError:
        return default
    return "#" + v.upper()


def set_accent_color(color: str) -> str:
    global _accent
    _accent = _normalize_hex(color)
    COLORS["PRIMARY"] = _accent
    # Compute hover (slightly brighter) and pressed (slightly darker) variants
    try:
        from PyQt6.QtGui import QColor
        c = QColor(_accent)
        h, s, v, a = c.getHsvF()
        hover_c = QColor.fromHsvF(h, max(0.0, s - 0.05), min(1.0, v + 0.08), a)
        pressed_c = QColor.fromHsvF(h, min(1.0, s + 0.05), max(0.0, v - 0.12), a)
        COLORS["PRIMARY_HOVER"] = hover_c.name().upper()
        COLORS["PRIMARY_PRESSED"] = pressed_c.name().upper()
    except Exception:
        pass
    return _accent


def get_accent() -> str:
    return _accent


SCROLLBAR_STYLE = f"""
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 0;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {COLORS['SURFACE_LIGHT']};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {COLORS['BORDER']};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {COLORS['SURFACE_LIGHT']};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {COLORS['BORDER']};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
"""

COVER_LABEL_STYLE = f"""
    QLabel {{
        background-color: {COLORS['COVER_BG']};
        border-radius: 8px;
    }}
"""

TRACK_TITLE_STYLE = f"""
    QLabel {{
        color: {COLORS['TEXT_PRIMARY']};
        font-weight: bold;
    }}
"""

ALBUM_WIDGET_STYLE = f"""
    QWidget#albumCard {{
        background-color: {COLORS['SURFACE']};
        border-radius: 14px;
    }}
    QWidget#albumCard:hover {{
        background-color: {COLORS['SURFACE_LIGHT']};
    }}
"""

SIDEBAR_STYLE = f"""
    QWidget#sidebar {{
        background-color: {COLORS['SURFACE']};
        border-radius: 12px;
    }}
"""

TRACK_ROW_STYLE = f"""
    QWidget#trackRow {{
        background: transparent;
        border-radius: 6px;
    }}
    QWidget#trackRow:hover {{
        background-color: {COLORS['SURFACE_LIGHT']};
    }}
"""
