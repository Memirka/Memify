"""Resolution and repair helpers for account-synced track references.

Liked tracks and playlist entries intentionally store a small reference instead
of a full library track.  Album IDs are normally the most stable part of that
reference, but an old desktop library cache can briefly contain an ID that the
server has since regenerated.  The media URL is still usable in that case, so
resolution must try it across the whole current library before treating the
entry as missing.

This module is UI-free so the same rules can be exercised without importing
PyQt or VLC.
"""

from __future__ import annotations

from typing import Iterable

from utils.format_utils import clean_artist_name, clean_title, normalize_track_url


_YOUTUBE_METADATA_KEYS = (
    "_youtube_thumbnail",
    "_youtube_channel_url",
    "_youtube_channel_avatar",
    "duration",
)


def _normalized_title(value) -> str:
    raw = str(value or "").strip()
    return clean_title(raw).strip().casefold() if raw else ""


def _normalized_artist(value) -> str:
    raw = str(value or "").strip()
    return clean_artist_name(raw).strip().casefold() if raw else ""


def _library_tracks(library: Iterable[dict]):
    for artist in library or []:
        if not isinstance(artist, dict):
            continue
        for album in artist.get("albums", []) or []:
            if not isinstance(album, dict):
                continue
            for track in album.get("tracks", []) or []:
                if isinstance(track, dict):
                    yield track, album, artist


class LibraryTrackResolver:
    """Reusable lookup index for resolving several refs against one library."""

    def __init__(self, library: Iterable[dict]):
        self._by_url: dict[str, tuple[dict, dict, dict]] = {}
        self._by_id_title: dict[tuple[str, str], tuple[dict, dict, dict]] = {}
        self._by_legacy: dict[tuple[str, str, str], tuple[dict, dict, dict]] = {}
        for track, album, artist in _library_tracks(library):
            match = (track, album, artist)
            url = normalize_track_url(track.get("url") or "")
            if url:
                self._by_url.setdefault(url, match)
            title = _normalized_title(track.get("title"))
            album_id = str(track.get("album_id") or album.get("album_id") or "").strip()
            if album_id and title:
                self._by_id_title.setdefault((album_id, title), match)
            artist_name = _normalized_artist(artist.get("artist"))
            album_title = _normalized_title(album.get("title"))
            if title and (artist_name or album_title):
                self._by_legacy.setdefault((artist_name, album_title, title), match)

    def resolve(self, ref):
        """Return ``(track, album, artist)`` for a saved library reference.

        Resolution order is deliberately URL-first.  It repairs the important
        stale-cache case where ``album_id`` changed but the concrete media URL
        did not.  Stable ID + title remains the second choice for album/folder
        renames, followed by the legacy artist/album/title tuple.
        """
        if isinstance(ref, str):
            ref = {"url": ref}
        if not isinstance(ref, dict) or ref.get("_is_youtube"):
            return None

        saved_url = normalize_track_url(ref.get("url") or "")
        if saved_url and saved_url in self._by_url:
            return self._by_url[saved_url]

        album_id = str(ref.get("album_id") or "").strip()
        track_title = _normalized_title(ref.get("track_title") or ref.get("title"))
        if album_id and track_title:
            match = self._by_id_title.get((album_id, track_title))
            if match:
                return match

        artist_name = _normalized_artist(ref.get("artist_name"))
        album_title = _normalized_title(ref.get("album_title"))
        if track_title and (artist_name or album_title):
            exact = self._by_legacy.get((artist_name, album_title, track_title))
            if exact:
                return exact
            # A very old ref may lack either artist or album.  Keep the
            # fallback constrained by whichever field is actually present.
            for (candidate_artist, candidate_album, candidate_title), match in self._by_legacy.items():
                if candidate_title != track_title:
                    continue
                if artist_name and candidate_artist != artist_name:
                    continue
                if album_title and candidate_album != album_title:
                    continue
                return match

        return None


def resolve_library_track_ref(ref, library: Iterable[dict]):
    return LibraryTrackResolver(library).resolve(ref)


def canonical_library_track_ref(track: dict, album: dict, artist: dict) -> dict:
    """Build the cross-client reference shape from a current library match."""
    return {
        "url": track.get("url") or "",
        "artist_name": track.get("artist_name") or artist.get("artist") or "",
        "album_title": album.get("title") or "",
        "track_title": track.get("title") or "",
        "album_id": str(track.get("album_id") or album.get("album_id") or "").strip(),
    }


def canonical_youtube_track_ref(ref: dict) -> dict | None:
    """Normalize a permanent YouTube reference, or reject a broken one."""
    if not isinstance(ref, dict) or not ref.get("_is_youtube"):
        return None
    url = str(ref.get("url") or ref.get("youtube_url") or "").strip()
    title = str(ref.get("track_title") or ref.get("title") or "").strip()
    if not url or not title:
        return None
    clean = {
        "url": url,
        "artist_name": str(ref.get("artist_name") or "YouTube").strip() or "YouTube",
        "album_title": str(ref.get("album_title") or title).strip(),
        "track_title": title,
        "album_id": "",
        "_is_youtube": True,
    }
    for key in _YOUTUBE_METADATA_KEYS:
        value = ref.get(key)
        if value:
            clean[key] = value
    return clean


def collection_ref_identity(ref: dict) -> str:
    album_id = str(ref.get("album_id") or "").strip()
    title = _normalized_title(ref.get("track_title") or ref.get("title"))
    if album_id and title:
        return f"id:{album_id}::{title}"
    url = normalize_track_url(ref.get("url") or "")
    return f"url:{url}" if url else ""


def repair_collection_refs(
    refs, library: Iterable[dict] | LibraryTrackResolver
) -> tuple[list[dict], int, bool]:
    """Canonicalize resolvable refs and remove only genuinely missing ones.

    Returns ``(clean_refs, removed_count, changed)``.  Duplicate references
    collapse to one entry while preserving the first occurrence/order.
    """
    clean_refs: list[dict] = []
    seen: set[str] = set()
    removed = 0
    changed = False
    resolver = library if isinstance(library, LibraryTrackResolver) else LibraryTrackResolver(library)

    for original in refs or []:
        if isinstance(original, dict) and original.get("_is_youtube"):
            canonical = canonical_youtube_track_ref(original)
        else:
            match = resolver.resolve(original)
            canonical = canonical_library_track_ref(*match) if match else None

        if not canonical:
            removed += 1
            changed = True
            continue

        identity = collection_ref_identity(canonical)
        if not identity or identity in seen:
            removed += 1
            changed = True
            continue
        seen.add(identity)
        clean_refs.append(canonical)
        if canonical != original:
            changed = True

    return clean_refs, removed, changed
