from PyQt6.QtCore import QObject, pyqtSignal
import requests

from config import SERVER_URL


class PublicPlaylistsWorker(QObject):
    """One-shot lookup of random public playlists (any account) featuring
    a given artist, via the Memify server's /playlists/public_for_artist
    route (server.py) — the matching scan runs server-side across every
    account's playlists, something a client has no way to do on its own
    since it only ever has its own account's playlist data locally."""

    finished = pyqtSignal(list)  # [{"id", "name", "cover_data", "owner_login"}, ...]

    TIMEOUT = 8

    def __init__(self, artist_name: str, album_ids: list[str], limit: int = 14):
        super().__init__()
        self._artist_name = artist_name
        self._album_ids = album_ids
        self._limit = limit
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        playlists = []
        try:
            resp = requests.post(
                f"{SERVER_URL}/playlists/public_for_artist",
                json={"artist_name": self._artist_name, "album_ids": self._album_ids, "limit": self._limit},
                timeout=self.TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("ok"):
                    playlists = data.get("playlists") or []
        except Exception:
            playlists = []
        if not self._stop:
            self.finished.emit(playlists)
