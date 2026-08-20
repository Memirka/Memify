"""Search + stream resolution for the search bar's YouTube toggle (see
ui/main_window.py's _search_source). There's no official public API for
streaming YouTube audio to a third-party player — this goes through yt-dlp,
same approach most self-hosted/DIY music players use, which means it's
unofficial and can break whenever YouTube changes something on their end.
"""

from yt_dlp import YoutubeDL


def _flat_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noplaylist": True,
    }


def _stream_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        # Use YouTube's Android progressive MP4 stream. yt-dlp's default
        # audio-only URLs can be DASH/range-limited in a way that returns 403
        # after the first chunks when proxied or opened by libVLC/browser
        # media elements. Format 18 carries audio in a normal seekable MP4,
        # which is larger but reliably playable.
        "format": "18/best[ext=mp4]/best",
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "skip_download": True,
        "noplaylist": True,
    }


def _channel_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 1,
        "skip_download": True,
    }


def _best_thumbnail_url(thumbnails: list | None, prefer_square: bool = False) -> str:
    thumbnails = [t for t in (thumbnails or []) if isinstance(t, dict) and t.get("url")]
    if not thumbnails:
        return ""

    def _score(t: dict) -> tuple[int, int]:
        try:
            w = int(t.get("width") or 0)
            h = int(t.get("height") or 0)
        except Exception:
            w = h = 0
        square = 0
        if prefer_square and w > 0 and h > 0:
            square = 1 if abs(w - h) <= max(4, int(max(w, h) * 0.08)) else 0
        return square, w * h

    return max(thumbnails, key=_score).get("url", "")


def search_youtube(query: str, limit: int = 15) -> list[dict]:
    """Lightweight search (extract_flat — no per-video format resolution,
    so this stays fast enough to run on every keystroke-debounced query).
    Returns [{id, title, uploader, duration, thumbnail, webpage_url,
    channel_url, channel_avatar}, ...].

    Deliberately doesn't swallow yt-dlp errors (network failure, YouTube
    bot-check, extractor breakage after a site update, ...) — the caller
    (see MusicApp._perform_youtube_search) surfaces them in the UI, since a
    silent [] here is indistinguishable from a genuine zero-results query
    and makes failures impossible to diagnose from a windowed app with no
    visible console."""
    query = (query or "").strip()
    if not query:
        return []
    out: list[dict] = []
    with YoutubeDL(_flat_opts()) as ydl:
        info = ydl.extract_info(f"ytsearch{max(1, limit)}:{query}", download=False)
        entries = (info or {}).get("entries") or []
        for e in entries:
            if not isinstance(e, dict):
                continue
            video_id = e.get("id") or ""
            if not video_id:
                continue
            thumb_url = _best_thumbnail_url(e.get("thumbnails")) or (e.get("thumbnail") or "")
            out.append({
                "id": video_id,
                "title": e.get("title") or "",
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": int(e.get("duration") or 0),
                "thumbnail": thumb_url,
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "channel_url": e.get("channel_url") or e.get("uploader_url") or "",
                "channel_avatar": (
                    e.get("channel_avatar")
                    or e.get("channel_thumbnail")
                    or e.get("uploader_avatar")
                    or e.get("uploader_thumbnail")
                    or ""
                ),
            })
    return out


def resolve_channel_avatar_url(channel_url: str) -> str:
    """Best-effort avatar URL for a YouTube channel/handle page.

    Search results usually include the video thumbnail and channel URL, but
    not the channel avatar itself. Resolve it lazily only when the side panel
    needs it so YouTube search stays responsive.
    """
    channel_url = (channel_url or "").strip()
    if not channel_url:
        return ""
    with YoutubeDL(_channel_opts()) as ydl:
        info = ydl.extract_info(channel_url, download=False)
        info = info or {}
        return (
            info.get("channel_avatar")
            or info.get("avatar")
            or _best_thumbnail_url(info.get("thumbnails"), prefer_square=True)
            or info.get("thumbnail")
            or ""
        )


def resolve_stream_url(webpage_url: str) -> str:
    """Resolve a YouTube video page URL to a direct, playable audio-stream
    URL (typically a googlevideo.com CDN link) — libVLC plays this exactly
    like any other network media URL. These links expire after a few hours,
    so resolve right before playback, never cache/persist them.

    Errors propagate (see search_youtube's docstring for why) — the caller
    shows the real message instead of a generic "couldn't play this"."""
    webpage_url = (webpage_url or "").strip()
    if not webpage_url:
        return ""
    with YoutubeDL(_stream_opts()) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
        return (info or {}).get("url") or ""
