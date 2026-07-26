from pynput import keyboard
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer, QMutex, QMutexLocker
import threading
import time

class MediaKeysHandler(QObject):

    play_pause_requested = pyqtSignal()
    next_requested = pyqtSignal()
    prev_requested = pyqtSignal()
    stop_requested = pyqtSignal()

    def __init__(self, player_controller, main_window=None):
        super().__init__()
        self.player_controller = player_controller
        self.main_window = main_window
        self.listener = None

        self.action_mutex = QMutex()

        self.global_lock_until = 0
        self.global_cooldown = 1.0

        # These signals can be emitted from the pynput thread; force queued delivery so slots
        # always execute on the Qt (GUI) thread to avoid crashes.
        self.play_pause_requested.connect(self._handle_play_pause, Qt.ConnectionType.QueuedConnection)
        self.next_requested.connect(self._handle_next, Qt.ConnectionType.QueuedConnection)
        self.prev_requested.connect(self._handle_prev, Qt.ConnectionType.QueuedConnection)
        self.stop_requested.connect(self._handle_stop, Qt.ConnectionType.QueuedConnection)

    def setup_media_keys(self):
        """Настройка глобальных медиа-клавиш"""
        try:
            def on_press(key):
                try:

                    with QMutexLocker(self.action_mutex):
                        current_time = time.time()

                        if current_time < self.global_lock_until:
                            print(f"Действие заблокировано глобально до {self.global_lock_until}")
                            return

                        action_type = None

                        if key == keyboard.Key.media_play_pause:
                            action_type = 'play_pause'
                        elif key == keyboard.Key.media_next:
                            action_type = 'next'
                        elif key == keyboard.Key.media_previous:
                            action_type = 'prev'
                        elif hasattr(keyboard.Key, 'media_stop') and key == keyboard.Key.media_stop:
                            action_type = 'stop'

                        if action_type:
                            print(f"Обработка медиа-клавиши: {action_type} в {current_time}")

                            self.global_lock_until = current_time + self.global_cooldown

                            if action_type == 'play_pause':
                                self.play_pause_requested.emit()
                            elif action_type == 'next':
                                self.next_requested.emit()
                            elif action_type == 'prev':
                                self.prev_requested.emit()
                            elif action_type == 'stop':
                                self.stop_requested.emit()

                except Exception as e:
                    print(f"Ошибка обработки медиа-клавиши: {e}")

            def on_release(key):

                pass

            if self.listener:
                try:
                    self.listener.stop()
                except:
                    pass

            self.listener = keyboard.Listener(
                on_press=on_press,
                on_release=on_release,
                suppress=False
            )
            self.listener.start()
            print("Медиа-клавиши успешно настроены")

        except Exception as e:
            print(f"Ошибка настройки медиа-клавиш: {e}")

    def _can_process_action(self, action_source):
        """Проверяет, можно ли обработать действие"""
        with QMutexLocker(self.action_mutex):
            current_time = time.time()

            if current_time < self.global_lock_until:
                print(f"Действие от {action_source} заблокировано до {self.global_lock_until}")
                return False

            self.global_lock_until = current_time + self.global_cooldown
            print(f"Принято действие от {action_source}, блокировка до {self.global_lock_until}")

            return True

    def _handle_play_pause(self):
        """Обработка play/pause"""
        try:
            print("Выполнение toggle_playback")
            self.player_controller.toggle_playback()
        except Exception as e:
            print(f"Ошибка toggle_playback: {e}")

    def _handle_next(self):
        """Обработка следующего трека"""
        try:
            print("Выполнение play_next")
            self.player_controller.play_next()

            if self.main_window:
                QTimer.singleShot(50, self.main_window.update_track_selection)
                QTimer.singleShot(200, self.main_window.update_track_selection)
        except Exception as e:
            print(f"Ошибка play_next: {e}")

    def _handle_prev(self):
        """Обработка предыдущего трека"""
        try:
            print("Выполнение play_prev")
            self.player_controller.play_prev()

            if self.main_window:
                QTimer.singleShot(50, self.main_window.update_track_selection)
                QTimer.singleShot(200, self.main_window.update_track_selection)
        except Exception as e:
            print(f"Ошибка play_prev: {e}")

    def _handle_stop(self):
        """Обработка остановки"""
        try:
            print("Выполнение stop")
            self.player_controller.stop()
        except Exception as e:
            print(f"Ошибка stop: {e}")

    def handle_key_press(self, event):
        """Обработка Qt клавиш"""
        try:
            key = event.key()

            if key in [Qt.Key.Key_MediaPlay, Qt.Key.Key_MediaTogglePlayPause]:
                if self._can_process_action('qt_play_pause'):
                    self.play_pause_requested.emit()
                event.accept()
                return

            elif key == Qt.Key.Key_MediaNext:
                if self._can_process_action('qt_next'):
                    self.next_requested.emit()
                event.accept()
                return

            elif key == Qt.Key.Key_MediaPrevious:
                if self._can_process_action('qt_prev'):
                    self.prev_requested.emit()
                event.accept()
                return

            elif key == Qt.Key.Key_MediaStop:
                if self._can_process_action('qt_stop'):
                    self.stop_requested.emit()
                event.accept()
                return

            elif key == Qt.Key.Key_Space:
                if self._can_process_action('space'):
                    self.play_pause_requested.emit()
                event.accept()
                return

            elif key == Qt.Key.Key_Right:
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    if self._can_process_action('ctrl_right'):
                        self.next_requested.emit()
                    event.accept()
                    return

            elif key == Qt.Key.Key_Left:
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    if self._can_process_action('ctrl_left'):
                        self.prev_requested.emit()
                    event.accept()
                    return

            event.ignore()

        except Exception as e:
            print(f"Ошибка обработки клавиш: {e}")
            event.ignore()

    def stop_listener(self):
        """Остановка listener"""
        if self.listener:
            try:
                self.listener.stop()
                self.listener = None
                print("Медиа-клавиши остановлены")
            except Exception as e:
                print(f"Ошибка остановки медиа-клавиш: {e}")

    def __del__(self):
        """Деструктор"""
        self.stop_listener()
