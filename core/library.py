import json
import os
import heapq
import re
import requests
from config import SERVER_URL, LIBRARY_CACHE_FILE
from utils.format_utils import clean_title, clean_artist_name, normalize_cyr, match_score, token_score, normalize_track_url

_library_cache = None


class SearchResult:
    def __init__(self, result_type, artist_name, album_title=None, track_title=None,
                 cover_url=None, artist_obj=None, album_obj=None, track_obj=None,
                 artist_names=None, playlist_obj=None, playlist_owner_login=None,
                 playlist_editable=False, playlist_cover_pixmap=None,
                 youtube_obj=None):
        self.type = result_type
        self.artist_name = artist_name
        self.album_title = album_title
        self.track_title = track_title
        self.cover_url = cover_url
        self.artist_obj = artist_obj
        self.album_obj = album_obj
        self.track_obj = track_obj
        # All artist names when this album/track is shared (same album_id)
        # across several artists — otherwise just [artist_name].
        self.artist_names = list(artist_names) if artist_names else ([artist_name] if artist_name else [])
        # Playlists aren't part of the shared library LibraryManager searches
        # (they're per-account) — MusicApp constructs these directly instead
        # of going through fast_search. playlist_obj is either one of the
        # account's own playlist dicts (playlist_editable=True) or a
        # playlist_subscriptions entry {owner_login, playlist_id, name,
        # cover_data} for a "+"-ed one (playlist_editable=False).
        self.playlist_obj = playlist_obj
        self.playlist_owner_login = playlist_owner_login
        self.playlist_editable = playlist_editable
        self.playlist_cover_pixmap = playlist_cover_pixmap
        # {id, title, uploader, duration, thumbnail, webpage_url} from
        # core/youtube.py's search_youtube() — only set when type == "youtube".
        self.youtube_obj = youtube_obj or {}

    def artists_display(self) -> str:
        return ", ".join(clean_artist_name(n) for n in self.artist_names) if self.artist_names else clean_artist_name(self.artist_name)

    def get_display_text(self):
        if self.type == "artist":
            return clean_artist_name(self.artist_name)
        elif self.type == "album":
            return f"📁 {clean_title(self.album_title)} • {self.artists_display()}"
        elif self.type == "track":
            return f"🎵 {clean_title(self.track_title)} • {self.artists_display()}"
        elif self.type == "youtube":
            return f"▶ {self.youtube_obj.get('title', '')}"
        return ""


