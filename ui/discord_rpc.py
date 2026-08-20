import os
import time
import re
import uuid
import threading
import hashlib
import json
import base64
import http.server
import socketserver
import urllib.request
from urllib.parse import urlsplit, urlunsplit, unquote, quote
from pypresence import Presence
from pypresence.exceptions import ServerError
from config import SERVER_URL

DISCORD_CLIENT_ID = "1399441874250502297"
APP_ICON_URL = SERVER_URL.rstrip("/") + "/assets/memify_logo.png"
YOUTUBE_ICON_KEY = "youtube_logo"

_DECODE_MAP = {
    "7c2": "/",
    "7c3": '"',
    "7c4": "?",
    "7c5": "*",
    "7c6": ":",
}
_DECODE_RE = re.compile("|".join(_DECODE_MAP.keys()))

def decode_symbols(text: str | None) -> str:
    if not text:
        return ""
    return _DECODE_RE.sub(lambda m: _DECODE_MAP[m.group(0)], text)

def strip_leading_index(text: str | None) -> str:
    if not text:
        return ""
    t = text.strip()

    def _strip(pattern: str, value: str) -> str:
        stripped = re.sub(pattern, '', value, count=1).lstrip()
        if stripped and len(stripped) >= 2 and stripped != value:
            return stripped
        return value

    t = _strip(r'^\s*\d{1,4}\s*[-–—\.:)\]_]\s*', t)
    t = _strip(r'^\s*\d{1,4}[-_]\s*', t)
    t = _strip(r'^\s*\d{1,4}\s+', t)
    return t or text

def clean_track_title(raw: str | None) -> str:
    """Чистит название трека: убирает 01 - и декодирует символы."""
    decoded = decode_symbols(raw)
    cleaned = strip_leading_index(decoded).strip()

    if len(cleaned) >= 2:
        return cleaned

    decoded = (decoded or "").strip()
    if len(decoded) >= 2:
        return decoded

    return "Unknown track"

def clean_artist_or_album(raw: str | None, fallback: str) -> str:
    """Чистит имя исполнителя или альбом (без префикса номера)."""
    text = decode_symbols(raw)
    text = strip_leading_index(text)
    text = text.strip()
    return text or fallback

