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
    # Windows keeps the running .exe's file locked. The helper retries the
    # replacement itself; relying on tasklist/PID polling can wait forever
    # after a process shutdown or a PID reuse.
    exe_dir = os.path.dirname(target)
    bat_path = os.path.join(exe_dir, "memify_update.bat")
    backup_path = os.path.join(exe_dir, f"{os.path.basename(target)}.old")
    helper_log_path = os.path.join(exe_dir, "memify_update.log")
    script = (
        "@echo off\n"
        "setlocal\n"
        'set "UPDATE=%MEMIFY_UPDATE_FILE%"\n'
        'set "TARGET=%MEMIFY_TARGET_FILE%"\n'
        'set "BACKUP=%MEMIFY_BACKUP_FILE%"\n'
        'set "LOG=%MEMIFY_UPDATE_LOG%"\n'
        'set "HELPER=%MEMIFY_UPDATE_HELPER%"\n'
        'echo [%date% %time%] updater started >> "%LOG%"\n'
        'echo [%date% %time%] update="%UPDATE%" target="%TARGET%" >> "%LOG%"\n'
        'if not exist "%UPDATE%" echo [%date% %time%] update file missing before retry loop >> "%LOG%"\n'
        'if not exist "%UPDATE%" goto done\n'
        'for %%A in ("%UPDATE%") do echo [%date% %time%] update size=%%~zA >> "%LOG%"\n'
        # Give QApplication and the PyInstaller parent process a moment to
        # exit, then let file operations decide when the replacement is safe.
        # ping works without a console, unlike timeout in a detached process.
        "ping 127.0.0.1 -n 3 >NUL\n"
        "set attempt=0\n"
        ":move_retry\n"
        "set /a attempt+=1\n"
        'if not exist "%UPDATE%" echo [%date% %time%] update file disappeared on attempt %attempt% >> "%LOG%"\n'
        'if not exist "%UPDATE%" goto done\n'
        'del /F /Q "%BACKUP%" >NUL 2>&1\n'
        'if not exist "%TARGET%" goto install_update\n'
        'move /Y "%TARGET%" "%BACKUP%" >NUL 2>&1\n'
        "if errorlevel 1 goto target_locked\n"
        ":install_update\n"
        'move /Y "%UPDATE%" "%TARGET%" >NUL 2>&1\n'
        "if errorlevel 1 goto install_failed\n"
        'del /F /Q "%BACKUP%" >NUL 2>&1\n'
        'echo [%date% %time%] update applied >> "%LOG%"\n'
        "goto done\n"
        ":target_locked\n"
        'echo [%date% %time%] replace attempt %attempt% failed: target is still locked >> "%LOG%"\n'
        "goto retry_wait\n"
        ":install_failed\n"
        'echo [%date% %time%] replace attempt %attempt% failed: could not move update into place >> "%LOG%"\n'
        'if exist "%BACKUP%" move /Y "%BACKUP%" "%TARGET%" >NUL 2>&1\n'
        ":retry_wait\n"
        "if %attempt% geq 180 goto failed\n"
        "ping 127.0.0.1 -n 2 >NUL\n"
        "goto move_retry\n"
        ":failed\n"
        'echo [%date% %time%] update failed; cleaning temporary update files if target still exists >> "%LOG%"\n'
        'if exist "%TARGET%" del /F /Q "%UPDATE%" >NUL 2>&1\n'
        'if exist "%TARGET%" del /F /Q "%BACKUP%" >NUL 2>&1\n'
        ":done\n"
        'if defined HELPER del /F /Q "%HELPER%" >NUL 2>&1\n'
        'del /F /Q "%~f0" >NUL 2>&1\n'
    )
    try:
        with open(bat_path, "w", encoding="ascii") as f:
            f.write(script)
        helper_env = os.environ.copy()
        helper_env.update(
            {
                "MEMIFY_UPDATE_FILE": os.path.abspath(new_file_path),
                "MEMIFY_TARGET_FILE": os.path.abspath(target),
                "MEMIFY_BACKUP_FILE": os.path.abspath(backup_path),
                "MEMIFY_UPDATE_LOG": os.path.abspath(helper_log_path),
                "MEMIFY_UPDATE_HELPER": os.path.abspath(bat_path),
            }
        )
        # The PyInstaller parent/child process arrangement can terminate a
        # normal child cmd.exe as the app exits. Put the helper in its own
        # detached process group so it survives long enough to copy the new
        # executable. CREATE_NO_WINDOW is deliberately retained: with
        # DETACHED_PROCESS it is ignored by Windows, but harmless.
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
        try:
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", "call", os.path.basename(bat_path)],
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                cwd=exe_dir,
                env=helper_env,
            )
        except OSError:
            # Some launchers put the app into a Windows job that does not
            # allow breakaway. Fall back to a detached helper rather than
            # failing the whole update.
            creationflags &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", "call", os.path.basename(bat_path)],
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                cwd=exe_dir,
                env=helper_env,
            )
        log(f"apply_update_and_exit: windows helper launched ({bat_path})")
        return True, ""
    except Exception as e:
        err = f"{e!r}\n{traceback.format_exc()}"
        log(f"apply_update_and_exit: windows helper launch FAILED: {err}")
        return False, str(e)


def find_pending_update() -> str | None:
    """Path to a previously-downloaded update that never got swapped in
    (e.g. the Windows helper batch script failed to launch or run to
    completion last time) — or None if there's nothing pending.

    Reusing it lets the app finish an interrupted update on the next launch
    instead of leaving `.exe.update`/`memify_update.bat` on disk forever and
    silently re-downloading the same build every single startup.
    """
    pending = download_dest_path()
    if not os.path.isfile(pending):
        return None
    if not _looks_like_complete_build(pending):
        # Most likely the app was killed mid-download (the only path that
        # doesn't already clean up a partial file — see
        # DownloadWorker._cleanup_partial_file for the normal-failure case).
        # Applying a truncated file would replace a working build with a
        # broken one, so discard it and let a normal update check re-download.
        log(f"find_pending_update: discarding corrupt/truncated leftover {pending}")
        try:
            os.remove(pending)
        except Exception:
            pass
        return None
    return pending


def _looks_like_complete_build(path: str) -> bool:
    try:
        # A real Memify build is well over 100MB; anything far smaller is a
        # truncated download, not a valid executable.
        if os.path.getsize(path) < 1_000_000:
            return False
        if sys.platform == "win32":
            with open(path, "rb") as f:
                return f.read(2) == b"MZ"
        return True
    except Exception:
        return False
