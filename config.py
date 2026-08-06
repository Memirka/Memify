import os
import sys

SERVER_URL = "https://memify.memiras.net"

# Bump this on every release that gets built into dist/Memify (or Memify.exe)
# and uploaded to the server — it's what the running app compares against
# the server's version.txt to decide whether to self-update.
APP_VERSION = "1.2.4"

CACHE_SIZE = 100
IMAGE_CACHE_SIZE = 50

if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

DATA_DIR = os.path.join(APP_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Where the user drops their own Artist/Album/track.mp3 folders (mirroring
# the server's "Музыка" layout) for the local-library sidebar section. Not
# created eagerly here — only on first enabling the feature, via
# core.local_library.ensure_local_music_dir() — so a user who never turns
# it on doesn't get an empty "music" folder cluttering their install.
LOCAL_MUSIC_DIR = os.path.join(APP_DIR, "music")

# Frozen builds bundle libVLC's shared library + its "plugins" folder right
# next to the executable (see memify.spec) — python-vlc's own auto-discovery
# (Windows registry lookup, Linux ldconfig cache) only ever finds a
# system-wide VLC install, never files sitting next to our own exe. Pointing
# it there explicitly via these env vars (which python-vlc's find_lib()
# checks FIRST, before any OS-specific discovery) is the officially
# supported override — see python-vlc's vlc.py, PYTHON_VLC_LIB_PATH/
# PYTHON_VLC_MODULE_PATH. This has to run before `import vlc` happens
# ANYWHERE in the process (it resolves the library at import time), which is
# why it lives here in config.py — the one module everything else already
# imports first. In dev (unfrozen) runs this is a no-op; the system's own
# installed VLC (e.g. via pacman/apt) is used instead, same as today.
if getattr(sys, "frozen", False):
    _vlc_lib_name = "libvlc.dll" if sys.platform == "win32" else "libvlc.so.5"
    _vlc_lib_path = os.path.join(BASE_DIR, _vlc_lib_name)
    _vlc_plugins_path = os.path.join(BASE_DIR, "plugins")
    if os.path.isfile(_vlc_lib_path) and os.path.isdir(_vlc_plugins_path):
        os.environ["PYTHON_VLC_LIB_PATH"] = _vlc_lib_path
        os.environ["PYTHON_VLC_MODULE_PATH"] = _vlc_plugins_path
        os.environ["VLC_PLUGIN_PATH"] = _vlc_plugins_path

LOGIN_FILE = os.path.join(DATA_DIR, "login.json")

CACHE_DIR = os.path.join(DATA_DIR, "cache")
COVER_CACHE_DIR = os.path.join(CACHE_DIR, "covers")
os.makedirs(COVER_CACHE_DIR, exist_ok=True)

LIBRARY_CACHE_FILE = os.path.join(CACHE_DIR, "library_cache.json")
APP_SETTINGS_FILE = os.path.join(DATA_DIR, "app_settings.json")

# Local player-data cache (liked tracks, subscriptions, ...) is scoped per
# account login — one file per account — so switching accounts on the same
# machine never shows/overwrites a different account's likes while the
# fresh copy is still being fetched from the server.
PLAYER_DATA_CACHE_DIR = os.path.join(DATA_DIR, "player_data_cache")
os.makedirs(PLAYER_DATA_CACHE_DIR, exist_ok=True)

# Legacy single shared cache file from before per-account scoping — no longer
# read or written, just removed if still present so it can't be confused for
# current data.
_LEGACY_PLAYER_DATA_CACHE_FILE = os.path.join(DATA_DIR, "player_data_cache.json")
if os.path.exists(_LEGACY_PLAYER_DATA_CACHE_FILE):
    try:
        os.remove(_LEGACY_PLAYER_DATA_CACHE_FILE)
    except OSError:
        pass

COVER_CACHE_MAX_SIZE_MB = 128
COVER_CACHE_MAX_FILES = 2000

ASSETS_DIR = os.path.join(BASE_DIR, "assets")
ICONS_DIR = os.path.join(ASSETS_DIR, "icons")
IMAGES_DIR = os.path.join(ASSETS_DIR, "images")
SOUNDS_DIR = os.path.join(ASSETS_DIR, "sounds")

PLAYER_WARMUP_SOUND = os.path.join(SOUNDS_DIR, "memify.wav")

APP_ICON_ICO = os.path.join(ICONS_DIR, "memify.ico")
APP_ICON_PNG = os.path.join(ICONS_DIR, "memify.png")
APP_ICON = APP_ICON_PNG if os.path.exists(APP_ICON_PNG) else APP_ICON_ICO

PLACEHOLDER_IMAGE = os.path.join(IMAGES_DIR, "placeholder.png")

WINDOW_MIN_WIDTH = 1000
WINDOW_MIN_HEIGHT = 520
