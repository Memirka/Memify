# PyInstaller spec for Memify.
# Build on the target OS itself (PyInstaller does not cross-compile):
#   Windows:  pyinstaller memify.spec
#   Linux:    pyinstaller memify.spec
# Output goes to dist/Memify (a single self-contained executable per OS —
# users just download and run it, no Python or libraries required).

import os
import sys
import glob

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = [
    ("assets", "assets"),
]
binaries = []


def _find_vlc_files():
    """Locate libVLC's shared library + "plugins" folder on the BUILD
    machine, so PyInstaller can bundle them into the exe — python-vlc (in
    requirements.txt) is only a thin ctypes wrapper; the actual libVLC
    runtime has to come from wherever the CI step installed it
    (choco/apt, see .github/workflows/build.yml), not from pip. Without
    this, the built exe imports fine wherever VLC happens to already be
    installed system-wide (e.g. this dev machine) but fails on a clean
    end-user machine — which was the whole point of bundling.

    Windows: bundled fully — libVLC's Windows build is fairly
    self-contained, so this is the well-trodden, reliable path.

    Linux: bundled best-effort. Several VLC plugins (mp3/alsa/pulse codecs)
    dynamically link against other system libraries (libmpg123, libasound,
    libpulse, ...) that this does NOT chase down and bundle too — doing
    that reliably across arbitrary distros is its own large, fragile
    undertaking (glibc/ABI differences make bundled Linux .so files far
    less portable than bundled Windows DLLs to begin with). This works
    correctly on Debian/Ubuntu-family desktops, where those libraries are
    near-universal pre-existing packages — which is most desktop Linux
    users, just not an absolute guarantee for every distro.
    """
    found_binaries = []
    found_datas = []

    if sys.platform == "win32":
        candidates = [
            os.environ.get("VLC_PATH", ""),
            r"C:\Program Files\VideoLAN\VLC",
            r"C:\Program Files (x86)\VideoLAN\VLC",
        ]
        vlc_dir = next((c for c in candidates if c and os.path.isdir(c)), None)
        if not vlc_dir:
            print("!! memify.spec: VLC install not found (checked VLC_PATH env + standard "
                  "Program Files paths) — build will NOT have a working equalizer/player "
                  "on machines without VLC installed.")
            return found_binaries, found_datas
        for dll_name in ("libvlc.dll", "libvlccore.dll"):
            dll_path = os.path.join(vlc_dir, dll_name)
            if os.path.isfile(dll_path):
                found_binaries.append((dll_path, "."))
        plugins_dir = os.path.join(vlc_dir, "plugins")
        if os.path.isdir(plugins_dir):
            found_datas.append((plugins_dir, "plugins"))
    else:
        lib_globs = [
            "/usr/lib/x86_64-linux-gnu/libvlc.so*",
            "/usr/lib/x86_64-linux-gnu/libvlccore.so*",
            "/usr/lib/*/libvlc.so*",
            "/usr/lib/*/libvlccore.so*",
            "/usr/lib/libvlc.so*",
            "/usr/lib/libvlccore.so*",
        ]
        seen = set()
        for pattern in lib_globs:
            for path in glob.glob(pattern):
                if os.path.isfile(path) and path not in seen:
                    seen.add(path)
                    found_binaries.append((path, "."))
        plugin_dir_globs = [
            "/usr/lib/x86_64-linux-gnu/vlc/plugins",
            "/usr/lib/*/vlc/plugins",
            "/usr/lib/vlc/plugins",
        ]
        plugins_dir = next(
            (p for pat in plugin_dir_globs for p in glob.glob(pat) if os.path.isdir(p)), None
        )
        if plugins_dir:
            found_datas.append((plugins_dir, "plugins"))
        if not found_binaries or not plugins_dir:
            print("!! memify.spec: VLC install not found (checked standard apt/system lib "
                  "paths) — build will NOT have a working equalizer/player on machines "
                  "without VLC installed.")

    return found_binaries, found_datas


_vlc_binaries, _vlc_datas = _find_vlc_files()
binaries += _vlc_binaries
datas += _vlc_datas

# PyQt6 ships its own plugins (platform, multimedia/ffmpeg backend, etc.) that
# plain import-analysis won't find on its own — pull them in explicitly, then
# drop the pieces the app doesn't use (Qt3D/QML/WebView/SQL drivers/
# translations/.sip source stubs...) — collect_all grabs the whole PyQt6
# distribution, most of which this app has no use for and just bloats the build.
#
# Note: we deliberately ignore collect_all's third return value (hiddenimports —
# every PyQt6 submodule: QtBluetooth, QtDesigner, Qt3D, QtWebEngine...). The
# modules this app actually imports (QtCore/QtGui/QtWidgets/QtMultimedia) are
# already found by PyInstaller's normal static import analysis; forcing the
# rest in via hiddenimports is what was dragging in and building all of them.
qt_datas, qt_binaries, _qt_hidden_unused = collect_all("PyQt6")

_NEEDED_PLUGIN_DIRS = {"platforms", "multimedia", "imageformats", "iconengines", "styles"}


def _keep_qt_entry(entry) -> bool:
    dest = entry[1].replace("\\", "/")
    parts = dest.split("/")
    if "bindings" in parts or "qml" in parts or "translations" in parts:
        return False
    if "plugins" in parts:
        idx = parts.index("plugins")
        if idx + 1 < len(parts) and parts[idx + 1] not in _NEEDED_PLUGIN_DIRS:
            return False
    return True


qt_datas = [d for d in qt_datas if _keep_qt_entry(d)]
qt_binaries = [b for b in qt_binaries if _keep_qt_entry(b)]
datas += qt_datas
binaries += qt_binaries

# Extra safety net: explicitly keep PyQt6's unused submodules out even if some
# hook chain tries to pull one back in transitively.
_UNUSED_QT_MODULES = [
    "PyQt6.QtBluetooth", "PyQt6.QtDBus", "PyQt6.QtDesigner", "PyQt6.QtHelp",
    "PyQt6.QtLocation", "PyQt6.QtMultimediaWidgets", "PyQt6.QtNfc",
    "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets", "PyQt6.QtPdf", "PyQt6.QtPdfWidgets",
    "PyQt6.QtPositioning", "PyQt6.QtPrintSupport", "PyQt6.QtQml", "PyQt6.QtQuick",
    "PyQt6.QtQuick3D", "PyQt6.QtQuickWidgets", "PyQt6.QtRemoteObjects",
    "PyQt6.QtSensors", "PyQt6.QtSerialPort", "PyQt6.QtSpatialAudio", "PyQt6.QtSql",
    "PyQt6.QtStateMachine", "PyQt6.QtSvg", "PyQt6.QtSvgWidgets", "PyQt6.QtTest",
    "PyQt6.QtTextToSpeech", "PyQt6.QtWebChannel", "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineQuick", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebSockets",
    "PyQt6.QtWebView", "PyQt6.QtXml", "PyQt6.Qt3DCore", "PyQt6.Qt3DRender",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_UNUSED_QT_MODULES,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# .ico only makes sense (and only exists) for the Windows build.
exe_icon = "assets/icons/memify.ico" if sys.platform == "win32" else None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Memify",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)
