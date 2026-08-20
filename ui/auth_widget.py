from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpacerItem,
    QSizePolicy,
)

from core.account import AccountManager
from ui.styles import COLORS, get_theme


class _AuthWorker(QThread):
    finished_with_result = pyqtSignal(bool, str)

    def __init__(self, mode: str, login: str, password: str, account_manager: AccountManager):
        super().__init__()
        self.mode = mode
        self.login = login
        self.password = password
        self.account_manager = account_manager

    def run(self):
        ok = False
        try:
            if self.mode == "register":
                ok = self.account_manager.register(self.login, self.password, persist=True)
            else:
                ok = self.account_manager.login(self.login, self.password, persist=True)
        except Exception:
            ok = False
        err = self.account_manager.last_error or ""
        self.finished_with_result.emit(ok, err)


class AuthWidget(QWidget):
    authenticated = pyqtSignal(dict)

    def __init__(self, account_manager: AccountManager, parent=None):
        super().__init__(parent)
        self.account_manager = account_manager
        self._mode = "register"
        self._worker: _AuthWorker | None = None
        self._build_ui()
        self.apply_theme()
        self._update_mode_text()

    def _build_ui(self):
        self.setObjectName("AuthWidget")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.card = QWidget(self)
        self.card.setObjectName("Card")
        card_layout = QVBoxLayout(self.card)
        card_layout.setSpacing(12)
        card_layout.setContentsMargins(12, 12, 12, 12)

        self.title_label = QLabel(self.card)
        self.title_label.setObjectName("titleLabel")

        self.hint_label = QLabel(self.card)
        self.hint_label.setObjectName("hintLabel")
        self.hint_label.setWordWrap(True)

        self.login_input = QLineEdit(self.card)
        self.login_input.setPlaceholderText("Логин")

        self.password_input = QLineEdit(self.card)
        self.password_input.setPlaceholderText("Пароль")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.error_label = QLabel(self.card)
        self.error_label.setObjectName("errorLabel")
        self.error_label.hide()

        self.submit_button = QPushButton(self.card)
        self.submit_button.clicked.connect(self._on_submit)

        self.toggle_button = QPushButton("Войти, если уже есть аккаунт", self.card)
        self.toggle_button.setObjectName("linkButton")
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.clicked.connect(self._toggle_mode)
        self.toggle_button.setFlat(True)

        card_layout.addWidget(self.title_label, alignment=Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self.hint_label)
        card_layout.addSpacing(6)
        card_layout.addWidget(self.login_input)
        card_layout.addWidget(self.password_input)
        card_layout.addWidget(self.error_label)
        card_layout.addWidget(self.submit_button)
        card_layout.addItem(QSpacerItem(0, 4, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
        card_layout.addWidget(self.toggle_button, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        layout.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(2)

    def apply_theme(self):
        c = COLORS
        if get_theme() == "light":
            page_bg = c["BACKGROUND"]
            card_bg = c["SURFACE"]
            input_bg = c["SURFACE_LIGHT"]
            text = c["TEXT_PRIMARY"]
            secondary = c["TEXT_SECONDARY"]
            border = c["BORDER"]
            error = "#D93025"
        else:
            page_bg = (
                "qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1, "
                "stop:0 #0f1116, stop:1 #161b22)"
            )
            card_bg = "rgba(19, 23, 31, 0.9)"
            input_bg = "#0f141d"
            text = "#e9edf2"
            secondary = "#9ea4aa"
            border = "#2c3747"
            error = "#ff7a7a"

        self.setStyleSheet(f"""
            QWidget#AuthWidget {{
                background: {page_bg};
                color: {text};
            }}
            QWidget#Card {{
                background: {card_bg};
                border: 1px solid {border};
                border-radius: 16px;
                padding: 24px;
            }}
            QLabel {{
                background: transparent;
                color: {text};
            }}
            QLabel#titleLabel {{
                font-size: 20px;
                font-weight: 700;
            }}
            QLabel#hintLabel {{
                color: {secondary};
            }}
            QLabel#errorLabel {{
                color: {error};
            }}
            QLineEdit {{
                padding: 12px;
                border: 1px solid {border};
                border-radius: 12px;
                background: {input_bg};
                color: {text};
                font-size: 14px;
                selection-background-color: {c['PRIMARY']};
                selection-color: #0b0f15;
            }}
            QLineEdit:focus {{
                border-color: {c['PRIMARY']};
            }}
            QPushButton {{
                padding: 12px;
                border: none;
                border-radius: 12px;
                background: {c['PRIMARY_GRADIENT']};
                color: #0b0f15;
                font-weight: 700;
                letter-spacing: 0.3px;
            }}
            QPushButton:hover {{
                background: {c['PRIMARY_HOVER']};
            }}
            QPushButton:disabled {{
                color: rgba(11, 15, 21, 0.55);
            }}
            QPushButton#linkButton {{
                background: transparent;
                color: {secondary};
                padding: 8px 4px;
                text-decoration: underline;
            }}
            QPushButton#linkButton:hover {{
                color: {text};
                background: transparent;
            }}
        """)

    def _toggle_mode(self):
        self._mode = "login" if self._mode == "register" else "register"
        self._update_mode_text()
        self._set_error("")

    def _update_mode_text(self):
        if self._mode == "register":
            self.title_label.setText("Создайте аккаунт")
            self.hint_label.setText("После регистрации, ваши данные будут сохранены в облаке.")
            self.submit_button.setText("Зарегистрироваться")
            self.toggle_button.setText("Войти, если уже есть аккаунт")
        else:
            self.title_label.setText("Войдите в аккаунт")
            self.hint_label.setText("Укажите логин и пароль, чтобы загрузить ваши сохранения.")
            self.submit_button.setText("Войти")
            self.toggle_button.setText("Создать новый аккаунт")

    def _set_error(self, text: str):
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text.strip()))

    def _set_busy(self, busy: bool):
        for widget in (self.login_input, self.password_input, self.submit_button, self.toggle_button):
            widget.setEnabled(not busy)

    def _on_submit(self):
        if self._worker and self._worker.isRunning():
            return
        login = self.login_input.text().strip()
        password = self.password_input.text().strip()
        if not login or not password:
            self._set_error("Введите логин и пароль.")
            return
        if len(login) > 20 or (not login.isascii()) or (not all(ch.isalnum() for ch in login)):
            self._set_error("Логин: до 20 символов, только латиница и цифры, без пробелов.")
            return
        if len(password) > 100 or (not password.isascii()):
            self._set_error("Пароль: до 100 ASCII-символов.")
            return

        self._set_error("")
        self._set_busy(True)
        self._worker = _AuthWorker(self._mode, login, password, self.account_manager)
        self._worker.finished_with_result.connect(self._on_auth_finished)
        self._worker.start()

    def _on_auth_finished(self, ok: bool, err: str):
        self._set_busy(False)
        if self._worker:
            # finished_with_result is emitted from inside run(), which can race
            # slightly ahead of the OS thread actually winding down — dropping
            # our last reference before it's truly done crashes the app with
            # "QThread: Destroyed while thread is still running". wait() blocks
            # until it's genuinely finished (near-instant, since run() already returned).
            self._worker.wait()
        self._worker = None
        if not ok:
            if self._mode == "register" and err == "user_exists":
                self._set_error("Этот логин уже существует. Войдите через меню входа.")
            elif self._mode == "login" and err == "invalid_credentials":
                self._set_error("Введён неверный логин или пароль.")
            else:
                self._set_error("Не удалось подтвердить аккаунт. Попробуйте ещё раз.")
            return

        self._set_error("")
        self.authenticated.emit({"login": self.login_input.text().strip()})
