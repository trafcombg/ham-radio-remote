import asyncio

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from common.civ import ptt_command


class MainWindow(QWidget):
    def __init__(self, relay, loop, audio, civ_address):
        super().__init__()
        self.relay = relay
        self.loop = loop
        self.audio = audio
        self.civ_address = civ_address

        self.setWindowTitle("HAM Radio Remote — Клиент")

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
        cmd = ptt_command(self.civ_address, on)
        asyncio.run_coroutine_threadsafe(self.relay.send_civ(cmd), self.loop)
        self.status_label.setText("Предава" if on else "Свързан")

    def _tick(self):
        connected = self.relay.writer is not None
        if connected and self.status_label.text() in ("Свързване...", "Няма връзка"):
            self.status_label.setText("Свързан")
        elif not connected:
            self.status_label.setText("Няма връзка")
        self.level_bar.setValue(min(100, int(self.audio.level / 200 * 100)))
