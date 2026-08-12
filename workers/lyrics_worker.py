import re

from PyQt6.QtCore import QObject, pyqtSignal
import requests


# [mm:ss.xx] or [mm:ss] at the start of an LRC line — xx (centiseconds) is
# optional and can be 1-3 digits depending on the source.
_LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]")


def parse_lrc(lrc_text: str) -> list[tuple[int, str]]:
    """[(ms, line_text), ...] sorted by time. A line can carry more than one
    timestamp (a repeated chorus tagged once, standard LRC shorthand) —
    each timestamp becomes its own entry pointing at the same text."""
    lines: list[tuple[int, str]] = []
    for raw_line in (lrc_text or "").splitlines():
        matches = list(_LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        text = _LRC_TIME_RE.sub("", raw_line).strip()
        for m in matches:
            minutes = int(m.group(1))
            seconds = float(m.group(2))
            ms = int(round((minutes * 60 + seconds) * 1000))
            lines.append((ms, text))
    lines.sort(key=lambda pair: pair[0])
    return lines


class LyricsWorker(QObject):
    """One-shot lyrics lookup against lrclib.net's free, keyless API — pulls
    both the plain text and, when available, the synced (LRC) version for
    line-by-line highlighting/seeking (see LyricsViewerOverlay.set_position).

    Tries the exact-match endpoint first (artist+title+album+duration, when
    known), which 404s on the smallest metadata mismatch (a remaster tag, a
    stray dash Memify's own track titles don't always agree with), then
    falls back to the fuzzy search endpoint and takes the first hit with
    actual text.
    """

    finished = pyqtSignal(str, list)  # (plain_lyrics, synced_lines: list[(ms, text)])

    BASE_URL = "https://lrclib.net/api"
    TIMEOUT = 6

    def __init__(self, artist: str, title: str, album: str = "", duration_sec: int = 0):
        super().__init__()
        self._artist = artist
        self._title = title
        self._album = album
        self._duration = duration_sec
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            plain, synced = self._fetch()
        except Exception:
            plain, synced = "", []
        if not self._stop:
            self.finished.emit(plain, synced)

    def _fetch(self) -> tuple[str, list[tuple[int, str]]]:
        session = requests.Session()
        session.headers.update({"User-Agent": "Memify/1.0"})

        params = {"artist_name": self._artist, "track_name": self._title}
        if self._album:
            params["album_name"] = self._album
        if self._duration:
            params["duration"] = self._duration
        try:
            resp = session.get(f"{self.BASE_URL}/get", params=params, timeout=self.TIMEOUT)
            if resp.status_code == 200:
                result = self._extract(resp.json())
                if result:
                    return result
        except Exception:
            pass

        if self._stop:
            return "", []

        try:
            resp = session.get(
                f"{self.BASE_URL}/search",
                params={"artist_name": self._artist, "track_name": self._title},
                timeout=self.TIMEOUT,
            )
            if resp.status_code == 200:
                for item in (resp.json() or []):
                    result = self._extract(item)
                    if result:
                        return result
        except Exception:
            pass
        return "", []

    @staticmethod
    def _extract(data: dict) -> tuple[str, list[tuple[int, str]]] | None:
        plain = (data.get("plainLyrics") or "").strip()
        synced_raw = (data.get("syncedLyrics") or "").strip()
        if not plain and not synced_raw:
            return None
        return plain, (parse_lrc(synced_raw) if synced_raw else [])
