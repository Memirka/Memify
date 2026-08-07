COLORS = {
    "PRIMARY": "#1DB954",
    "PRIMARY_HOVER": "#1ED760",
    "PRIMARY_PRESSED": "#179F47",
    # Same as PRIMARY when the accent is a single solid color; a CSS
    # `qlineargradient(...)` string when the user picked a second color —
    # see set_accent_color(). Only usable in QSS `background:` contexts
    # (native QColor(...) painting and `color:`/`border-color:` in QSS can't
    # render a gradient, so those keep using plain PRIMARY everywhere).
    "PRIMARY_GRADIENT": "#1DB954",
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
_accent2: str | None = None  # None => solid accent; a hex string => gradient's 2nd stop
_theme = "dark"


def _normalize_hex(color: str | None) -> str:
    """Always returns a valid hex color — falls back to the default green
    when given anything invalid/empty. Used for the primary accent color,
    which must always have *some* value."""
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


def _try_normalize_hex(color: str | None) -> str | None:
    """Like _normalize_hex, but returns None instead of falling back to a
    default — used for the optional gradient 2nd color, where "not given"
    has to mean "no gradient", not "green"."""
    if not color:
        return None
    v = color.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        int(v, 16)
    except ValueError:
        return None
    return "#" + v.upper()


def set_accent_color(color: str, color2: str | None = None) -> str:
    """Sets the accent to a solid color, or — when color2 is given and
    differs from color — a two-stop gradient. COLORS['PRIMARY'] always
    stays a flat, valid color (color1) so every existing QColor(...)/`color:`
    /`border-color:` usage keeps working untouched; COLORS['PRIMARY_GRADIENT']
    holds the CSS gradient string (or just falls back to the flat color) for
    the QSS `background:` spots built to use it."""
    global _accent, _accent2
    _accent = _normalize_hex(color)
    color2_norm = _try_normalize_hex(color2)
    _accent2 = color2_norm if (color2_norm and color2_norm != _accent) else None

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

    COLORS["PRIMARY_GRADIENT"] = (
        f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {_accent}, stop:1 {_accent2})"
        if _accent2 else _accent
    )
    return _accent


def get_accent_secondary() -> str | None:
    return _accent2


def is_gradient_accent() -> bool:
    return _accent2 is not None


def accent_brush(x1: float, y1: float, x2: float, y2: float):
    """QBrush spanning the given local line — a genuine 2-stop gradient
    when the accent has a 2nd color, otherwise a plain solid-color brush.
    Safe to use unconditionally in any QPainter fill/stroke that wants "the
    accent color" (toggle switches, active glyphs, spinners, dots, ...) —
    callers don't need their own if/else for solid vs. gradient mode."""
    from PyQt6.QtGui import QBrush, QColor, QLinearGradient
    if _accent2:
        grad = QLinearGradient(x1, y1, x2, y2)
        grad.setColorAt(0.0, QColor(_accent))
        grad.setColorAt(1.0, QColor(_accent2))
        return QBrush(grad)
    return QBrush(QColor(_accent))


def accent_fade_brush(x1: float, y1: float, x2: float, y2: float, alpha_start: int = 255, alpha_end: int = 0):
    """Like accent_brush(), but fading in alpha from alpha_start (at the
    line's start) to alpha_end (at its end) — e.g. a glow/fill that fades
    to transparent. Blends both accent colors along the same line when a
    gradient is set, so the fade isn't just a flat color's opacity ramp."""
    from PyQt6.QtGui import QBrush, QColor, QLinearGradient
    c1 = QColor(_accent)
    c1.setAlpha(alpha_start)
    c2 = QColor(_accent2 or _accent)
    c2.setAlpha(alpha_end)
    grad = QLinearGradient(x1, y1, x2, y2)
    grad.setColorAt(0.0, c1)
    grad.setColorAt(1.0, c2)
    return QBrush(grad)


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
