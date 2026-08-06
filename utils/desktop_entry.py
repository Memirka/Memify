"""Linux application-menu registration (a .desktop file), Linux only.

Distributed as a single portable binary rather than through a package
manager, so nothing ever installs an entry into the desktop's app launcher
("Пуск"/Application Menu/whatever the DE calls it) automatically — the only
way to open Memify is to know where the downloaded binary is and run it
directly. install_desktop_entry() writes that entry ourselves, at startup,
so the app shows up like any properly-installed one.

Categories=Network; is what freedesktop.org's menu spec categories mean by
"Internet" in the menus of every major desktop environment (KDE Plasma,
GNOME, XFCE, ...) — that's what actually controls which submenu it lands
in, not a literal "Internet" string anywhere in this file.
"""

import os
import shutil
import sys


def is_supported() -> bool:
    return sys.platform.startswith("linux")


def install_desktop_entry() -> None:
    """Writes ~/.local/share/applications/memify.desktop pointing at the
    currently running binary. Only for a frozen build — a dev running from
    source has no single executable to point Exec= at, and registering
    `python main.py` (plus whichever venv happened to launch it) in
    someone's application menu would be actively wrong. Safe to call on
    every launch: overwrites unconditionally, which is cheap and keeps
    Exec=/Icon= correct if the binary's own path ever changes (e.g. a
    fresh download placed somewhere new).
    """
    if not is_supported() or not getattr(sys, "frozen", False):
        return
    try:
        from config import APP_ICON

        exe_path = sys.executable
        apps_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "applications")
        os.makedirs(apps_dir, exist_ok=True)
        desktop_path = os.path.join(apps_dir, "memify.desktop")

        icon_path = _stable_icon_path(APP_ICON)

        entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Memify\n"
            "Comment=Memify music player\n"
            f'Exec="{exe_path}"\n'
            f"Icon={icon_path}\n"
            "Categories=Network;\n"
            "Terminal=false\n"
        )
        with open(desktop_path, "w", encoding="utf-8") as f:
            f.write(entry)
        os.chmod(desktop_path, 0o755)
    except Exception:
        pass


def _stable_icon_path(app_icon: str) -> str:
    """APP_ICON, for a PyInstaller onefile build, lives under sys._MEIPASS
    — a temp directory the bootloader extracts on launch and deletes again
    on exit. A .desktop file pointing Icon= there would show a broken icon
    the moment Memify isn't actually running. Copy it once to a location
    that outlives the process instead, and point there."""
    if not app_icon or not os.path.isfile(app_icon):
        return app_icon
    icons_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "icons")
    os.makedirs(icons_dir, exist_ok=True)
    dest = os.path.join(icons_dir, "memify" + os.path.splitext(app_icon)[1])
    try:
        shutil.copyfile(app_icon, dest)
    except Exception:
        return app_icon
    return dest
