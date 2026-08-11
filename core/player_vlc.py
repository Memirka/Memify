"""Прототип PlayerController на libVLC вместо Qt Multimedia (QMediaPlayer).

Причина: QMediaPlayer/QAudioOutput в Qt6 не дают доступа к сырому аудиопотоку
для наложения фильтров — нет штатного способа сделать эквалайзер. У libVLC
эквалайзер встроен (10 фиксированных полос, см. get_eq_band_frequencies()).

Публичный интерфейс класса (методы/атрибуты, которые дергают main_window.py и
utils/media_keys.py) сохранён 1:1 с core/player.py — оба файла НЕ трогают
внутренние объекты плеера напрямую, только вызывают методы PlayerController,
поэтому этот файл можно подставить взамен оригинала простой заменой
`from core.player import PlayerController` на `from core.player_vlc import
PlayerController` в main_window.py, ничего больше менять не нужно.

Не перенесено (см. TODO по тексту) — можно доделать отдельным проходом:
- переключение аудиоустройства (get_audio_outputs/set_audio_output_device) —
  у libVLC для этого совсем другое API (audio_output_device_enum), сделан
  черновой вариант, но не проверен на реальном множественном выборе устройств
  так тщательно, как остальное;
- warm_up() упрощён — у VLC нет того же холодного старта FFmpeg-бэкенда Qt,
  поэтому не переносился 1:1, а сделан заметно проще.

Требует: pip install python-vlc (тонкая ctypes-обвязка) + сам libVLC,
установленный в системе (на Windows — рантайм VLC нужно будет тащить в
сборку PyInstaller, см. обсуждение в чате).
"""

import os
import random
import vlc
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from config import PLAYER_WARMUP_SOUND


# 10 полос эквалайзера у libVLC фиксированы (не настраиваются) — в отличие от
# скриншота-референса (60/150/400/1K/2.4K/15K), тут ровно эти частоты.
#
# ВАЖНО: libvlc_audio_equalizer_get_band_frequency() (публичный API) всегда
# возвращает ISO-сетку (31/62/125/250/500/1K/2K/4K/8K/16K, см. lib/audio.c
# upstream) — но реальный DSP-фильтр, который применяет усиление
# (modules/audio_filter/equalizer.c), по умолчанию считает по ДРУГОЙ,
# "родной" сетке libVLC (60/170/310/600/1K/3K/6K/12K/14K/16K,
# equalizer-vlcfreqs=true — дефолт модуля). Эти таблицы совпадают только на
# индексах 5 (1K) и 9 (16K) — значит доверять публичному геттеру нельзя,
# он подписывает 7 из 10 слайдеров чужими частотами (напр. "8K" в
# реальности фильтрует ~14кГц). Пробовали форсировать DSP на ISO-сетку
# флагом --equalizer-vlcfreqs=0 при создании инстанса — оказалось
# ненадёжно: на стоковой Arch-сборке libVLC 3.0.23 (с установленным
# libequalizer_plugin.so) эта CLI-опция не распознаётся вообще и
# libvlc_new() возвращает NULL вместо предупреждения. Поэтому вместо
# переключения DSP просто подписываем полосы РЕАЛЬНОЙ (родной) сеткой,
# которую фильтр использует по умолчанию.
_VLC_NATIVE_BAND_FREQS = (60.0, 170.0, 310.0, 600.0, 1000.0, 3000.0, 6000.0, 12000.0, 14000.0, 16000.0)


def get_eq_band_frequencies() -> list[float]:
    count = vlc.libvlc_audio_equalizer_get_band_count()
    if count == len(_VLC_NATIVE_BAND_FREQS):
        return list(_VLC_NATIVE_BAND_FREQS)
    return [vlc.libvlc_audio_equalizer_get_band_frequency(i) for i in range(count)]


