import os
import sys

SERVER_URL = "http://93.116.83.14:5050"

# Bump this on every release that gets built into dist/Memify (or Memify.exe)
# and uploaded to the server — it's what the running app compares against
# the server's version.txt to decide whether to self-update.
APP_VERSION = "1.1.4"

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
