"""Scanner for the local-library sidebar section — a client-side mirror of
server.py's scan_music_folder(), but reading a folder on THIS machine
(config.LOCAL_MUSIC_DIR) instead of the server's "Музыка" tree, and with no
network/Flask involved at all.

Every artist/album/track dict produced here matches the shape the rest of
the app already expects from LibraryManager.library (see core/library.py
and server.py's scan_music_folder), plus a "local": True marker used
elsewhere (ui/main_window.py) to hide download actions, skip
subscription-only listings, etc. "url"/"cover" fields are file:// URIs
(QUrl.fromLocalFile(...).toString()) rather than server-relative paths —
utils.format_utils.resolve_media_url() passes those through unchanged,
and workers/image_loader.py reads file:// URLs straight off disk instead
of over HTTP.
"""

import os
import re

from PyQt6.QtCore import QUrl

from config import LOCAL_MUSIC_DIR

LOCAL_COVER_NAME = "cover.png"
NON_COVER_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
LOCAL_AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".ogg", ".wav", ".opus")

_NUMBER_RE = re.compile(r"(\d+)\s*-\s*(.*)")


def ensure_local_music_dir() -> str:
    os.makedirs(LOCAL_MUSIC_DIR, exist_ok=True)
    return LOCAL_MUSIC_DIR


def _extract_number(name: str):
    """Same convention as server.py's extract_number(): leading "NN - " ->
    that number (for sort order); no number -> sorts last."""
    m = _NUMBER_RE.match(name or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 9999


def _to_file_url(path: str) -> str:
    return QUrl.fromLocalFile(path).toString()


def _resolve_cover(folder_path: str) -> str | None:
    """cover.png if present. Otherwise, if there's exactly one (or several
    — picks the alphabetically-first) non-cover.png image in the folder,
    rename it to cover.png so it's picked up from now on, same as if the
    user had named it that way to begin with. Returns a file:// URI, or
    None if no usable image exists (caller falls back to the placeholder,
    same as artists/albums without a cover.png on the server)."""
    cover_path = os.path.join(folder_path, LOCAL_COVER_NAME)
    if os.path.isfile(cover_path):
        return _to_file_url(cover_path)

    try:
        entries = sorted(os.listdir(folder_path), key=str.lower)
    except OSError:
        return None

    for entry in entries:
        if entry.lower() == LOCAL_COVER_NAME:
            continue
        entry_path = os.path.join(folder_path, entry)
        if not os.path.isfile(entry_path):
            continue
        if os.path.splitext(entry)[1].lower() not in NON_COVER_IMAGE_EXTS:
            continue
        try:
            os.rename(entry_path, cover_path)
        except OSError:
            # Read-only folder or similar — leave the file as-is rather
            # than failing the whole scan over a missing cover.
            return None
        return _to_file_url(cover_path)

    return None


def _scan_album(artist_name: str, album_name: str, album_path: str, artist_cover: str | None) -> dict | None:
    try:
        filenames = os.listdir(album_path)
    except OSError:
        return None

    tracks = []
    for filename in filenames:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in LOCAL_AUDIO_EXTENSIONS:
            continue
        track_path = os.path.join(album_path, filename)
        if not os.path.isfile(track_path):
            continue
        tracks.append({
            "title": os.path.splitext(filename)[0],
            "url": _to_file_url(track_path),
            "local": True,
        })
    if not tracks:
        return None

    tracks.sort(key=lambda t: _extract_number(t["title"]))

    album_cover = _resolve_cover(album_path) or artist_cover

    return {
        "title": album_name,
        "album_id": f"local::{artist_name}::{album_name}",
        "cover": album_cover,
        "genre": [],
        "tracks": tracks,
        "local": True,
    }


def _scan_artist(artist_name: str, artist_path: str) -> dict | None:
    try:
        entries = os.listdir(artist_path)
    except OSError:
        return None

    artist_cover = _resolve_cover(artist_path)

    albums = []
    for album_name in entries:
        album_path = os.path.join(artist_path, album_name)
        if not os.path.isdir(album_path):
            continue
        try:
            album = _scan_album(artist_name, album_name, album_path, artist_cover)
        except OSError:
            album = None
        if album:
            albums.append(album)
    if not albums:
        return None

    albums.sort(key=lambda a: _extract_number(a["title"]))

    return {
        "artist": artist_name,
        "cover": artist_cover,
        "albums": albums,
        "local": True,
    }


def scan_local_library(root: str = LOCAL_MUSIC_DIR) -> list:
    """Walk root/Artist/Album/track.{mp3,flac,...} into the same
    list-of-artist-dicts shape LibraryManager.library normally holds."""
    try:
        entries = os.listdir(root)
    except OSError:
        return []

    artists = []
    for artist_name in entries:
        artist_path = os.path.join(root, artist_name)
        if not os.path.isdir(artist_path):
            continue
        try:
            artist = _scan_artist(artist_name, artist_path)
        except OSError:
            artist = None
        if artist:
            artists.append(artist)

    artists.sort(key=lambda a: (a.get("artist") or "").lower())
    return artists
