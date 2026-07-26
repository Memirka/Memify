# PyInstaller spec for Memify.
# Build on the target OS itself (PyInstaller does not cross-compile):
#   Windows:  pyinstaller memify.spec
#   Linux:    pyinstaller memify.spec
# Output goes to dist/Memify (a single self-contained executable per OS —
# users just download and run it, no Python or libraries required).

import sys

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = [
    ("assets", "assets"),
]

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
    binaries=qt_binaries,
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
