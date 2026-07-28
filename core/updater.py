"""Self-update: check the server for a newer build, download it, and swap
the running executable for it. Only meaningful for a frozen (PyInstaller)
build — running from source has no single executable file to replace, so
every entry point here is a no-op in that case.
"""

import os
import sys
import subprocess
import tempfile
import traceback
from datetime import datetime

import requests


def _log_path() -> str:
    try:
        from config import DATA_DIR
        return os.path.join(DATA_DIR, "update_log.txt")
    except Exception:
        return os.path.join(tempfile.gettempdir(), "memify_update_log.txt")


def log(msg: str) -> None:
    """Append a line to the persistent update log — this build has no
    console (console=False in the PyInstaller spec), so print() output is
    otherwise thrown away and self-update failures are impossible to debug."""
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def platform_tag() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def current_executable_path() -> str | None:
    if not is_frozen():
        return None
    return sys.executable


def check_update(server_url: str, current_version: str, timeout: float = 5.0) -> dict | None:
    """Returns {"latest_version": str, "download_url": str} if a newer build
    is available on the server, otherwise None (including on any network/
    server error — a failed check must never block startup)."""
    if not is_frozen():
        log("check_update: skipped, not a frozen build")
        return None
    log(f"check_update: querying {server_url} (current={current_version}, platform={platform_tag()})")
    try:
        resp = requests.get(
            f"{server_url}/app/update_check",
            params={"version": current_version, "platform": platform_tag()},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"check_update: request failed: {e!r}")
        return None

    log(f"check_update: server responded {data!r}")
    if not data.get("update_available"):
        return None

    download_url = (data.get("download_url") or "").strip()
    if not download_url:
        log("check_update: update_available but no download_url")
        return None
    if not download_url.startswith("http"):
        download_url = server_url + download_url

    return {"latest_version": data.get("latest_version", ""), "download_url": download_url}


def download_dest_path() -> str:
    """Where the freshly-downloaded build should be saved before swapping in.

    Deliberately placed next to the running executable (NOT in the system
    temp dir) — os.replace()/os.rename() (and the Windows `move` command)
    require source and destination to be on the same filesystem. The temp
    dir is very commonly a separate mount (e.g. tmpfs on Linux) from wherever
    the user actually keeps the app, which made the swap silently fail with
    a cross-device error every time and left the old build in place forever.
    """
    target = current_executable_path()
    if not target:
        return os.path.join(tempfile.gettempdir(), "Memify.update")
    exe_dir = os.path.dirname(target)
    exe_name = os.path.basename(target)
    return os.path.join(exe_dir, f".{exe_name}.update")


def apply_update_and_exit(new_file_path: str) -> tuple[bool, str]:
    """Swaps the running executable for the downloaded one. Returns
    (True, "") if the swap was (or, on Windows, will be) applied — the
    caller should only quit the app in that case; on failure the old build
    is still intact and startup should continue normally instead of closing
    for nothing. The second element is a human-readable error on failure."""
    target = current_executable_path()
    if not target:
        return False, "not a frozen build (no target executable)"
    if not os.path.isfile(new_file_path):
        return False, f"downloaded file missing: {new_file_path}"

    log(f"apply_update_and_exit: target={target} new_file={new_file_path} "
        f"new_size={os.path.getsize(new_file_path)}")
    if sys.platform == "win32":
        return _apply_update_windows(target, new_file_path)
    return _apply_update_linux(target, new_file_path)


def _apply_update_linux(target: str, new_file_path: str) -> tuple[bool, str]:
    # A running process keeps its already-open inode, so replacing the path
    # it was launched from is safe on Linux — the new file just takes over
    # for the *next* launch.
    try:
        os.chmod(new_file_path, 0o755)
        os.replace(new_file_path, target)
        log("apply_update_and_exit: linux swap OK")
        return True, ""
    except Exception as e:
        err = f"{e!r}\n{traceback.format_exc()}"
        log(f"apply_update_and_exit: linux swap FAILED: {err}")
        return False, str(e)


def _apply_update_windows(target: str, new_file_path: str) -> tuple[bool, str]:
    # Windows keeps the running .exe's file locked, so a tiny detached helper
    # script waits for this process to exit, then swaps the file in place.
    pid = os.getpid()
    bat_path = os.path.join(os.path.dirname(target), "memify_update.bat")
    script = (
        "@echo off\n"
        ":wait\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\n'
        "if not errorlevel 1 (\n"
        "  timeout /t 1 /nobreak >NUL\n"
        "  goto wait\n"
        ")\n"
        f'move /Y "{new_file_path}" "{target}"\n'
        'del "%~f0"\n'
    )
    try:
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(script)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(["cmd.exe", "/c", bat_path], creationflags=creationflags)
        log(f"apply_update_and_exit: windows helper launched ({bat_path})")
        return True, ""
    except Exception as e:
        err = f"{e!r}\n{traceback.format_exc()}"
        log(f"apply_update_and_exit: windows helper launch FAILED: {err}")
        return False, str(e)
