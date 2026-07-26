import random
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices
from utils.format_utils import clean_title, format_duration


class PlayerController(QObject):
    currentUrlChanged = pyqtSignal(str)
    audioOutputsUpdated = pyqtSignal(list)
    audioOutputDeviceChanged = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)

        self._preferred_audio_output_id = None
        self._current_audio_output_id = None
        self._has_emitted_audio_device = False
        self._media_devices = QMediaDevices()
        self._media_devices.audioOutputsChanged.connect(self._handle_audio_outputs_changed)
        self._available_audio_outputs = self._collect_audio_outputs()

        self.current_playing_album = None
        self.current_playing_artist = None
        self.current_track = None
        self.current_track_idx = None
        self.track_durations = {}
        self.shuffle_enabled = False
        self.repeat_mode = "off"
        self.shuffled_indices = []
        self._manual_track_switch = False

        self.timer = QTimer()
        self.timer.setInterval(500)

        self.on_track_changed = None
        self.on_playback_state_changed = None
        self.on_position_changed = None
        self.on_duration_changed = None
        self.on_album_finished = None
        self.on_album_previous = None

    def setup_connections(self):
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.timer.timeout.connect(self._on_timer_timeout)

    def set_callbacks(self, track_changed=None, playback_state_changed=None,
                      position_changed=None, duration_changed=None,
                      album_finished=None, album_previous=None):
        self.on_track_changed = track_changed
        self.on_playback_state_changed = playback_state_changed
        self.on_position_changed = position_changed
        self.on_duration_changed = duration_changed
        self.on_album_finished = album_finished
        self.on_album_previous = album_previous

    # ── Audio device management ───────────────────────────────────────────────

    def get_audio_outputs(self) -> list:
        self._available_audio_outputs = self._collect_audio_outputs()
        return list(self._available_audio_outputs)

    def set_audio_output_device(self, device_id):
        self._preferred_audio_output_id = device_id or None
        return self._apply_audio_output_device()

    def _collect_audio_outputs(self) -> list:
        default = QMediaDevices.defaultAudioOutput()
        return [
            {
                "id": self._device_id(d),
                "description": d.description(),
                "is_default": d == default,
            }
            for d in QMediaDevices.audioOutputs()
        ]

    def _device_id(self, device) -> str | None:
        try:
            if not device or device.isNull():
                return None
            raw = bytes(device.id())
            return raw.decode("utf-8", errors="ignore") if raw else None
        except Exception:
            return None

    def _handle_audio_outputs_changed(self):
        self._available_audio_outputs = self._collect_audio_outputs()
        try:
            self.audioOutputsUpdated.emit(list(self._available_audio_outputs))
        except Exception:
            pass
        self._apply_audio_output_device()

    def _apply_audio_output_device(self) -> bool:
        try:
            target = None
            preferred_available = False
            if self._preferred_audio_output_id:
                for d in QMediaDevices.audioOutputs():
                    if self._device_id(d) == self._preferred_audio_output_id:
                        target = d
                        preferred_available = True
                        break
            if target is None:
                target = QMediaDevices.defaultAudioOutput()
            if not target or target.isNull():
                return False
            self.audio_output.setDevice(target)
            active_id = self._preferred_audio_output_id if preferred_available else None
            if self._current_audio_output_id != active_id or not self._has_emitted_audio_device:
                self._current_audio_output_id = active_id
                self._has_emitted_audio_device = True
                try:
                    self.audioOutputDeviceChanged.emit(active_id)
                except Exception:
                    pass
            return True
        except Exception as e:
            print(f"Audio device error: {e}")
            return False

    # ── Playback control ──────────────────────────────────────────────────────

    def set_album(self, album: dict, artist: dict):
        self.current_playing_album = album
        self.current_playing_artist = artist
        track_count = len(album.get("tracks", []))
        self.shuffled_indices = list(range(track_count))
        if self.shuffle_enabled:
            random.shuffle(self.shuffled_indices)
        self.current_track = None
        self.current_track_idx = None

    def play_track(self, index: int) -> bool:
        if not self.current_playing_album:
            return False
        if not self.shuffled_indices:
            track_count = len(self.current_playing_album.get("tracks", []))
            self.shuffled_indices = list(range(track_count))
            if self.shuffle_enabled:
                random.shuffle(self.shuffled_indices)
        if index >= len(self.shuffled_indices):
            return False
        try:
            self._manual_track_switch = True
            self.current_track = index
            self.current_track_idx = self.shuffled_indices[index]
            track = self.current_playing_album["tracks"][self.current_track_idx]

            from config import SERVER_URL
            track_url = SERVER_URL + track.get("url", "")
            # setSource on an active player triggers async pipeline re-use in GStreamer
            # (avoids the sync stop→ready state transition that blocks the main thread)
            self.player.setSource(QUrl(track_url))
            self.player.play()
            self.timer.start()

            if self.on_track_changed:
                self.on_track_changed(track, self.current_playing_artist, self.current_playing_album)
            if self.on_playback_state_changed:
                self.on_playback_state_changed(True)

            QTimer.singleShot(300, lambda: setattr(self, "_manual_track_switch", False))
            self.currentUrlChanged.emit(track_url)
            return True
        except Exception as e:
            print(f"play_track error: {e}")
            return False

    def toggle_playback(self):
        try:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
                if self.on_playback_state_changed:
                    self.on_playback_state_changed(False)
            else:
                if self.player.source().isEmpty():
                    if self.current_track is not None and self.current_playing_album:
                        self.play_track(self.current_track)
                    # no source and no album set — do nothing (avoids ghost ⏸ with no audio)
                else:
                    self.player.play()
                    if self.on_playback_state_changed:
                        self.on_playback_state_changed(True)
        except Exception as e:
            print(f"toggle_playback error: {e}")

    def play_next(self):
        if getattr(self, "_processing_next", False):
            return
        try:
            self._processing_next = True
            if not self.current_playing_album or not self.shuffled_indices:
                return
            if self.current_track is None:
                self.play_track(0)
                return
            if self.repeat_mode == "track":
                self.play_track(self.current_track)
                return
            next_pos = self.current_track + 1
            if next_pos < len(self.shuffled_indices):
                self.play_track(next_pos)
            elif self.repeat_mode == "album":
                self.play_track(0)
            else:
                handled = False
                try:
                    if self.on_album_finished:
                        handled = bool(self.on_album_finished(self.current_playing_artist, self.current_playing_album))
                except Exception:
                    pass
                if not handled:
                    self.stop()
        except Exception as e:
            print(f"play_next error: {e}")
        finally:
            QTimer.singleShot(200, lambda: setattr(self, "_processing_next", False))

    def play_prev(self):
        if getattr(self, "_processing_prev", False):
            return
        try:
            self._processing_prev = True
            if not self.current_playing_album or not self.shuffled_indices:
                return
            if self.current_track is None:
                self.play_track(len(self.shuffled_indices) - 1)
                return
            if self.repeat_mode == "track":
                self.play_track(self.current_track)
                return
            pos_ms = 0
            try:
                pos_ms = self.player.position()
            except Exception:
                pass
            if pos_ms < 2000:
                if self.repeat_mode == "off" and self.current_track == 0 and self.on_album_previous:
                    if self.on_album_previous(self.current_playing_artist, self.current_playing_album):
                        return
                prev = self.current_track - 1 if self.current_track > 0 else len(self.shuffled_indices) - 1
                self.play_track(prev)
            else:
                self.play_track(self.current_track)
        except Exception as e:
            print(f"play_prev error: {e}")
        finally:
            QTimer.singleShot(200, lambda: setattr(self, "_processing_prev", False))

    def stop(self):
        self.player.stop()
        self.timer.stop()
        if self.on_playback_state_changed:
            self.on_playback_state_changed(False)

    def seek_position(self, value: float):
        try:
            if self.player.duration() > 0:
                self.player.setPosition(int(self.player.duration() * value / 100))
        except Exception:
            pass

    def set_volume(self, value: int):
        try:
            self.audio_output.setVolume(value / 100.0)
        except Exception:
            pass

    def get_volume(self) -> int:
        try:
            return max(0, min(100, int(round(self.audio_output.volume() * 100))))
        except Exception:
            return 100

    def toggle_shuffle(self) -> bool:
        self.shuffle_enabled = not self.shuffle_enabled
        if self.current_playing_album:
            old_idx = self.current_track_idx
            track_count = len(self.current_playing_album["tracks"])
            self.shuffled_indices = list(range(track_count))
            if self.shuffle_enabled:
                random.shuffle(self.shuffled_indices)
            if old_idx is not None:
                try:
                    self.current_track = self.shuffled_indices.index(old_idx)
                except ValueError:
                    self.current_track = 0
        return self.shuffle_enabled

    def toggle_repeat(self) -> str:
        modes = ["off", "album", "track"]
        self.repeat_mode = modes[(modes.index(self.repeat_mode) + 1) % len(modes)]
        return self.repeat_mode

    def get_current_position(self) -> int:
        return self.player.position()

    def get_duration(self) -> int:
        return self.player.duration()

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def get_current_track_real_index(self) -> int | None:
        return self.current_track_idx

    # ── Internal slots ────────────────────────────────────────────────────────

    def _on_position_changed(self):
        if self.on_position_changed:
            self.on_position_changed()

    def _on_duration_changed(self):
        if self.on_duration_changed:
            self.on_duration_changed()

    def _on_timer_timeout(self):
        if self.on_position_changed:
            self.on_position_changed()

    def _on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._handle_track_end()

    def _on_playback_state_changed(self, state):
        try:
            if self._manual_track_switch:
                return
            if state != QMediaPlayer.PlaybackState.StoppedState:
                return
            pos, dur = 0, 0
            try:
                pos = self.player.position()
                dur = self.player.duration()
            except Exception:
                pass
            if dur > 0 and dur - pos <= 800:
                self._handle_track_end()
        except Exception:
            pass

    def _handle_track_end(self):
        try:
            if self.repeat_mode == "track":
                self.play_next()
            elif self.repeat_mode == "album":
                self.play_next()
            else:
                if not self.current_playing_album or self.current_track is None:
                    return
                if self.current_track + 1 < len(self.shuffled_indices):
                    self.play_next()
                else:
                    handled = False
                    try:
                        if self.on_album_finished:
                            handled = bool(self.on_album_finished(self.current_playing_artist, self.current_playing_album))
                    except Exception:
                        pass
                    if not handled:
                        self.stop()
        except Exception:
            pass
