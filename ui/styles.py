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

# Non-accent palette per theme. PRIMARY/PRIMARY_HOVER/PRIMARY_PRESSED are
# managed separately by set_accent_color() and carried over untouched
# whenever the theme changes.
THEMES = {
    "dark": {
        "BACKGROUND": "#121212",
        "SURFACE": "#1E1E1E",
        "SURFACE_LIGHT": "#2A2A2A",
        "SURFACE_HOVER": "#2E2E2E",
        "TEXT_PRIMARY": "#FFFFFF",
        "TEXT_SECONDARY": "#B3B3B3",
        "BORDER": "#404040",
        "COVER_BG": "#282828",
    },
    "light": {
        "BACKGROUND": "#F5F5F5",
        "SURFACE": "#FFFFFF",
        "SURFACE_LIGHT": "#EDEDED",
        "SURFACE_HOVER": "#E4E4E4",
        "TEXT_PRIMARY": "#1A1A1A",
        "TEXT_SECONDARY": "#6B6B6B",
        "BORDER": "#D9D9D9",
        "COVER_BG": "#E4E4E4",
    },
}

_accent = COLORS["PRIMARY"]
_theme = "dark"


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


def set_theme(mode: str) -> str:
    global _theme
    _theme = mode if mode in THEMES else "dark"
    COLORS.update(THEMES[_theme])
    return _theme


def get_theme() -> str:
    return _theme


def apply_palette(app) -> None:
    """Refresh the QApplication-wide QPalette from the current COLORS/theme.

    Widgets style themselves via per-widget stylesheets computed from COLORS,
    but the app-level palette is what the composited window falls back to
    for regions Qt paints outside those stylesheets (e.g. during widget
    show/hide transitions in a QStackedLayout). Left stale after a theme
    switch, that fallback still shows the old theme even though every
    widget's own QSS is already correct — so this must be re-called whenever
    the theme (or accent) changes, not just once at startup.
    """
    from PyQt6.QtGui import QPalette, QColor

    c = COLORS
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c["BACKGROUND"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(c["SURFACE"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c["SURFACE"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["SURFACE_LIGHT"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(c["SURFACE"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["TEXT_PRIMARY"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c["PRIMARY"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    app.setPalette(palette)


def get_scrollbar_style() -> str:
    """Built fresh on each call (not frozen at import time) so it always
    reflects the current theme/accent — call this from widget construction
    code, after set_theme()/set_accent_color() have already run."""
    return f"""
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


def get_album_widget_style() -> str:
    return f"""
        QWidget#albumCard {{
            background-color: {COLORS['SURFACE']};
            border-radius: 14px;
        }}
        QWidget#albumCard:hover {{
            background-color: {COLORS['SURFACE_LIGHT']};
        }}
    """
