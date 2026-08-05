"""MPRIS2 D-Bus service (Linux only).

Why this exists: utils/media_keys.py catches physical media keys via pynput,
which on Linux only sees literal XF86Audio* keysyms. A wired headset's inline
buttons usually register as exactly that, so they already work. Bluetooth
headset buttons go over the AVRCP profile instead, and modern Linux desktops
(KDE/GNOME + BlueZ + PipeWire — this dev machine included) route AVRCP
commands to whichever application is registered as the active MPRIS2 player
on the session D-Bus, not to synthetic key events. Memify wasn't registering
as one, so Bluetooth headset buttons either did nothing or controlled some
other MPRIS-aware app. This module makes it one. (Windows already forwards
AVRCP as standard virtual media keys system-wide, which pynput catches fine
— nothing to fix there, hence Linux-only.)

Hand-rolled on jeepney (pure Python, no system libdbus/GObject headers to
build against) instead of a ready-made MPRIS package: the obvious PyPI
option, mpris-server, depends on pydbus + PyGObject, both C extensions that
are painful to get into a single-file PyInstaller build. jeepney has no
"MPRIS service" helper, so the D-Bus method/property dispatch below is done
by hand against its low-level Message API.
"""

import sys
import threading

from PyQt6.QtCore import QObject, pyqtSignal, Qt

try:
    from jeepney import (
        DBusAddress, HeaderFields, MessageType, new_error, new_method_return,
        new_signal,
    )
    from jeepney.bus_messages import DBusNameFlags, message_bus
    from jeepney.io.threading import DBusRouter, Proxy, ReceiveStopped, open_dbus_connection
    _JEEPNEY_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - Linux-only optional dependency
    _JEEPNEY_IMPORT_ERROR = e


BUS_NAME = "org.mpris.MediaPlayer2.memify"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_PROPERTIES = "org.freedesktop.DBus.Properties"
IFACE_INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"

_INTROSPECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<node>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg name="xml_data" type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="property_name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="out"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface_name" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


def is_supported() -> bool:
    return _JEEPNEY_IMPORT_ERROR is None and sys.platform.startswith("linux")


