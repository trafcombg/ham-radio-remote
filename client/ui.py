import asyncio
import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from client.settings_dialog import SettingsDialog
from common.updater import download_and_run_installer


class MainWindow(QWidget):
    def __init__(self, session, loop, app_cfg: dict, config_path: Path, update_state=None):
        super().__init__()
        self.session = session
        self.loop = loop
        self.app_cfg = app_cfg
        self.config_path = config_path
        self.update_state = update_state

        self.setWindowTitle(f"HAM Radio Remote — {app_cfg['username']}")

        self.radio_list = QListWidget()
        for radio_cfg in app_cfg["radios"]:
            item = QListWidgetItem(radio_cfg["name"])
            item.setData(1000, radio_cfg)
            self.radio_list.addItem(item)
        self.radio_list.itemClicked.connect(self._on_radio_clicked)

        self.status_label = QLabel("Свързване...")

        self.ptt_button = QPushButton("PTT (задръж)")
        self.ptt_button.setStyleSheet("font-size: 20px; font-weight: bold; padding: 18px;")

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)

        self.cw_key_button = QPushButton("CW ключ (задръж)")
        self.cw_text_input = QLineEdit()
        self.cw_text_input.setPlaceholderText("текст за CW")
        self.cw_send_button = QPushButton("Изпрати CW")

        self.settings_button = QPushButton("Настройки")

        self.update_button = QPushButton("Обнови")
        self.update_button.setStyleSheet("background: #f59e0b;")
        self.update_button.hide()
        self.update_button.clicked.connect(self._apply_update)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Радиа"))
        layout.addWidget(self.radio_list)
        layout.addWidget(self.status_label)
        layout.addWidget(self.ptt_button)
        layout.addWidget(self.level_bar)
        layout.addWidget(self.cw_key_button)
        cw_row = QHBoxLayout()
        cw_row.addWidget(self.cw_text_input)
        cw_row.addWidget(self.cw_send_button)
        layout.addLayout(cw_row)
        layout.addWidget(self.settings_button)
        layout.addWidget(self.update_button)

        self.ptt_button.pressed.connect(lambda: self._send_ptt(True))
        self.ptt_button.released.connect(lambda: self._send_ptt(False))
        self.cw_key_button.pressed.connect(lambda: self._cw_key_press(True))
        self.cw_key_button.released.connect(lambda: self._cw_key_press(False))
        self.cw_send_button.clicked.connect(self._send_cw_text)
        self.settings_button.clicked.connect(self._open_settings)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

        if app_cfg["radios"]:
            self.radio_list.setCurrentRow(0)
            self._switch_radio(app_cfg["radios"][0])

    def _on_radio_clicked(self, item: QListWidgetItem):
        self._switch_radio(item.data(1000))

    def _switch_radio(self, radio_cfg: dict):
        asyncio.run_coroutine_threadsafe(self.session.switch_to(radio_cfg), self.loop)
        self.status_label.setText("Свързване...")

    def _send_ptt(self, on: bool):
        if self.session.control:
            asyncio.run_coroutine_threadsafe(self.session.control.request_ptt(on), self.loop)

    def _cw_key_press(self, on: bool):
        if self.session.cw_link:
            self.session.cw_link.send_key(on)

    def _send_cw_text(self):
        text = self.cw_text_input.text().strip()
        if text and self.session.text_cw:
            asyncio.run_coroutine_threadsafe(self.session.text_cw.send(text), self.loop)

    def _open_settings(self):
        dialog = SettingsDialog(self.app_cfg["cw"], self.app_cfg["rc28"], self)
        if dialog.exec():
            self.app_cfg["cw"] = dialog.result_cw_cfg(self.app_cfg["cw"])
            self.app_cfg["rc28"] = dialog.result_rc28_cfg(self.app_cfg["rc28"])
            self.config_path.write_text(json.dumps(self.app_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            current = self.radio_list.currentItem()
            if current:
                self._switch_radio(current.data(1000))

    def _apply_update(self):
        update = self.update_state.available
        if not update:
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Обновяване...")
        if download_and_run_installer(update["download_url"]):
            self.close()
        else:
            self.update_button.setEnabled(True)
            self.update_button.setText("Обнови (грешка, опитай пак)")

    def _tick(self):
        if self.update_state and self.update_state.available and not self.update_button.isVisible():
            self.update_button.setText(f"Налична версия {self.update_state.available['version']} — Обнови")
            self.update_button.show()

        for i in range(self.radio_list.count()):
            item = self.radio_list.item(i)
            radio_cfg = item.data(1000)
            busy_by = self.session.radio_status(radio_cfg["name"])
            label = radio_cfg["name"] + (f" — заето от {busy_by}" if busy_by else " — свободно")
            if item.text() != label:
                item.setText(label)

        if not self.session.control:
            self.level_bar.setValue(0)
            return

        if self.session.control.last_notice:
            self.status_label.setText(self.session.control.last_notice)
            self.session.control.last_notice = None
            return

        connected = self.session.relay is not None and self.session.relay.writer is not None
        busy_by = self.session.control.busy_by
        username = self.app_cfg["username"]
        if not connected:
            self.status_label.setText("Няма връзка")
        elif busy_by and busy_by != username:
            self.status_label.setText(f"Заето от {busy_by}")
        elif busy_by == username:
            self.status_label.setText("Предава")
        else:
            self.status_label.setText("Свързан")

        if self.session.audio:
            self.level_bar.setValue(min(100, int(self.session.audio.level / 200 * 100)))