class DiscordRPC:
    def __init__(self):
        self.rpc = None
        self.connected = False
        self._last_started = None
        self._last_tuple = None
        self._last_cover_url = None
        self._last_is_youtube = False
        self._last_is_local = False
        self._last_position_ms = 0
        self._last_duration_ms = 0
        self._cover_proxy_server = None
        self._cover_proxy_thread = None
        self._cover_proxy_port = None
        self._cover_proxy_map: dict[str, str] = {}
        self._cover_proxy_lock = threading.Lock()
        self._rpc_lock = threading.RLock()
        self._external_image_cache: dict[str, str] = {}
        self._external_image_fail_ts: dict[str, float] = {}
        self._external_image_fail_ttl_s = 60.0
        self._local_cover_login = ""
        self._local_cover_password = ""
        self._local_cover_digest_cache: dict[str, str] = {}
        self._local_cover_stat_cache: dict[str, str] = {}
        self._local_cover_max_bytes = 5 * 1024 * 1024

    def set_local_cover_credentials(self, login: str | None, password: str | None):
        login = (login or "").strip()
        password = (password or "").strip()
        if login == self._local_cover_login and password == self._local_cover_password:
            return
        self._local_cover_login = login
        self._local_cover_password = password
        self._local_cover_digest_cache.clear()
        self._local_cover_stat_cache.clear()

    def connect(self):
        try:
            self.rpc = Presence(DISCORD_CLIENT_ID)
            self.rpc.connect()
            self.connected = True
            print("[RPC] Discord подключен")
        except Exception as e:
            print(f"[RPC] Не удалось подключиться: {e}")
            self.connected = False

    def _safe_update(self, should_cancel=None, **kwargs):
        if not self.connected:
            return
        if should_cancel and should_cancel():
            return
        payload = kwargs.get("payload_override") if isinstance(kwargs, dict) else None
        if isinstance(payload, dict):
            args = payload.get("args") or {}
            resp = self._rpc_request(
                str(payload.get("cmd") or "SET_ACTIVITY"),
                args,
                timeout_s=0.8,
                should_cancel=should_cancel,
            )
            if should_cancel and should_cancel():
                return
            try:
                if (
                    isinstance(resp, dict)
                    and resp.get("evt") == "ERROR"
                    and isinstance(args, dict)
                    and isinstance(args.get("activity"), dict)
                    and args["activity"].get("assets")
                ):
                    activity = args["activity"]
                    fallback_activity = dict(activity)
                    fallback_activity.pop("assets", None)
                    fallback_args = dict(args)
                    fallback_args["activity"] = fallback_activity
                    self._rpc_request(
                        "SET_ACTIVITY",
                        fallback_args,
                        timeout_s=0.8,
                        should_cancel=should_cancel,
                    )
            except Exception:
                pass
            return

        try:
            with self._rpc_lock:
                if should_cancel and should_cancel():
                    return
                self.rpc.update(**kwargs)
        except Exception as e:
            # Keep console output actionable: show exception type + minimal payload summary.
            try:
                msg = f"{type(e).__name__}: {e}"
            except Exception:
                msg = str(e)
            print(f"[RPC] Ошибка обновления: {msg}")

            payload = kwargs.get("payload_override") if isinstance(kwargs, dict) else None
            try:
                if isinstance(payload, dict):
                    act = (payload.get("args") or {}).get("activity") or {}
                    assets = act.get("assets") or {}
                    li = assets.get("large_image")
                    details = act.get("details")
                    state = act.get("state")
                    li_bytes = None
                    try:
                        if isinstance(li, str):
                            li_bytes = len(li.encode("utf-8", errors="ignore"))
                    except Exception:
                        li_bytes = None
                    print(
                        "[RPC] Payload summary: "
                        f"type={act.get('type')!r} "
                        f"details_len={(len(details) if isinstance(details, str) else None)!r} "
                        f"state_len={(len(state) if isinstance(state, str) else None)!r} "
                        f"large_image_len={(len(li) if isinstance(li, str) else None)!r} "
                        f"large_image_bytes={li_bytes!r}"
                    )
                    try:
                        if isinstance(li, str) and li:
                            sample = li[:180].replace("\n", "\\n").replace("\r", "\\r")
                            print(f"[RPC] large_image sample: {sample}")
                    except Exception:
                        pass
            except Exception:
                pass

            # Some Discord builds occasionally return a generic ERROR; try a conservative fallback
            # using the official `update()` fields (no payload_override) so at least something shows.
            try:
                if (
                    isinstance(payload, dict)
                    and str(e).strip().lower() == "unknown error"
                    and getattr(self, "rpc", None) is not None
                ):
                    act = (payload.get("args") or {}).get("activity") or {}
                    assets = act.get("assets") or {}
                    ts = act.get("timestamps") or {}
                    large_image = assets.get("large_image")
                    small_image = assets.get("small_image")
                    small_text = assets.get("small_text")
                    try:
                        if isinstance(large_image, str) and (large_image.startswith("http://") or large_image.startswith("https://")):
                            # If Discord rejects the URL, try our server short-cover endpoint.
                            short = self._rpc_short_cover_url(large_image)
                            short_s = self._sanitize_cover_url_for_rpc(short)
                            if short_s:
                                large_image = short_s
                    except Exception:
                        pass
                    with self._rpc_lock:
                        self.rpc.update(
                            state=act.get("state"),
                            details=act.get("details"),
                            start=ts.get("start"),
                            end=ts.get("end"),
                            large_image=large_image,
                            small_image=small_image,
                            small_text=small_text,
                            instance=bool(act.get("instance", True)),
                        )
            except Exception as e2:
                try:
                    msg2 = f"{type(e2).__name__}: {e2}"
                except Exception:
                    msg2 = str(e2)
                print(f"[RPC] Fallback тоже не сработал: {msg2}")
                # Last resort: drop the image completely so at least the presence shows.
                try:
                    if (
                        isinstance(payload, dict)
                        and str(e).strip().lower() == "unknown error"
                        and getattr(self, "rpc", None) is not None
                    ):
                        act = (payload.get("args") or {}).get("activity") or {}
                        ts = act.get("timestamps") or {}
                        with self._rpc_lock:
                            self.rpc.update(
                                state=act.get("state"),
                                details=act.get("details"),
                                start=ts.get("start"),
                                end=ts.get("end"),
                                instance=bool(act.get("instance", True)),
                            )
                except Exception:
                    pass

    def _read_rpc_output(self):
        coro = None
        try:
            coro = self.rpc.read_output()
            return self.rpc.loop.run_until_complete(coro)
        except Exception:
            # If the loop was already closed, run_until_complete never gets
            # ownership of the coroutine. Close it explicitly so Python
            # doesn't print "coroutine ... was never awaited" during cleanup.
            if coro is not None:
                try:
                    coro.close()
                except Exception:
                    pass
            raise

    def _activity_assets(
        self,
        cover_url: str | None,
        is_youtube: bool = False,
        is_local: bool = False,
    ) -> dict:
        assets = {}
        cover_image = self._large_image_for_cover(cover_url)
        small_icon = self._youtube_icon_for_rpc() if is_youtube else self._app_icon_for_rpc()
        small_text = "YouTube" if is_youtube else "Локальная библиотека" if is_local else "Memify"

        if cover_image:
            assets["large_image"] = cover_image
            if small_icon:
                assets["small_image"] = small_icon
                assets["small_text"] = small_text

        return assets

    def _app_icon_for_rpc(self) -> str | None:
        try:
            return self._sanitize_cover_url_for_rpc(APP_ICON_URL)
        except Exception:
            return None

    def _youtube_icon_for_rpc(self) -> str:
        return YOUTUBE_ICON_KEY

    def _add_assets(
        self,
        activity: dict,
        cover_url: str | None,
        is_youtube: bool = False,
        is_local: bool = False,
    ) -> None:
        assets = self._activity_assets(cover_url, is_youtube, is_local)
        if assets:
            activity["assets"] = assets

    def _rpc_request(
        self,
        cmd: str,
        args: dict | None = None,
        *,
        timeout_s: float = 1.2,
        should_cancel=None,
    ) -> dict | None:
        if not self.connected or not self.rpc:
            return None
        try:
            if should_cancel and should_cancel():
                return None
            nonce = uuid.uuid4().hex
            payload = {"cmd": cmd, "args": args or {}, "nonce": nonce}
            with self._rpc_lock:
                if should_cancel and should_cancel():
                    return None
                prev_timeout = getattr(self.rpc, "response_timeout", None)
                try:
                    if prev_timeout is not None:
                        self.rpc.response_timeout = float(timeout_s)
                except Exception:
                    prev_timeout = None

                try:
                    self.rpc.send_data(1, payload)
                    deadline = time.time() + max(0.2, float(timeout_s))
                    resp = None
                    while time.time() < deadline:
                        if should_cancel and should_cancel():
                            return None
                        r = self._read_rpc_output()
                        if not isinstance(r, dict):
                            continue
                        if r.get("nonce") == nonce:
                            resp = r
                            break
                        # Ignore unrelated events (e.g., DISPATCH without nonce).
                        if r.get("evt") and not r.get("nonce"):
                            continue
                        resp = r
                finally:
                    try:
                        if prev_timeout is not None:
                            self.rpc.response_timeout = prev_timeout
                    except Exception:
                        pass

            return resp if isinstance(resp, dict) else None
        except ServerError as e:
            return {"evt": "ERROR", "data": {"message": str(e)}}
        except Exception:
            return None

    def _ensure_cover_proxy(self) -> int | None:
        """Start a tiny local HTTP server to proxy long cover URLs to short ones."""
        try:
            if self._cover_proxy_server is not None and self._cover_proxy_port is not None:
                return int(self._cover_proxy_port)

            parent = self

            class _Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args, **_kwargs):
                    return

                def do_GET(self):
                    try:
                        path = (self.path or "").split("?", 1)[0]
                        if not path.startswith("/c/"):
                            self.send_response(404)
                            self.end_headers()
                            return
                        key = path[len("/c/"):].strip().strip("/")
                        if not key:
                            self.send_response(404)
                            self.end_headers()
                            return

                        with parent._cover_proxy_lock:
                            url = parent._cover_proxy_map.get(key)

                        if not url:
                            self.send_response(404)
                            self.end_headers()
                            return

                        try:
                            parts = urlsplit(url)
                            if parts.scheme not in ("http", "https"):
                                raise ValueError("bad scheme")
                        except Exception:
                            self.send_response(400)
                            self.end_headers()
                            return

                        req = urllib.request.Request(
                            url,
                            headers={
                                "User-Agent": "Memify/DiscordRPC",
                                "Accept": "image/*,*/*;q=0.8",
                            },
                        )
                        with urllib.request.urlopen(req, timeout=6) as resp:
                            data = resp.read()
                            ctype = resp.headers.get("Content-Type") or "application/octet-stream"

                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    except Exception:
                        try:
                            self.send_response(502)
                            self.end_headers()
                        except Exception:
                            pass

            class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
                daemon_threads = True
                allow_reuse_address = True

            server = _ThreadingServer(("127.0.0.1", 0), _Handler)
            port = int(server.server_address[1])
            self._cover_proxy_server = server
            self._cover_proxy_port = port

            thread = threading.Thread(target=server.serve_forever, name="MemifyCoverProxy", daemon=True)
            self._cover_proxy_thread = thread
            thread.start()
            return port
        except Exception:
            return None

    def _cover_url_for_discord_fetch(self, url: str | None) -> str | None:
        """
        Provide a URL that Discord can fetch.
        - If URL is short enough (<=300 chars), pass it through as-is.
        - If URL is too long (>300 chars), proxy through localhost to avoid Discord RPC length limits.
        """
        try:
            u = (url or "").strip()
            if not u:
                return None
            if not (u.startswith("http://") or u.startswith("https://")):
                return None
            if len(u) <= 300:
                try:
                    if len(u.encode("utf-8", errors="ignore")) <= 300:
                        return u
                except Exception:
                    return u

            port = self._ensure_cover_proxy()
            if not port:
                return None

            key = hashlib.sha1(u.encode("utf-8", errors="ignore")).hexdigest()[:14]
            with self._cover_proxy_lock:
                self._cover_proxy_map[key] = u
            return f"http://127.0.0.1:{port}/c/{key}"
        except Exception:
            return None

    @staticmethod
    def _rpc_short_cover_url(cover_url: str | None) -> str | None:
        """
        Build a short, ASCII-only cover URL that points to the server endpoint
        `/rpc/c/<sha1>.png`, where sha1 is computed from the relative `/cover/...` path.

        This is the most reliable way to avoid Discord's `large_image` length/encoding limits.
        """
        try:
            u = (cover_url or "").strip()
            if not u:
                return None
            if u.startswith("http://") or u.startswith("https://"):
                parts = urlsplit(u)
                path = (parts.path or "").split("?", 1)[0].split("#", 1)[0]
            else:
                path = u.split("?", 1)[0].split("#", 1)[0]

            if not path.startswith("/cover/"):
                return None

            key = hashlib.sha1(path.encode("utf-8", errors="ignore")).hexdigest()
            return SERVER_URL.rstrip("/") + f"/rpc/c/{key}.png"
        except Exception:
            return None

    def _get_external_image_key(self, fetch_url: str | None) -> str | None:
        """Resolve URL to mp:external key using Discord IPC GET_IMAGE (best-effort)."""
        try:
            u = (fetch_url or "").strip()
            if not u:
                return None

            cached = self._external_image_cache.get(u)
            if cached:
                return cached

            last_fail = float(self._external_image_fail_ts.get(u) or 0.0)
            if last_fail and (time.time() - last_fail) < float(self._external_image_fail_ttl_s):
                return None

            resp = self._rpc_request("GET_IMAGE", {"type": 1, "url": u}, timeout_s=1.4)
            data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
            key = data.get("image")
            if isinstance(key, str) and key and len(key) <= 300:
                self._external_image_cache[u] = key
                return key

            self._external_image_fail_ts[u] = time.time()
            return None
        except Exception:
            return None

    def _sanitize_cover_url_for_rpc(self, url: str | None) -> str | None:
        """
        Return a safe URL suitable for Discord RPC `assets.large_image`.
        Discord validates `large_image` string length (<=300) and rejects invalid URLs.
        """
        try:
            u = (url or "").strip()
            if not u:
                return None
            if not (u.startswith("http://") or u.startswith("https://")):
                return None

            parts = urlsplit(u)
            if not parts.scheme or not parts.netloc:
                return None

            # Keep the URL ASCII/escaped; Discord RPC often rejects raw unicode/spaces here.
            # Note: don't keep '%' as safe here; otherwise a literal '%' in filenames would create
            # an invalid URL (e.g. ".../100%/cover.png").
            safe_path = quote(unquote(parts.path or ""), safe="/._-~")
            safe_query = quote(unquote(parts.query or ""), safe="=&._-~")

            raw = urlunsplit((parts.scheme, parts.netloc, safe_path, safe_query, ""))
            if len(raw) <= 300:
                try:
                    if len(raw.encode("utf-8", errors="ignore")) <= 300:
                        return raw
                except Exception:
                    return raw

            # If query makes the URL too long, drop it.
            raw_no_q = urlunsplit((parts.scheme, parts.netloc, safe_path, "", ""))
            if len(raw_no_q) <= 300:
                try:
                    if len(raw_no_q.encode("utf-8", errors="ignore")) <= 300:
                        return raw_no_q
                except Exception:
                    return raw_no_q

            return None
        except Exception:
            return None

    @staticmethod
    def _local_path_from_file_url(file_url: str | None) -> str:
        try:
            parts = urlsplit((file_url or "").strip())
            if parts.scheme != "file":
                return ""
            path = urllib.request.url2pathname(unquote(parts.path or ""))
            if os.name == "nt" and re.fullmatch(r"/[A-Za-z]:/.*", path or ""):
                path = path[1:]
            return os.path.abspath(path)
        except Exception:
            return ""

    @staticmethod
    def _local_cover_ext(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if data.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        return ""

    def _upload_local_cover_for_rpc(self, file_url: str | None) -> str | None:
        try:
            if not self._local_cover_login or not self._local_cover_password:
                return None
            path = self._local_path_from_file_url(file_url)
            if not path or not os.path.isfile(path):
                return None
            st = os.stat(path)
            if st.st_size <= 0 or st.st_size > self._local_cover_max_bytes:
                return None
            stat_key = f"{path}|{int(st.st_mtime_ns)}|{int(st.st_size)}"
            cached = self._local_cover_stat_cache.get(stat_key)
            if cached:
                return cached

            with open(path, "rb") as f:
                data = f.read(int(self._local_cover_max_bytes) + 1)
            if len(data) > self._local_cover_max_bytes:
                return None
            ext = self._local_cover_ext(data)
            if not ext:
                return None
            digest = hashlib.sha1(data).hexdigest()
            cached = self._local_cover_digest_cache.get(digest)
            if cached:
                self._local_cover_stat_cache[stat_key] = cached
                return cached

            payload = json.dumps(
                {
                    "login": self._local_cover_login,
                    "password": self._local_cover_password,
                    "sha1": digest,
                    "ext": ext,
                    "image_data": base64.b64encode(data).decode("ascii"),
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                SERVER_URL.rstrip("/") + "/rpc/local_cover/upload",
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Memify/DiscordRPC"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = resp.read(4096)
            result = json.loads(raw.decode("utf-8", errors="ignore")) if raw else {}
            if not isinstance(result, dict) or not result.get("ok"):
                return None
            url = (result.get("url") or "").strip()
            if url.startswith("/"):
                url = SERVER_URL.rstrip("/") + url
            url = self._sanitize_cover_url_for_rpc(url)
            if not url:
                return None
            self._local_cover_digest_cache[digest] = url
            self._local_cover_stat_cache[stat_key] = url
            if len(self._local_cover_digest_cache) > 128:
                self._local_cover_digest_cache.pop(next(iter(self._local_cover_digest_cache)), None)
            if len(self._local_cover_stat_cache) > 256:
                self._local_cover_stat_cache.pop(next(iter(self._local_cover_stat_cache)), None)
            return url
        except Exception as e:
            try:
                print(f"[RPC] local cover upload failed: {type(e).__name__}: {e}")
            except Exception:
                pass
            return None

    def _large_image_for_cover(self, cover_url: str | None) -> str | None:
        """
        Pick a Discord-compatible `assets.large_image` value.
        Use a public URL directly so the viewing Discord client loads the
        image itself. Avoid Discord GET_IMAGE here: doing that in Memify
        made track switches wait on image preparation and briefly showed the
        application icon instead of the track cover.
        """
        try:
            raw = (cover_url or "").strip()
            if not raw:
                return None

            if raw.startswith("mp:external/"):
                return raw
            if raw.startswith("file://"):
                raw = self._upload_local_cover_for_rpc(raw) or ""
                if not raw:
                    return None
            if not (raw.startswith("http://") or raw.startswith("https://") or raw.startswith("/")):
                return None

            candidate = raw
            if raw.startswith("/"):
                candidate = SERVER_URL.rstrip("/") + raw

            short = self._rpc_short_cover_url(candidate)
            short_s = self._sanitize_cover_url_for_rpc(short) if short else None
            direct_s = self._sanitize_cover_url_for_rpc(candidate)

            if short_s:
                return short_s
            if direct_s:
                return direct_s
        except Exception:
            pass
        return None

    @staticmethod
    def _build_set_activity_payload(pid: int, activity: dict | None) -> dict:
        return {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": int(pid), "activity": activity},
            "nonce": "{:.20f}".format(time.time()),
        }

    def set_play(
        self,
        title: str,
        artist: str,
        album: str,
        cover_url: str | None = None,
        position_ms: int | None = None,
        duration_ms: int | None = None,
        is_youtube: bool = False,
        is_local: bool = False,
        should_cancel=None,
    ):
        """Обновляет статус 'играет'"""
        if should_cancel and should_cancel():
            return
        self._last_started = time.time()
        ct = clean_track_title(title)
        ca = clean_artist_or_album(artist, "Неизвестный исполнитель")
        cal = clean_artist_or_album(album, "Неизвестный альбом")
        self._last_tuple = (ct, ca, cal)
        self._last_cover_url = (cover_url or "").strip() or None
        self._last_is_youtube = bool(is_youtube)
        self._last_is_local = bool(is_local)
        try:
            self._last_position_ms = int(position_ms or 0)
        except Exception:
            self._last_position_ms = 0
        try:
            self._last_duration_ms = int(duration_ms or 0)
        except Exception:
            self._last_duration_ms = 0

        now = int(time.time())
        timestamps = {}
        try:
            if self._last_duration_ms > 0:
                pos_s = max(0, int(self._last_position_ms // 1000))
                dur_s = max(1, int(self._last_duration_ms // 1000))
                pos_s = min(pos_s, dur_s)
                timestamps = {"start": int(now - pos_s), "end": int(now + (dur_s - pos_s))}
        except Exception:
            timestamps = {}

        activity = {
            "name": "Memify",
            "type": 2,  # Listening
            "details": ct,
            "state": ca,
            "instance": True,
        }
        if timestamps:
            activity["timestamps"] = timestamps

        self._add_assets(activity, self._last_cover_url, self._last_is_youtube, self._last_is_local)
        if should_cancel and should_cancel():
            return
        self._safe_update(
            payload_override=self._build_set_activity_payload(os.getpid(), activity),
            should_cancel=should_cancel,
        )

    def set_pause(self, should_cancel=None):
        """Обновляет статус 'пауза'"""
        if should_cancel and should_cancel():
            return
        if not self._last_tuple:
            return

        ct, ca, cal = self._last_tuple

        activity = {
            "name": "Memify",
            "type": 2,  # Listening
            "details": ct,
            "state": ca,
            "instance": True,
        }
        self._add_assets(activity, self._last_cover_url, self._last_is_youtube, self._last_is_local)
        if should_cancel and should_cancel():
            return

        self._safe_update(
            payload_override=self._build_set_activity_payload(os.getpid(), activity),
            should_cancel=should_cancel,
        )

    def clear(self, should_cancel=None):
        """Очистка статуса"""
        if should_cancel and should_cancel():
            return
        self._last_started = None
        self._last_tuple = None
        self._last_cover_url = None
        self._last_is_youtube = False
        self._last_is_local = False
        self._last_position_ms = 0
        self._last_duration_ms = 0
        if self.connected:
            try:
                self._rpc_request(
                    "SET_ACTIVITY",
                    {"pid": os.getpid(), "activity": None},
                    timeout_s=0.8,
                    should_cancel=should_cancel,
                )
                print("[RPC] Очищен")
            except Exception:
                pass

    def disconnect(self):
        """Закрывает соединение с Discord RPC."""
        # ui/main_window.py now calls set_play()/set_pause()/clear() from a
        # background thread (their GET_IMAGE round trip over the Discord
        # IPC pipe can block for real, up to a few seconds — doing that on
        # the Qt main thread used to freeze the whole app on every track
        # change). That means disconnect() — always called from the main
        # thread — can now genuinely race one of those calls. Flip
        # `connected` first so any call that hasn't started yet bails out
        # immediately via its own guard, then take _rpc_lock (the same lock
        # _safe_update/_rpc_request hold for the whole length of their own
        # IPC round trip) before tearing anything down, so a call already
        # in flight finishes cleanly on the still-live rpc/loop instead of
        # having them yanked out from under it mid-request.
        self.connected = False
        with self._rpc_lock:
            # pypresence opens its own asyncio event loop per connection. If
            # rpc.close() raises partway through (e.g. the Discord IPC socket
            # was already gone), the loop can end up abandoned — Python then
            # prints a harmless-but-noisy "Exception ignored ... Invalid file
            # descriptor" from its __del__ at garbage-collection time. Grab the
            # loop up front and force-close it ourselves regardless.
            loop = getattr(self.rpc, "loop", None) if self.rpc else None
            try:
                if self.rpc:
                    self.rpc.close()
            except Exception:
                pass
            try:
                if loop is not None and not loop.is_closed():
                    loop.close()
            except Exception:
                pass
            self.rpc = None
        try:
            self._external_image_cache.clear()
            self._external_image_fail_ts.clear()
        except Exception:
            pass
        try:
            srv = getattr(self, "_cover_proxy_server", None)
            if srv is not None:
                try:
                    srv.shutdown()
                except Exception:
                    pass
                try:
                    srv.server_close()
                except Exception:
                    pass
        except Exception:
            pass
        self._cover_proxy_server = None
        self._cover_proxy_thread = None
        self._cover_proxy_port = None
        try:
            with self._cover_proxy_lock:
                self._cover_proxy_map.clear()
        except Exception:
            pass
