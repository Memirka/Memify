from PyQt6.QtCore import QObject, pyqtSignal
import requests

from config import SERVER_URL


class ArtistBioWorker(QObject):
    """One-shot artist biography lookup against the Memify server's
    /artist_bio route (server.py), which itself proxies Last.fm's
    artist.getinfo — kept server-side (not called directly from here, unlike
    LyricsWorker's lrclib.net calls) since it needs an API key that
    shouldn't ship inside every client build, and so the server can cache
    one lookup per artist across every user instead of each client
    re-fetching it."""

    finished = pyqtSignal(str, str)  # (artist, bio) — bio is "" if not found/unavailable

    TIMEOUT = 6

    def __init__(self, artist: str):
        super().__init__()
        self._artist = artist
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        bio = ""
        try:
            resp = requests.post(
                f"{SERVER_URL}/artist_bio", json={"artist": self._artist}, timeout=self.TIMEOUT
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("ok"):
                    bio = (data.get("bio") or "").strip()
        except Exception:
            bio = ""
        if not self._stop:
            self.finished.emit(self._artist, bio)
