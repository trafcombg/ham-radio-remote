import asyncio
import json
import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from client import credential_store
from client.debug_dialog import DebugDialog
from client.settings_dialog import SettingsDialog
from common.app_paths import app_dir
from common.updater import check_for_update, download_and_run_installer
from common.version import APP_VERSION

log = logging.getLogger("ui")

LOG_PATH = app_dir(__file__) / "client.log"


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
            if not radio_cfg.get("active", True):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)  # greyed out + unclickable, native Qt look
            self.radio_list.addItem(item)
        self.radio_list.itemClicked.connect(self._on_radio_clicked)

        self.status_label = QLabel("Свързване...")
        self.version_label = QLabel(f"Клиент v{APP_VERSION}")
        self.version_label.setStyleSheet("color: gray; font-size: 11px;")

        self.ptt_button = QPushButton("PTT (задръж)")
        self.ptt_button.setStyleSheet("font-size: 20px; font-weight: bold; padding: 18px;")

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)

        self.output_level_bar = QProgressBar()
        self.output_level_bar.setRange(0, 100)
        self.output_level_bar.setTextVisible(False)
        self.output_level_bar.setStyleSheet("QProgressBar::chunk { background-color: #3b82f6; }")

        audio_cfg = app_cfg["audio"]
        self.mic_gain_slider = QSlider(Qt.Horizontal)
        self.mic_gain_slider.setRange(0, 200)
        self.mic_gain_slider.setValue(int(audio_cfg.get("mic_gain", 1.0) * 100))
        self.mic_gain_slider.valueChanged.connect(self._on_mic_gain_changed)

        self.speaker_gain_slider = QSlider(Qt.Horizontal)
        self.speaker_gain_slider.setRange(0, 200)
        self.speaker_gain_slider.setValue(int(audio_cfg.get("speaker_gain", 1.0) * 100))
        self.speaker_gain_slider.valueChanged.connect(self._on_speaker_gain_changed)

        self.cw_key_button = QPushButton("CW ключ (задръж)")
        self.cw_text_input = QLineEdit()
        self.cw_text_input.setPlaceholderText("текст за CW")
        self.cw_send_button = QPushButton("Изпрати CW")

        self.settings_button = QPushButton("Настройки")

        self.debug_button = QPushButton("Debug настройки")
        self.debug_button.clicked.connect(self._open_debug_dialog)

        self.log_button = QPushButton("Отвори лог файл")
        self.log_button.clicked.connect(self._open_log_file)

        self.check_update_button = QPushButton("Провери за ъпдейт")
        self.check_update_button.clicked.connect(self._check_update_now)
        self._checking_update = False
        self._last_check_no_update = False

        self.update_button = QPushButton("Обнови")
        self.update_button.setStyleSheet("background: #f59e0b;")
        self.update_button.hide()
        self.update_button.clicked.connect(self._apply_update)

        self.amp_section_label = QLabel("Усилватели")
        self.amp_container = QVBoxLayout()
        self.amp_widget = QWidget()
        self.amp_widget.setLayout(self.amp_container)
        self._amp_signature = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Радиа"))
        layout.addWidget(self.radio_list)
        layout.addWidget(self.status_label)
        layout.addWidget(self.ptt_button)
        layout.addWidget(QLabel("Микрофон (вход)"))
        layout.addWidget(self.level_bar)
        layout.addWidget(self.mic_gain_slider)
        layout.addWidget(QLabel("Говорител (изход)"))
        layout.addWidget(self.output_level_bar)
        layout.addWidget(self.speaker_gain_slider)
        layout.addWidget(self.cw_key_button)
        cw_row = QHBoxLayout()
        cw_row.addWidget(self.cw_text_input)
        cw_row.addWidget(self.cw_send_button)
        layout.addLayout(cw_row)
        layout.addWidget(self.settings_button)
        layout.addWidget(self.debug_button)
        layout.addWidget(self.log_button)
        layout.addWidget(self.check_update_button)
        layout.addWidget(self.update_button)
        layout.addWidget(self.amp_section_label)
        layout.addWidget(self.amp_widget)
        layout.addWidget(self.version_label)
        self.amp_section_label.hide()
        self.amp_widget.hide()

        self.ptt_button.pressed.connect(lambda: self._send_ptt(True))
        self.ptt_button.released.connect(lambda: self._send_ptt(False))
        self.cw_key_button.pressed.connect(lambda: self._cw_key_press(True))
        self.cw_key_button.released.connect(lambda: self._cw_key_press(False))
        self.cw_send_button.clicked.connect(self._send_cw_text)
        self.settings_button.clicked.connect(self._open_settings)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

        active_radios = [r for r in app_cfg["radios"] if r.get("active", True)]
        if active_radios:
            # Compare by name, not identity: PySide6's QVariant storage
            # doesn't guarantee item.data() returns the exact same Python
            # object that was passed to setData() for a plain dict.
            first_name = active_radios[0]["name"]
            first_row = next(i for i in range(self.radio_list.count()) if self.radio_list.item(i).data(1000)["name"] == first_name)
            self.radio_list.setCurrentRow(first_row)
            self._switch_radio(active_radios[0])

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

    def _on_mic_gain_changed(self, value: int):
        gain = value / 100.0
        self.app_cfg["audio"]["mic_gain"] = gain
        if self.session.audio:
            self.session.audio.mic_gain = gain

    def _on_speaker_gain_changed(self, value: int):
        gain = value / 100.0
        self.app_cfg["audio"]["speaker_gain"] = gain
        if self.session.audio:
            self.session.audio.speaker_gain = gain

    def _open_settings(self):
        active_radios = [r for r in self.app_cfg["radios"] if r.get("active", True)]
        current_password = credential_store.decrypt(self.app_cfg.get("password_encrypted", ""))
        dialog = SettingsDialog(
            self.app_cfg["server_host"], current_password, self.app_cfg["cw"], self.app_cfg["rc28"],
            self.app_cfg.get("com_ports", {}), active_radios, self.app_cfg.get("radio_audio", {}), self,
        )
        if dialog.exec():
            new_server_host = dialog.result_server_host()
            new_password = dialog.result_password()
            password_changed = new_password != current_password
            self.app_cfg["password_encrypted"] = credential_store.encrypt(new_password)
            self.session.password = new_password
            self.app_cfg["cw"] = dialog.result_cw_cfg(self.app_cfg["cw"])
            self.app_cfg["rc28"] = dialog.result_rc28_cfg(self.app_cfg["rc28"])
            self.app_cfg["radio_audio"] = dialog.result_radio_audio()
            self.config_path.write_text(json.dumps(self.app_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            if (new_server_host and new_server_host != self.session.server_host) or password_changed:
                asyncio.run_coroutine_threadsafe(self.session.reconnect(new_server_host or self.session.server_host), self.loop)
                self.status_label.setText("Свързване...")
            else:
                current = self.radio_list.currentItem()
                if current:
                    self._switch_radio(current.data(1000))

    def _refresh_amplifier_panel(self):
        amps = self.session.amplifiers
        signature = tuple((a["name"], a["can_control"], (a["telemetry"] or {}).get("status")) for a in amps)
        if signature == self._amp_signature:
            return
        self._amp_signature = signature

        while self.amp_container.count():
            item = self.amp_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.amp_section_label.setVisible(bool(amps))
        self.amp_widget.setVisible(bool(amps))
        for amp in amps:
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            status = (amp["telemetry"] or {}).get("status") or "—"
            row.addWidget(QLabel(f"{amp['name']} ({status})"))
            for mode_label, mode in (("Operate", "operate"), ("Standby", "standby"), ("Off", "off")):
                btn = QPushButton(mode_label)
                btn.setEnabled(amp["can_control"])
                btn.clicked.connect(lambda checked=False, n=amp["name"], m=mode: self._set_amp_mode(n, m))
                row.addWidget(btn)
            self.amp_container.addWidget(row_widget)

    def _set_amp_mode(self, name: str, mode: str):
        asyncio.run_coroutine_threadsafe(self._set_amp_mode_async(name, mode), self.loop)

    async def _set_amp_mode_async(self, name: str, mode: str):
        ok, detail = await self.session.set_amplifier_mode(name, mode)
        if not ok:
            log.warning("смяна на режим за %s се провали: %s", name, detail)

    def _open_debug_dialog(self):
        dialog = DebugDialog(self)
        if dialog.exec():
            dialog.apply()
            log.info("debug logging updated")

    def _open_log_file(self):
        try:
            subprocess.Popen([
                "powershell.exe", "-NoExit", "-Command",
                f"Get-Content -Path '{LOG_PATH}' -Wait -Tail 200",
            ])
        except OSError:
            log.warning("не успях да отворя лог файла %s", LOG_PATH, exc_info=True)

    def _check_update_now(self):
        if self._checking_update:
            return
        self._checking_update = True
        self.check_update_button.setEnabled(False)
        self.check_update_button.setText("Проверка...")
        asyncio.run_coroutine_threadsafe(self._check_update_now_async(), self.loop)

    async def _check_update_now_async(self):
        update = await asyncio.to_thread(check_for_update, APP_VERSION, "Client-Setup")
        if self.update_state:
            self.update_state.available = update
        self._last_check_no_update = update is None
        self._checking_update = False

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
        server_version = self.session.server_version
        if server_version and server_version != APP_VERSION:
            self.version_label.setText(f"Клиент v{APP_VERSION} — сървър v{server_version} (различни версии!)")
            self.version_label.setStyleSheet("color: #f59e0b; font-size: 11px; font-weight: bold;")
        elif server_version:
            self.version_label.setText(f"Клиент v{APP_VERSION} — сървър v{server_version}")
            self.version_label.setStyleSheet("color: gray; font-size: 11px;")

        self._refresh_amplifier_panel()

        if self.update_state and self.update_state.available and not self.update_button.isVisible():
            self.update_button.setText(f"Налична версия {self.update_state.available['version']} — Обнови")
            self.update_button.show()

        if not self._checking_update and not self.check_update_button.isEnabled():
            self.check_update_button.setEnabled(True)
            self.check_update_button.setText("Няма нова версия" if self._last_check_no_update else "Провери за ъпдейт")

        for i in range(self.radio_list.count()):
            item = self.radio_list.item(i)
            radio_cfg = item.data(1000)
            name = radio_cfg["name"]
            com_port = self.session.app_cfg.get("com_ports", {}).get(name, {}).get("local")
            port_suffix = f" ({com_port})" if com_port else ""
            if not radio_cfg.get("active", True):
                label = f"{name}{port_suffix} — неактивно"
            else:
                busy_by = self.session.radio_status(name)
                label = name + port_suffix + (f" — заето от {busy_by}" if busy_by else " — свободно")
            if item.text() != label:
                item.setText(label)

        if self.session.audio_error:
            self.status_label.setText(self.session.audio_error)
            self.session.audio_error = None
            return

        if not self.session.control:
            self.level_bar.setValue(0)
            self.output_level_bar.setValue(0)
            return

        if self.session.control.last_notice:
            self.status_label.setText(self.session.control.last_notice)
            self.session.control.last_notice = None
            return

        active_relay = self.session.cat_relays.get(self.session.radio_name)
        connected = active_relay is not None and active_relay.writer is not None
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
            self.level_bar.setValue(min(100, int(self.session.audio.input_level / 200 * 100)))
            self.output_level_bar.setValue(min(100, int(self.session.audio.output_level / 200 * 100)))
        else:
            self.level_bar.setValue(0)
            self.output_level_bar.setValue(0)