class LibraryManager:
    def __init__(self):
        self.library = []
        self._search_index = []
        self._album_id_artists: dict[str, list[str]] = {}

    def load_library(self) -> list:
        global _library_cache
        if _library_cache is not None:
            self.library = list(_library_cache)
            self.build_search_index()
            return self.library

        cached_data = None
        try:
            if os.path.exists(LIBRARY_CACHE_FILE):
                with open(LIBRARY_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
        except Exception:
            cached_data = None

        lib_data = None
        try:
            res = requests.get(SERVER_URL + "/music/library", timeout=8)
            res.raise_for_status()
            lib_data = res.json()
        except Exception:
            lib_data = None

        def _sanitize(data):
            if not isinstance(data, list):
                return None
            return [
                a for a in data
                if isinstance(a, dict) and not (a.get("artist", "") or "").startswith("---")
            ]

        lib_data = _sanitize(lib_data) or _sanitize(cached_data) or []

        try:
            if lib_data:
                os.makedirs(os.path.dirname(LIBRARY_CACHE_FILE), exist_ok=True)
                with open(LIBRARY_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(lib_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        _library_cache = sorted(
            lib_data,
            key=lambda x: (x.get("artist", "") or "").lower() if isinstance(x, dict) else ""
        )

        self.library = list(_library_cache)
        self.build_search_index()
        return self.library

    def reload_from_server(self) -> list:
        global _library_cache
        _library_cache = None
        try:
            if os.path.exists(LIBRARY_CACHE_FILE):
                os.remove(LIBRARY_CACHE_FILE)
        except Exception:
            pass
        return self.load_library()

    def clear_cache(self):
        """Reset the in-memory library cache so next load goes to network."""
        global _library_cache
        _library_cache = None

    def refresh_from_network(self) -> list:
        """Fetch fresh library from server; on failure keep existing data unchanged."""
        global _library_cache
        lib_data = None
        try:
            res = requests.get(SERVER_URL + "/music/library", timeout=8)
            res.raise_for_status()
            lib_data = res.json()
        except Exception:
            lib_data = None

        def _sanitize(data):
            if not isinstance(data, list):
                return None
            return [
                a for a in data
                if isinstance(a, dict) and not (a.get("artist", "") or "").startswith("---")
            ]

        clean = _sanitize(lib_data)
        if not clean:
            # Network failed — keep whatever is already in library
            return list(self.library)

        try:
            os.makedirs(os.path.dirname(LIBRARY_CACHE_FILE), exist_ok=True)
            with open(LIBRARY_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        _library_cache = sorted(
            clean,
            key=lambda x: (x.get("artist", "") or "").lower() if isinstance(x, dict) else ""
        )
        self.library = list(_library_cache)
        self.build_search_index()
        return self.library

    def build_search_index(self):
        self._search_index = []
        self._album_id_artists = {}

        def _norm(text: str) -> str:
            return normalize_cyr(clean_title(text)) if text else ""

        def _tokens(text: str) -> tuple:
            return tuple(t for t in (text or "").split() if t)

        def _ujoin(*parts) -> str:
            seen, out = set(), []
            for p in parts:
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
            return " ".join(out)

        def _sorted_names(entries) -> list:
            seen, names = set(), []
            for a, *_rest in entries:
                n = a.get("artist", "")
                if n and n not in seen:
                    seen.add(n)
                    names.append(n)
            return sorted(names, key=lambda s: (s or "").lower())

        # album_id (or a per-object fallback key for albums/tracks without one)
        # -> list of (artist_obj, album_obj[, track_obj]), so albums/tracks
        # duplicated under several artists collapse into a single search result.
        album_groups: dict = {}
        track_groups: dict = {}

        for artist in self.library:
            artist_name = artist.get("artist", "")
            if not artist_name:
                continue

            artist_norm = _norm(clean_artist_name(artist_name))
            self._search_index.append({
                "type": "artist",
                "artist_obj": artist,
                "text": artist_norm,
                "full_text": artist_norm,
                "tokens": _tokens(artist_norm),
            })

            for album in artist.get("albums", []):
                title = album.get("title", "")
                album_id = str(album.get("album_id") or "").strip()
                if album_id:
                    artists_for_id = self._album_id_artists.setdefault(album_id, [])
                    if artist_name not in artists_for_id:
                        artists_for_id.append(artist_name)

                if title:
                    album_key = album_id or ("noid", id(album))
                    album_groups.setdefault(album_key, []).append((artist, album))

                for track in album.get("tracks", []):
                    t = track.get("title", "")
                    if not t:
                        continue
                    track_album_id = str(track.get("album_id") or album_id or "").strip()
                    track_key = (track_album_id, _norm(t)) if track_album_id else ("noid", id(track))
                    track_groups.setdefault(track_key, []).append((artist, album, track))

        for pairs in album_groups.values():
            names = _sorted_names(pairs)
            primary_artist, primary_album = sorted(pairs, key=lambda p: (p[0].get("artist") or "").lower())[0]
            album_norm = _norm(primary_album.get("title", ""))
            artist_norms = [_norm(clean_artist_name(n)) for n in names]
            album_blob = _ujoin(album_norm, *artist_norms)
            self._search_index.append({
                "type": "album",
                "artist_obj": primary_artist,
                "album_obj": primary_album,
                "artist_names": names,
                "text": album_norm,
                "full_text": album_blob,
                "tokens": _tokens(album_blob),
            })

        for entries in track_groups.values():
            names = _sorted_names(entries)
            primary_artist, primary_album, primary_track = sorted(
                entries, key=lambda e: (e[0].get("artist") or "").lower()
            )[0]
            track_norm = _norm(primary_track.get("title", ""))
            album_norm = _norm(primary_album.get("title", ""))
            artist_norms = [_norm(clean_artist_name(n)) for n in names]
            track_blob = _ujoin(track_norm, album_norm, *artist_norms)
            self._search_index.append({
                "type": "track",
                "artist_obj": primary_artist,
                "album_obj": primary_album,
                "track_obj": primary_track,
                "artist_names": names,
                "text": track_norm,
                "full_text": track_blob,
                "tokens": _tokens(track_blob),
            })

    def fast_search(self, query: str, limit: int = 100) -> list:
        raw = (query or "").strip()
        if not raw:
            return []
        q = normalize_cyr(raw).strip()
        if not q:
            return []
        if not self._search_index:
            self.build_search_index()
        if not self._search_index:
            return []

        q_words = [t for t in q.split() if t]
        q_tokens = tuple(t for t in q_words if len(t) >= 2) or tuple(q_words[:1])

        keep = {"track": max(80, limit), "album": max(60, int(limit * 0.75)), "artist": max(30, int(limit * 0.4))}
        heaps = {"track": [], "album": [], "artist": []}
        seq = 0

        def _score(item: dict) -> int:
            blob = item.get("full_text") or item.get("text") or ""
            text = item.get("text") or blob
            if not blob:
                return 0

            full_match = max(match_score(q, text), match_score(q, blob))
            token_hits, token_sum = 0, 0
            for tok in q_tokens:
                s = match_score(tok, blob)
                token_sum += s
                if s > 0:
                    token_hits += 1

            if full_match == 0 and token_hits == 0:
                return 0

            base = int(full_match * 10)
            base += int((token_sum / max(1, len(q_tokens))) * 6)
            base += int((token_hits / max(1, len(q_tokens))) * 240)
            base += {"track": 90, "album": 60, "artist": 30}.get(item.get("type"), 0)
            try:
                base -= min(80, max(0, len(text) - len(q)) // 8)
            except Exception:
                pass
            return max(0, base)

        for item in self._search_index:
            t = item.get("type")
            if t not in heaps:
                continue
            score = _score(item)
            if score <= 0:
                continue
            seq += 1
            heap = heaps[t]
            heapq.heappush(heap, (score, seq, item))
            if len(heap) > keep[t]:
                heapq.heappop(heap)

        def _sorted(t: str) -> list:
            return [it for _, _, it in sorted(heaps[t], key=lambda x: (-x[0], x[1]))]

        results = []
        for item in _sorted("track") + _sorted("album") + _sorted("artist"):
            if item["type"] == "artist":
                results.append(SearchResult(
                    "artist", item["artist_obj"]["artist"],
                    cover_url=item["artist_obj"].get("cover", ""),
                    artist_obj=item["artist_obj"],
                ))
            elif item["type"] == "album":
                ao = item["album_obj"]
                results.append(SearchResult(
                    "album", item["artist_obj"]["artist"],
                    album_title=ao.get("title", ""),
                    cover_url=ao.get("cover", ""),
                    artist_obj=item["artist_obj"],
                    album_obj=ao,
                    artist_names=item.get("artist_names"),
                ))
            elif item["type"] == "track":
                ao = item["album_obj"]
                to = item["track_obj"]
                results.append(SearchResult(
                    "track",
                    to.get("artist_name") or item["artist_obj"]["artist"],
                    album_title=ao.get("title", ""),
                    track_title=to.get("title", ""),
                    cover_url=ao.get("cover", ""),
                    artist_obj=item["artist_obj"],
                    album_obj=ao,
                    track_obj=to,
                    artist_names=item.get("artist_names"),
                ))
        return results

    def get_library(self) -> list:
        global _library_cache
        return list(_library_cache) if _library_cache is not None else list(self.library)

    def get_artist_by_name(self, name: str) -> dict | None:
        if not name:
            return None
        name_clean = clean_artist_name(name)
        for a in self.library:
            if not isinstance(a, dict):
                continue
            if a.get("artist") == name or clean_artist_name(a.get("artist", "")) == name_clean:
                return a
        return None

    def get_artists_for_album_id(self, album_id: str) -> list:
        """All artist names that have an album with this album_id (shared/duplicated
        album), sorted alphabetically (А-Я)."""
        if not album_id:
            return []
        if not self._album_id_artists and self.library:
            self.build_search_index()
        artists = self._album_id_artists.get(str(album_id).strip(), [])
        return sorted(artists, key=lambda s: (s or "").lower())
