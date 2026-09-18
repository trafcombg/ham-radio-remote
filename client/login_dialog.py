"""Startup login — collects username/password before the main window
opens. Doesn't itself validate against the server (there's no single
account-wide "log in" call for the desktop client, only per-radio auth);
a wrong password or missing permission shows up per-radio once connected
(see control.py's hello_denied / session.py's last_notice)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit


class LoginDialog(QDialog):
    def __init__(self, username: str, password: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HAM Radio Remote — вход")
        self.setWindowFlag(Qt.WindowStaysOnTopHint)

        self.username = QLineEdit(username)
        self.password = QLineEdit(password)
        self.password.setEchoMode(QLineEdit.Password)

        form = QFormLayout(self)
        form.addRow("Потребител", self.username)
        form.addRow("Парола", self.password)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Вход")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_accept(self):
        if not self.username.text().strip():
            self.username.setFocus()
            return
        self.accept()

    def result_credentials(self) -> tuple:
        return self.username.text().strip(), self.password.text()