class MPRISService(QObject):
    """Registers org.mpris.MediaPlayer2.memify on the session bus and
    forwards Play/Pause/Next/Previous/Stop calls (from Bluetooth headset
    buttons via AVRCP, desktop "now playing" widgets, etc.) into the same
    player entry points utils/media_keys.py already uses — from the
    player's point of view, a headset button and a keyboard media key are
    just two different sources of the same request."""

    play_pause_requested = pyqtSignal()
    play_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    next_requested = pyqtSignal()
    prev_requested = pyqtSignal()
    stop_requested = pyqtSignal()

    def __init__(self, player_controller, parent=None):
        super().__init__(parent)
        self.player_controller = player_controller
        self._conn = None
        self._thread = None
        self._running = False
        self._track_counter = 0
        self._current_track_id = "/org/mpris/MediaPlayer2/Track/0"
        self._playback_status = "Stopped"
        self._metadata: dict = {}

        self.play_pause_requested.connect(self._handle_play_pause, Qt.ConnectionType.QueuedConnection)
        self.play_requested.connect(self._handle_play, Qt.ConnectionType.QueuedConnection)
        self.pause_requested.connect(self._handle_pause, Qt.ConnectionType.QueuedConnection)
        self.next_requested.connect(self._handle_next, Qt.ConnectionType.QueuedConnection)
        self.prev_requested.connect(self._handle_prev, Qt.ConnectionType.QueuedConnection)
        self.stop_requested.connect(self._handle_stop, Qt.ConnectionType.QueuedConnection)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if not is_supported():
            return False
        try:
            self._conn = open_dbus_connection(bus="SESSION")
            with DBusRouter(self._conn) as router:
                reply = Proxy(message_bus, router, timeout=5).RequestName(
                    BUS_NAME, DBusNameFlags.do_not_queue
                )
            if reply[0] != 1:  # not DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER
                print(f"MPRIS: could not claim {BUS_NAME} (reply={reply[0]}), skipping")
                self._conn.close()
                self._conn = None
                return False
        except Exception as e:
            print(f"MPRIS: failed to connect to session bus: {e}")
            self._conn = None
            return False

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    # ── State pushed in from MusicApp's player callbacks ────────────────────

    def update_track(self, track: dict, artist: dict, album: dict):
        self._track_counter += 1
        self._current_track_id = f"/org/mpris/MediaPlayer2/Track/{self._track_counter}"
        title = (track or {}).get("title") or "Неизвестно"
        artist_name = (track or {}).get("artist_name") or (artist or {}).get("artist") or ""
        album_title = (track or {}).get("_real_album_title") or (album or {}).get("title") or ""
        length_us = int(self.player_controller.get_duration()) * 1000

        self._metadata = {
            "mpris:trackid": ("o", self._current_track_id),
            "mpris:length": ("x", length_us),
            "xesam:title": ("s", title),
            "xesam:artist": ("as", [artist_name] if artist_name else []),
            "xesam:album": ("s", album_title),
        }
        self._emit_properties_changed(IFACE_PLAYER, {"Metadata": ("a{sv}", self._metadata)})

    def update_playback_state(self, is_playing: bool):
        self._playback_status = "Playing" if is_playing else "Paused"
        self._emit_properties_changed(
            IFACE_PLAYER, {"PlaybackStatus": ("s", self._playback_status)}
        )

    def update_position(self, position_ms: int, duration_ms: int):
        # Per the MPRIS spec, Position is deliberately NOT sent via
        # PropertiesChanged (it changes constantly) — clients are expected
        # to poll Get("Position") themselves, which _get_property() below
        # always computes fresh from the live player position.
        pass

    # ── Control signal handlers (run on the GUI thread) ─────────────────────
    # Same entry points utils/media_keys.py's MediaKeysHandler calls.

    def _handle_play_pause(self):
        self.player_controller.toggle_playback()

    def _handle_play(self):
        if not self.player_controller.is_playing():
            self.player_controller.toggle_playback()

    def _handle_pause(self):
        if self.player_controller.is_playing():
            self.player_controller.toggle_playback()

    def _handle_next(self):
        self.player_controller.play_next()

    def _handle_prev(self):
        self.player_controller.play_prev()

    def _handle_stop(self):
        self.player_controller.stop()

    # ── D-Bus server loop ────────────────────────────────────────────────────

    def _serve(self):
        try:
            while self._running:
                try:
                    msg = self._conn.receive()
                except ReceiveStopped:
                    break
                except Exception as e:
                    if self._running:
                        print(f"MPRIS: receive error: {e}")
                    break
                if msg.header.message_type == MessageType.method_call:
                    self._dispatch(msg)
        finally:
            self._running = False

    def _dispatch(self, msg):
        fields = msg.header.fields
        interface = fields.get(HeaderFields.interface, "")
        member = fields.get(HeaderFields.member, "")
        try:
            reply = self._handle_call(interface, member, msg)
        except Exception as e:
            reply = new_error(msg, "org.freedesktop.DBus.Error.Failed", "s", (str(e),))
        if reply is not None:
            try:
                self._conn.send(reply)
            except Exception as e:
                print(f"MPRIS: failed to send reply: {e}")

    def _handle_call(self, interface: str, member: str, msg):
        if interface == IFACE_INTROSPECTABLE and member == "Introspect":
            return new_method_return(msg, "s", (_INTROSPECT_XML,))

        if interface == IFACE_PROPERTIES:
            if member == "Get":
                target_iface, prop_name = msg.body
                value = self._get_property(target_iface, prop_name)
                return new_method_return(msg, "v", (value,))
            if member == "GetAll":
                (target_iface,) = msg.body
                props = self._get_all_properties(target_iface)
                return new_method_return(msg, "a{sv}", (props,))
            if member == "Set":
                # Nothing user-settable is exposed (no volume/loop/shuffle
                # control here) — accept silently rather than error, since
                # some clients probe this speculatively.
                return new_method_return(msg)

        if interface == IFACE_ROOT:
            if member in ("Raise", "Quit"):
                # CanRaise/CanQuit are both False, so well-behaved clients
                # won't call these — no-op reply rather than an error for
                # the odd client that tries anyway.
                return new_method_return(msg)

        if interface == IFACE_PLAYER:
            signal_map = {
                "PlayPause": self.play_pause_requested,
                "Play": self.play_requested,
                "Pause": self.pause_requested,
                "Next": self.next_requested,
                "Previous": self.prev_requested,
                "Stop": self.stop_requested,
            }
            if member in signal_map:
                signal_map[member].emit()
                return new_method_return(msg)

        return new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod",
                          "s", (f"Unknown method {interface}.{member}",))

    # ── Property table ───────────────────────────────────────────────────────

    def _root_properties(self) -> dict:
        return {
            "CanQuit": ("b", False),
            "CanRaise": ("b", False),
            "HasTrackList": ("b", False),
            "Identity": ("s", "Memify"),
            "SupportedUriSchemes": ("as", []),
            "SupportedMimeTypes": ("as", []),
        }

    def _player_properties(self) -> dict:
        has_album = bool(self.player_controller.current_playing_album)
        position_us = int(self.player_controller.get_current_position()) * 1000
        return {
            "PlaybackStatus": ("s", self._playback_status),
            "Metadata": ("a{sv}", self._metadata),
            "Position": ("x", position_us),
            "CanGoNext": ("b", has_album),
            "CanGoPrevious": ("b", has_album),
            "CanPlay": ("b", has_album),
            "CanPause": ("b", has_album),
            "CanSeek": ("b", False),
            "CanControl": ("b", True),
        }

    def _get_all_properties(self, interface: str) -> dict:
        if interface == IFACE_ROOT:
            return self._root_properties()
        if interface == IFACE_PLAYER:
            return self._player_properties()
        return {}

    def _get_property(self, interface: str, name: str):
        props = self._get_all_properties(interface)
        if name not in props:
            raise KeyError(f"No property {interface}.{name}")
        return props[name]

    def _emit_properties_changed(self, interface: str, changed: dict):
        if not self._conn or not self._running:
            return
        emitter = DBusAddress(OBJECT_PATH, interface=IFACE_PROPERTIES)
        signal = new_signal(
            emitter, "PropertiesChanged", "sa{sv}as",
            (interface, changed, []),
        )
        try:
            self._conn.send(signal)
        except Exception as e:
            print(f"MPRIS: failed to emit PropertiesChanged: {e}")
