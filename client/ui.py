import asyncio

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget


class MainWindow(QWidget):
    def __init__(self, relay, control, loop, audio, username):
        super().__init__()
        self.relay = relay
        self.control = control
        self.loop = loop
        self.audio = audio
        self.username = username

        self.setWindowTitle(f"HAM Radio Remote — {username}")

        self.status_label = QLabel("Свързване...")
        self.ptt_button = QPushButton("PTT (задръж)")
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.ptt_button)
        layout.addWidget(self.level_bar)

        self.ptt_button.pressed.connect(lambda: self._send_ptt(True))
        self.ptt_button.released.connect(lambda: self._send_ptt(False))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

    def _send_ptt(self, on: bool):
        asyncio.run_coroutine_threadsafe(self.control.request_ptt(on), self.loop)

    def _tick(self):
        if self.control.last_notice:
            self.status_label.setText(self.control.last_notice)
            self.control.last_notice = None
            self.level_bar.setValue(min(100, int(self.audio.level / 200 * 100)))
            return

        connected = self.relay.writer is not None
        busy_by = self.control.busy_by
        if not connected:
            self.status_label.setText("Няма връзка")
        elif busy_by and busy_by != self.username:
            self.status_label.setText(f"Заето от {busy_by}")
        elif busy_by == self.username:
            self.status_label.setText("Предава")
        else:
            self.status_label.setText("Свързан")
        self.level_bar.setValue(min(100, int(self.audio.level / 200 * 100)))
