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
        self._update_mode_text()

    def _build_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: qlineargradient(
                    spread:pad, x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0f1116, stop:1 #161b22
                );
                color: #e9edf2;
            }
            #Card {
                background: rgba(19, 23, 31, 0.9);
                border: 1px solid #1f2937;
                border-radius: 16px;
                padding: 24px;
                /* Qt stylesheets не поддерживают box-shadow, имитируем отступами */
            }
            QLineEdit {
                padding: 12px;
                border: 1px solid #2c3747;
                border-radius: 12px;
                background: #0f141d;
                color: #ffffff;
                font-size: 14px;
            }
            QLineEdit:focus {
                border-color: #35d27c;
                outline: 2px solid rgba(53,210,124,0.18);
            }
            QPushButton {
                padding: 12px;
                border-radius: 12px;
                background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1,
                    stop:0 #35d27c, stop:1 #2bb36c);
                color: #0b0f15;
                font-weight: 700;
                letter-spacing: 0.3px;
            }
            QPushButton#linkButton {
                background: transparent;
                color: #9ea4aa;
                padding: 8px 4px;
                text-decoration: underline;
            }
            QPushButton#linkButton:hover {
                color: #cfd4dc;
            }
            QLabel#errorLabel {
                color: #ff7a7a;
            }
            QLabel#hintLabel {
                color: #9ea4aa;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QWidget(self)
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(12)
        card_layout.setContentsMargins(12, 12, 12, 12)

        self.title_label = QLabel(card)
        self.title_label.setStyleSheet("font-size: 20px; font-weight: 700;")

        self.hint_label = QLabel(card)
        self.hint_label.setObjectName("hintLabel")
        self.hint_label.setWordWrap(True)

        self.login_input = QLineEdit(card)
        self.login_input.setPlaceholderText("Логин")

        self.password_input = QLineEdit(card)
        self.password_input.setPlaceholderText("Пароль")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.error_label = QLabel(card)
        self.error_label.setObjectName("errorLabel")
        self.error_label.hide()

        self.submit_button = QPushButton(card)
        self.submit_button.clicked.connect(self._on_submit)

        self.toggle_button = QPushButton("Войти, если уже есть аккаунт", card)
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
        layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(2)

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