class PlayerController(QObject):
    currentUrlChanged = pyqtSignal(str)
    audioOutputsUpdated = pyqtSignal(list)
    audioOutputDeviceChanged = pyqtSignal(object)
    _warmupNetworkParsed = pyqtSignal()

    # libVLC's own "Flat" equalizer preset uses a +12dB preamp with every
    # band at 0dB (modules/audio_filter/equalizer_presets.h upstream) — the
    # band filters lose ~12dB of headroom internally even at 0dB gain, so a
    # bare vlc.AudioEqualizer() (preamp defaults to 0) plays ~12dB quieter
    # than "no equalizer" the instant it's enabled, even with every slider
    # left at its neutral position. Baking this in makes our UI's "0dB"
    # preamp mean "same loudness as EQ off", like VLC's own reference.
    _PREAMP_BASELINE_DB = 12.0

    def __init__(self):
        super().__init__()
        # --no-video: аудио-плеер, декодировать/показывать видеодорожку не нужно.
        self._vlc_instance = vlc.Instance(["--no-video", "--quiet"])
        self.player = self._vlc_instance.media_player_new()

        self._preferred_audio_output_id = None
        self._current_audio_output_id = None
        self._has_emitted_audio_device = False
        self._available_audio_outputs: list[dict] = []

        # Эквалайзер живёт независимо от текущего трека — пересоздавать
        # media на play_track() эквалайзер не сбрасывает (в отличие от
        # некоторых свойств QMediaPlayer), но explicit set_equalizer()
        # нужно дергать заново на каждый новый MediaPlayer (тут он один и
        # тот же на весь процесс, так что один раз в конце __init__ и потом
        # при каждом изменении полосы).
        self._eq = vlc.AudioEqualizer()
        self._eq.set_preamp(self._PREAMP_BASELINE_DB)
        self._eq_band_freqs = get_eq_band_frequencies()
        self._eq_amps = [0.0] * len(self._eq_band_freqs)
        self._eq_preamp = 0.0
        self._eq_enabled = False
        self._eq_apply_timer = QTimer()
        self._eq_apply_timer.setSingleShot(True)
        self._eq_apply_timer.setInterval(60)
        self._eq_apply_timer.timeout.connect(self._apply_eq)

        self.current_playing_album = None
        self.current_playing_artist = None
        self.current_track = None
        self.current_track_idx = None
        self.track_durations = {}
        self.shuffle_enabled = False
        self.repeat_mode = "off"
        self.shuffled_indices = []
        self._manual_track_switch = False
        self._ended_handled = False  # чтобы _handle_track_end не сработал дважды на одном Ended-состоянии
        self._last_volume: int | None = None

        self.timer = QTimer()
        self.timer.setInterval(500)

        self.on_track_changed = None
        self.on_playback_state_changed = None
        self.on_position_changed = None
        self.on_duration_changed = None
        self.on_album_finished = None
        self.on_album_previous = None
        self.on_resolve_track_url = None

        self._warmup_network_media = None
        self._warmupNetworkParsed.connect(self._on_warmup_network_parsed)

    def setup_connections(self):
        # В отличие от QMediaPlayer, здесь нет Qt-сигналов positionChanged/
        # durationChanged/mediaStatusChanged — libVLC шлёт события на своём
        # внутреннем потоке (небезопасно дёргать оттуда Qt напрямую), поэтому
        # всё состояние (позиция, длительность, конец трека) вычитывается по
        # таймеру ниже, тем же самым, который раньше просто дублировал
        # positionChanged для прогресс-бара.
        self.timer.timeout.connect(self._on_timer_timeout)

    def set_callbacks(self, track_changed=None, playback_state_changed=None,
                      position_changed=None, duration_changed=None,
                      album_finished=None, album_previous=None,
                      resolve_track_url=None):
        self.on_track_changed = track_changed
        self.on_playback_state_changed = playback_state_changed
        self.on_position_changed = position_changed
        self.on_duration_changed = duration_changed
        self.on_album_finished = album_finished
        self.on_album_previous = album_previous
        # Called as resolve_track_url(track, continue_playback) before every
        # play_track — lets the app resolve a track['url'] that isn't
        # directly playable yet (e.g. a YouTube watch link, see
        # MusicApp._resolve_track_url_for_player) into a real stream URL
        # first, whether play_track was reached directly or via play_next/
        # play_prev/repeat (a mid-queue auto-advance onto a YouTube track
        # goes through the exact same path as a manual click). Must call
        # continue_playback(url) exactly once, synchronously or later from
        # a background thread — if unset, tracks are played with their
        # 'url' field as-is (previous behavior).
        self.on_resolve_track_url = resolve_track_url

    # ── Эквалайзер ───────────────────────────────────────────────────────────

    def get_eq_bands(self) -> list[tuple[float, float]]:
        """[(частота, текущий_дБ), ...] — для построения UI."""
        return list(zip(self._eq_band_freqs, self._eq_amps))

    def set_eq_band(self, index: int, db: float) -> None:
        if not (0 <= index < len(self._eq_amps)):
            return
        db = max(-20.0, min(20.0, db))
        self._eq_amps[index] = db
        self._eq.set_amp_at_index(db, index)
        self._schedule_eq_apply()

    def set_eq_preamp(self, db: float) -> None:
        db = max(-20.0, min(20.0, db))
        self._eq_preamp = db
        self._eq.set_preamp(db + self._PREAMP_BASELINE_DB)
        self._schedule_eq_apply()

    def set_eq_enabled(self, enabled: bool) -> None:
        self._eq_enabled = enabled
        self._eq_apply_timer.stop()
        self._apply_eq()

    def reset_eq(self) -> None:
        self._eq_amps = [0.0] * len(self._eq_band_freqs)
        self._eq_preamp = 0.0
        self._eq = vlc.AudioEqualizer()
        self._eq.set_preamp(self._PREAMP_BASELINE_DB)
        self._eq_apply_timer.stop()
        self._apply_eq()

    def _schedule_eq_apply(self) -> None:
        # libVLC пересобирает часть аудио-пайплайна на каждый
        # set_equalizer() (проверено: mid-playback вызов на живом плеере
        # приводит к пересозданию audio output) — при быстром перетаскивании
        # ползунка это может слать десятки вызовов в секунду и заикать звук.
        # Дебаунс коротким таймером схлопывает их в один вызов после того,
        # как палец/мышь на мгновение остановились.
        self._eq_apply_timer.start()

    def _apply_eq(self) -> None:
        # set_equalizer(None) в libVLC отключает эквалайзер полностью —
        # используем это для тумблера "выкл", вместо обнуления полос (что
        # оставило бы преамп/эквалайзер формально включённым на нулях).
        self.player.set_equalizer(self._eq if self._eq_enabled else None)

    # ── Audio device management (черновик, не так тщательно проверено,
    #    как остальное — TODO) ──────────────────────────────────────────────

    def get_audio_outputs(self) -> list:
        devices = []
        node = self.player.audio_output_device_enum()
        head = node
        while node:
            d = node.contents
            device_id = (d.device or b"").decode("utf-8", errors="ignore")
            description = (d.description or b"").decode("utf-8", errors="ignore")
            devices.append({"id": device_id, "description": description, "is_default": False})
            node = d.next
        if head:
            vlc.libvlc_audio_output_device_list_release(head)
        self._available_audio_outputs = devices
        return list(devices)

    def set_audio_output_device(self, device_id):
        self._preferred_audio_output_id = device_id or None
        try:
            # module=None — рекомендуемый libVLC способ: применяется сразу,
            # без пересоздания audio output (см. докстринг audio_output_device_set).
            self.player.audio_output_device_set(None, device_id)
            if self._current_audio_output_id != device_id or not self._has_emitted_audio_device:
                self._current_audio_output_id = device_id
                self._has_emitted_audio_device = True
                try:
                    self.audioOutputDeviceChanged.emit(device_id)
                except Exception:
                    pass
            return True
        except Exception as e:
            print(f"Audio device error: {e}")
            return False

    # ── Playback control ──────────────────────────────────────────────────────

    def warm_up(self, remote_url: str | None = None):
        """Прогревает audio-пайплайн ОС (открытие устройства вывода), играя
        собственный bundled-файл (assets/sounds/memify.wav) — НИКОГДА
        реальный трек из библиотеки пользователя. Раньше сюда передавался
        url первого попавшегося трека библиотеки, чтобы заодно прогреть
        сетевой HTTP/TLS-стек; на Windows у libVLC та же проблема, что и у
        Qt6/FFmpeg-бэкенда в TrackDurationWorker (см. его докстринг) — mute
        (audio_set_volume(0)) применяется не мгновенно, и в момент открытия
        устройства вывода долю секунды звучит настоящее содержимое трека.
        Раз в библиотеке первым мог оказаться чей угодно трек, слышимый
        результат выглядел как случайный чужой трек, включающийся сам по
        себе при каждом запуске приложения (и, соответственно, сразу после
        применения автообновления, когда новый .exe запускается впервые).

        memify.wav сам по себе — цифровая тишина (тот же формат/канал/
        длительность, что и исходный звук-джингл, только все сэмплы
        обнулены), а не звук, который просто пытаются заглушить через
        mute/volume=0. НЕ мьютит и НЕ обнуляет громкость throwaway-плеера —
        WASAPI на Windows и PulseAudio на Linux у libVLC оба группируют
        звуковые потоки процесса в ОДНУ сессию/запись в системном микшере,
        а не по отдельности на каждый MediaPlayer, так что
        audio_set_mute(True)/audio_set_volume(0) на этом отдельном
        throwaway-плеере откатывали ГРОМКОСТЬ ВСЕГО ПРОЦЕССА в микшере до
        0 (была такая версия раньше) — ту самую, которую
        self.player.set_volume(saved_vol) секундой раньше (main_window.py)
        уже корректно выставил, и, в отличие от обычной паузы, эта
        настройка микшера не привязана к времени жизни конкретного
        плеера: play_track()'ы через self.player продолжали идти на 0%
        громкости в микшере ОС даже после warmup_player.stop(), пока
        пользователь не поднимал её сам вручную. Раз содержимое файла само
        по себе беззвучно, трогать mute/volume вообще не нужно — устройство
        честно открывается на реальной громкости пользователя, играть
        физически нечему.

        Сетевой путь по-прежнему прогревается, если remote_url передан — но
        только через parse_with_options(..., network), который качает ровно
        столько байт, сколько нужно, чтобы определить формат, и никогда не
        создаёт MediaPlayer/не трогает звуковое устройство вообще, так что
        утечка в принципе невозможна.

        До самого первого play() на любом MediaPlayer этого процесса
        реального WASAPI/PulseAudio output-устройства ещё не существует —
        set_volume(saved_vol), вызванный до warm_up() (main_window.py),
        применяется к player'у, у которого ещё нет живой аудио-сессии, и
        может быть потерян: когда устройство физически открывается (первый
        реальный play(), которым как раз и оказывается play() этого
        throwaway-плеера), ОС может проинициализировать НОВУЮ сессию своим
        собственным запомненным ранее значением, а не тем, что просили
        мы — соответствие сообщённому на Windows поведению, когда громкость
        в приложении стоит на максимуме, а в системном микшере после этого
        видно заметно меньше. Поэтому здесь же, вскоре после того как
        warmup-плеер действительно откроет устройство, громкость
        принудительно переприменяется на РЕАЛЬНОМ self.player.
        """
        if remote_url:
            self._start_warmup_network_parse(remote_url)

        if not os.path.exists(PLAYER_WARMUP_SOUND):
            return
        try:
            warmup_player = self._vlc_instance.media_player_new()
            media = self._vlc_instance.media_new(PLAYER_WARMUP_SOUND)
            warmup_player.set_media(media)
            warmup_player.play()
            QTimer.singleShot(300, self._reapply_volume_after_warmup)
            QTimer.singleShot(600, warmup_player.stop)
        except Exception:
            pass

    def _reapply_volume_after_warmup(self) -> None:
        if self._last_volume is not None:
            self.set_volume(self._last_volume)

    def _start_warmup_network_parse(self, remote_url: str) -> None:
        try:
            media = self._vlc_instance.media_new(remote_url)
            self._warmup_network_media = media  # keep alive until parsed
            media.event_manager().event_attach(
                vlc.EventType.MediaParsedChanged, self._on_warmup_network_parsed_event
            )
            media.parse_with_options(vlc.MediaParseFlag.network, 8000)
        except Exception:
            self._warmup_network_media = None

    def _on_warmup_network_parsed_event(self, event):
        # Runs on a libVLC-internal thread — hop to the Qt thread via a
        # queued signal before touching self/Qt state.
        self._warmupNetworkParsed.emit()

    def _on_warmup_network_parsed(self):
        self._warmup_network_media = None

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
        # Set before the resolve hook runs (not after) — it may resolve
        # asynchronously (see on_resolve_track_url), and this timer-tick
        # guard needs to already be up for that whole gap so a stray Ended/
        # Error state read off the *previous* track's still-live media
        # object (self.timer keeps running across the transition) can't
        # re-trigger _handle_track_end mid-resolve and skip a track.
        self._manual_track_switch = True
        self._ended_handled = False
        self.current_track = index
        self.current_track_idx = self.shuffled_indices[index]
        track = self.current_playing_album["tracks"][self.current_track_idx]

        def _continue(resolved_url: str, _track=track, _index=index):
            self._start_playback(_index, _track, resolved_url)

        if self.on_resolve_track_url:
            self.on_resolve_track_url(track, _continue)
        else:
            _continue(track.get("url", ""))
        return True

    def _start_playback(self, index: int, track: dict, resolved_url: str):
        """The actual libVLC media switch — split out from play_track so it
        can run either immediately or after an async URL resolve completes
        (see on_resolve_track_url)."""
        if index != self.current_track:
            # A newer play_track (skip, next/prev spam, ...) has since
            # superseded this one — a resolve that finishes late must not
            # override whatever's playing now.
            return
        try:
            from utils.format_utils import resolve_media_url
            track_url = resolve_media_url(resolved_url or "")
            if not track_url:
                return
            media = self._vlc_instance.media_new(track_url)
            self.player.set_media(media)
            self.player.play()
            self.timer.start()

            if self.on_track_changed:
                self.on_track_changed(track, self.current_playing_artist, self.current_playing_album)
            if self.on_playback_state_changed:
                self.on_playback_state_changed(True)

            QTimer.singleShot(300, lambda: setattr(self, "_manual_track_switch", False))
            self.currentUrlChanged.emit(track_url)
        except Exception as e:
            print(f"play_track error: {e}")

    def toggle_playback(self):
        try:
            if self.player.is_playing():
                self.player.set_pause(1)
                if self.on_playback_state_changed:
                    self.on_playback_state_changed(False)
            else:
                if self.player.get_media() is None:
                    if self.current_track is not None and self.current_playing_album:
                        self.play_track(self.current_track)
                    # нет media и нет альбома — ничего не делаем (не показываем "играет" без звука)
                else:
                    self.player.set_pause(0)
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
                pos_ms = self.player.get_time()
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
            length = self.player.get_length()
            if length > 0:
                self.player.set_time(int(length * value / 100))
        except Exception:
            pass

    def set_volume(self, value: int):
        try:
            # У QAudioOutput громкость была float 0.0-1.0 — libVLC сразу
            # принимает int 0-100, конвертация больше не нужна.
            #
            # audio_set_mute(False) здесь обязателен, не опционален: mute —
            # ОТДЕЛЬНЫЙ булев флаг у WASAPI/PulseAudio-сессии, независимый
            # от уровня громкости — если он где-то остался включённым
            # (например, из-за более раннего бага в warm_up(), который до
            # этого мьютил всю сессию процесса), один только
            # audio_set_volume(...) с ненулевым значением его НЕ снимает.
            volume = max(0, min(100, int(value)))
            self._last_volume = volume
            self.player.audio_set_mute(False)
            self.player.audio_set_volume(volume)
        except Exception:
            pass

    def get_volume(self) -> int:
        try:
            return max(0, min(100, int(self.player.audio_get_volume())))
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
        try:
            return max(0, self.player.get_time())
        except Exception:
            return 0

    def get_duration(self) -> int:
        try:
            return max(0, self.player.get_length())
        except Exception:
            return 0

    def is_playing(self) -> bool:
        try:
            return bool(self.player.is_playing())
        except Exception:
            return False

    def get_current_track_real_index(self) -> int | None:
        return self.current_track_idx

    # ── Internal: таймер вместо Qt-сигналов от QMediaPlayer ────────────────

    def _on_timer_timeout(self):
        if self.on_position_changed:
            self.on_position_changed()
        if self._manual_track_switch:
            return
        try:
            state = self.player.get_state()
        except Exception:
            return
        if state == vlc.State.Ended and not self._ended_handled:
            self._ended_handled = True
            self._handle_track_end()
        elif state == vlc.State.Error and not self._ended_handled:
            # Битый/недоступный поток — не зависаем молча, ведём себя как
            # обычный конец трека, чтобы очередь пошла дальше.
            self._ended_handled = True
            self._handle_track_end()

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
