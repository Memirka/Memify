import json
import os
from typing import Optional, Dict

import requests

from config import DATA_DIR, LOGIN_FILE, SERVER_URL

DEFAULT_PLAYER_DATA = {
    "album_subscriptions": [],
    "follow_order": [],
    "last_played": {},
    "subscriptions": [],
    "liked_tracks": [],
    "playlists": [],
    "playlist_subscriptions": [],
    "app_settings": {},
    "listen_stats": {},
    "avatar_data": None,
    "avatar_filename": None,
    "display_name": None,
    "account_id": None,
}


class AccountManager:
    def __init__(self):
        self.data_dir = DATA_DIR
        self.login_file = LOGIN_FILE
        self.active_login: Optional[str] = None
        self._password: Optional[str] = None
        # None (not {}) until a fetch/save actually succeeds — fetch_player_data()
        # falls back to this on a failed load, and None vs. {} is the signal
        # the caller (ui/main_window.py) uses to tell "never got real data
        # yet" apart from "this account genuinely has nothing saved".
        self._player_cache: Optional[Dict] = None
        self.last_error: str = ""

    # === validation ===
    def _is_valid_login(self, login: str) -> bool:
        if not login or len(login) > 20:
            return False
        return login.isascii() and all(ch.isalnum() for ch in login)

    def _is_valid_password(self, password: str) -> bool:
        return bool(password) and len(password) <= 100 and password.isascii()

    # === login.json helpers ===
    def has_valid_login_file(self) -> bool:
        return self._is_valid_credentials(self._read_login_file())

    def _read_login_file(self) -> dict:
        try:
            if not os.path.exists(self.login_file):
                return {}
            with open(self.login_file, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _is_valid_credentials(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        login = data.get("login")
        password = data.get("password")
        return (
            isinstance(login, str)
            and isinstance(password, str)
            and self._is_valid_login(login.strip())
            and self._is_valid_password(password.strip())
        )

    def _write_login_file(self, login: str, password: str):
        try:
            os.makedirs(os.path.dirname(self.login_file), exist_ok=True)
            with open(self.login_file, "w", encoding="utf-8") as f:
                json.dump({"login": login, "password": password}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"login.json write error: {e}")

    # === high level actions ===
    def activate_saved_login(self) -> bool:
        data = self._read_login_file()
        if not self._is_valid_credentials(data):
            return False
        login = data.get("login", "").strip()
        password = data.get("password", "").strip()
        return self.login(login, password, persist=False)

    def register(self, login: str, password: str, persist: bool = True) -> bool:
        self.last_error = ""
        login = (login or "").strip()
        password = (password or "").strip()
        if not self._is_valid_login(login) or not self._is_valid_password(password):
            return False
        exists = self._check_exists_with_server(login)
        if exists is True:
            self.last_error = "user_exists"
            return False
        if exists is None:
            # If we cannot determine, fall back to server-side handling.
            pass
        if not self._register_with_server(login, password):
            return False
        self._set_active(login, password, persist=persist)
        return True

    def login(self, login: str, password: str, persist: bool = True) -> bool:
        self.last_error = ""
        login = (login or "").strip()
        password = (password or "").strip()
        if not self._is_valid_login(login) or not self._is_valid_password(password):
            return False
        if not self._verify_with_server(login, password):
            return False
        self._set_active(login, password, persist=persist)
        return True

    def _set_active(self, login: str, password: str, persist: bool):
        self.active_login = login
        self._password = password
        if persist:
            self._write_login_file(login, password)
        self._player_cache = None
        self.last_error = ""

    def logout(self) -> bool:
        """Clear current credentials and wipe the persisted login file."""
        removed = False
        try:
            if os.path.exists(self.login_file):
                os.remove(self.login_file)
                removed = True
        except Exception as e:
            print(f"login.json delete error: {e}")
        self.active_login = None
        self._password = None
        self._player_cache = None
        self.last_error = ""
        return removed or (not os.path.exists(self.login_file))

    def delete_account(self) -> bool:
        """Удалить аккаунт на сервере и очистить локальные данные."""
        if not self.active_login or not self._password:
            self.last_error = "no_active_login"
            return False
        try:
            resp = self._post("/auth/delete", {"login": self.active_login, "password": self._password}, timeout=6)
            if resp.status_code != 200:
                self.last_error = f"status:{resp.status_code}"
                return False
            data = resp.json() if resp.content else {}
            if not data.get("ok"):
                self.last_error = data.get("error", "delete_failed")
                return False
        except Exception as e:
            print(f"Delete account request failed: {e}")
            self.last_error = "network_error"
            return False
        # Удаляем локальный login.json и сбрасываем состояние.
        self.logout()
        return True

    def change_password(self, current_password: str, new_password: str, persist: bool = True) -> bool:
        self.last_error = ""
        login = (self.active_login or "").strip()
        current_password = (current_password or "").strip()
        new_password = (new_password or "").strip()
        if not login:
            self.last_error = "no_active_login"
            return False
        if not self._is_valid_password(current_password) or not self._is_valid_password(new_password):
            self.last_error = "invalid_password"
            return False
        try:
            resp = self._post(
                "/auth/change_password",
                {"login": login, "password": current_password, "new_password": new_password},
                timeout=6,
            )
            if resp.status_code != 200:
                self.last_error = "invalid_credentials" if resp.status_code == 401 else f"status:{resp.status_code}"
                return False
            data = resp.json() if resp.content else {}
            if not data.get("ok"):
                self.last_error = data.get("error", "change_failed")
                return False
        except Exception as e:
            print(f"Change password request failed: {e}")
            self.last_error = "network_error"
            return False
        self._password = new_password
        if persist:
            self._write_login_file(login, new_password)
        return True

    def get_login_data(self) -> dict:
        if not self.active_login:
            return {}
        return {"login": self.active_login, "password": self._password}

    # === server comms ===
    def _post(self, path: str, payload: dict, timeout: int = 5):
        url = f"{SERVER_URL}{path}"
        return requests.post(url, json=payload, timeout=timeout)

    def _register_with_server(self, login: str, password: str) -> bool:
        try:
            resp = self._post("/auth/register", {"login": login, "password": password}, timeout=5)
            if resp.status_code == 409:
                self.last_error = "user_exists"
                return False
            if resp.status_code != 200:
                self.last_error = f"status:{resp.status_code}"
                return False
            data = resp.json() if resp.content else {}
            ok = bool(data.get("ok"))
            if not ok:
                self.last_error = data.get("error", "register_failed")
            return ok
        except Exception as e:
            print(f"Register request failed: {e}")
            self.last_error = "network_error"
            return False

    def _check_exists_with_server(self, login: str) -> Optional[bool]:
        try:
            resp = self._post("/auth/exists", {"login": login}, timeout=5)
            if resp.status_code != 200:
                return None
            data = resp.json() if resp.content else {}
            return bool(data.get("exists"))
        except Exception:
            return None

    def _verify_with_server(self, login: str, password: str) -> bool:
        try:
            resp = self._post("/auth/verify", {"login": login, "password": password}, timeout=5)
            if resp.status_code != 200:
                self.last_error = "invalid_credentials" if resp.status_code == 401 else f"status:{resp.status_code}"
                return False
            data = resp.json() if resp.content else {}
            ok = bool(data.get("ok"))
            if not ok:
                self.last_error = data.get("error", "auth_failed")
            return ok
        except Exception as e:
            print(f"Verify request failed: {e}")
            self.last_error = "network_error"
            return False

    # === public users ===
    def search_users(self, query: str, limit: int = 8) -> list:
        query = (query or "").strip()
        if not query:
            return []
        try:
            resp = self._post("/users/search", {"query": query, "limit": limit}, timeout=5)
            if resp.status_code != 200:
                return []
            data = resp.json() if resp.content else {}
            items = data.get("items")
            return items if isinstance(items, list) else []
        except Exception as e:
            print(f"User search failed: {e}")
            return []

    def get_public_profile(self, login: str = "", account_id: str = "") -> dict:
        login = (login or "").strip()
        account_id = (account_id or "").strip()
        if not login and not account_id:
            return {}
        try:
            resp = self._post("/users/profile", {"login": login, "account_id": account_id}, timeout=5)
            if resp.status_code != 200:
                return {}
            data = resp.json() if resp.content else {}
            return data if data.get("ok") else {}
        except Exception as e:
            print(f"Public profile fetch failed: {e}")
            return {}

    def is_server_reachable(self, timeout: float = 3.0) -> bool:
        """Lightweight connectivity probe — used at startup when there's no
        saved login to naturally test reachability with (see main.py's
        offline-mode branch): any response at all means the network and
        server are both up; any exception (timeout, DNS failure, connection
        refused, ...) means they're not."""
        try:
            requests.get(SERVER_URL, timeout=timeout)
            return True
        except Exception:
            return False

    # === player data ===
    def fetch_player_data(self) -> dict | None:
        """None means "couldn't get real data" (network/server error, and
        nothing fetched/saved yet this session to fall back on) — the caller
        (ui/main_window.py's _fetch_player_data) must leave whatever's
        already loaded (local cache) alone in that case rather than treat it
        as a genuinely-empty account. Returning dict(DEFAULT_PLAYER_DATA)
        here on a transient failure used to look identical to "this account
        really has no likes/playlists/subscriptions" to that caller, which
        would then get persisted right back to the server as truth the next
        time anything triggered a save — silently wiping real data on any
        blip during the very first fetch of a session."""
        if not self.active_login or not self._password:
            return dict(DEFAULT_PLAYER_DATA)
        try:
            resp = self._post("/player/load", {"login": self.active_login, "password": self._password}, timeout=5)
            if resp.status_code != 200:
                return self._player_cache
            data = resp.json() if resp.content else {}
            payload = {k: data.get(k, DEFAULT_PLAYER_DATA[k]) for k in DEFAULT_PLAYER_DATA}
            self._player_cache = payload
            return payload
        except Exception as e:
            print(f"Player data fetch failed: {e}")
            return self._player_cache

    def save_player_data(self, updates: dict) -> bool:
        if not self.active_login or not self._password or not isinstance(updates, dict):
            return False
        payload = {"login": self.active_login, "password": self._password}
        payload.update({k: v for k, v in updates.items() if k in DEFAULT_PLAYER_DATA})
        try:
            resp = self._post("/player/save", payload, timeout=5)
            if resp.status_code != 200:
                print(f"Player data save failed with status {resp.status_code}")
                return False
            data = resp.json() if resp.content else {}
            if data.get("ok"):
                self._player_cache = {k: data.get(k, DEFAULT_PLAYER_DATA[k]) for k in DEFAULT_PLAYER_DATA}
                return True
            return False
        except Exception as e:
            print(f"Player data save failed: {e}")
            return False
