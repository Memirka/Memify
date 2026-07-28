"""Self-update: check the server for a newer build, download it, and swap
the running executable for it. Only meaningful for a frozen (PyInstaller)
build — running from source has no single executable file to replace, so
every entry point here is a no-op in that case.
"""

import os
import sys
import subprocess
import tempfile

import requests


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
        return None
    try:
        resp = requests.get(
            f"{server_url}/app/update_check",
            params={"version": current_version, "platform": platform_tag()},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not data.get("update_available"):
        return None

    download_url = (data.get("download_url") or "").strip()
    if not download_url:
        return None
    if not download_url.startswith("http"):
        download_url = server_url + download_url

    return {"latest_version": data.get("latest_version", ""), "download_url": download_url}


def download_dest_path() -> str:
    """Where the freshly-downloaded build should be saved before swapping in."""
    exe_name = os.path.basename(current_executable_path() or "Memify")
    return os.path.join(tempfile.gettempdir(), f"{exe_name}.update")


def apply_update_and_exit(new_file_path: str) -> None:
    """Swaps the running executable for the downloaded one. Caller must quit
    the Qt event loop / exit the process right after calling this."""
    target = current_executable_path()
    if not target or not os.path.isfile(new_file_path):
        return
    if sys.platform == "win32":
        _apply_update_windows(target, new_file_path)
    else:
        _apply_update_linux(target, new_file_path)


def _apply_update_linux(target: str, new_file_path: str) -> None:
    # A running process keeps its already-open inode, so replacing the path
    # it was launched from is safe on Linux — the new file just takes over
    # for the *next* launch.
    try:
        os.chmod(new_file_path, 0o755)
        os.replace(new_file_path, target)
    except Exception:
        pass


def _apply_update_windows(target: str, new_file_path: str) -> None:
    # Windows keeps the running .exe's file locked, so a tiny detached helper
    # script waits for this process to exit, then swaps the file in place.
    pid = os.getpid()
    bat_path = os.path.join(tempfile.gettempdir(), "memify_update.bat")
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
    except Exception:
        pass
