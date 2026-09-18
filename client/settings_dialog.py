"""Server address, CW source and RC-28 settings — deliberately a
separate, non-main screen (per the project plan): these are configured
once per operator setup, not touched during a QSO."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QSpinBox,
)

from client.audio_devices import list_input_devices, list_output_devices

DEFAULT_DEVICE_LABEL = "(по подразбиране)"


def _device_combo(devices: list, current) -> QComboBox:
    combo = QComboBox()
    combo.addItem(DEFAULT_DEVICE_LABEL, None)
    for idx, name in devices:
        combo.addItem(name, idx)
    if current is not None:
        i = combo.findData(current)
        if i >= 0:
            combo.setCurrentIndex(i)
    return combo


class SettingsDialog(QDialog):
    def __init__(
        self, server_host: str, password: str, cw_cfg: dict, rc28_cfg: dict,
        com_ports: dict, active_radios: list, radio_audio: dict, audio_latency: str = "low", parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Настройки — сървър, CW и RC-28")
        self.setWindowFlag(Qt.WindowStaysOnTopHint)

        self.server_host = QLineEdit(server_host)
        self.server_host.setPlaceholderText("IP или име на сървъра, напр. 192.168.1.10")

        self.password = QLineEdit(password)
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("парола за този сървър (зададена от администратора)")

        self.audio_latency = QComboBox()
        self.audio_latency.addItem("Ниска (по-малко закъснение, риск от прекъсвания)", "low")
        self.audio_latency.addItem("По-стабилна (по-голям буфер)", "high")
        self.audio_latency.setCurrentIndex(max(0, self.audio_latency.findData(audio_latency)))

        self.source = QComboBox()
        self.source.addItem("Прав ключ", "straight")
        self.source.addItem("Iambic paddle (USB-сериен)", "iambic")
        self.source.addItem("WinKeyer (external hardware)", "winkeyer")
        self.source.setCurrentIndex(max(0, self.source.findData(cw_cfg.get("source", "straight"))))

        self.wpm = QSpinBox()
        self.wpm.setRange(5, 60)
        self.wpm.setValue(cw_cfg.get("wpm", 20))

        self.paddle_port = QLineEdit(cw_cfg.get("paddle_port") or "")
        self.paddle_port.setPlaceholderText("COM порт на paddle интерфейса, напр. COM15")

        self.winkeyer_port = QLineEdit(cw_cfg.get("winkeyer_port") or "")
        self.winkeyer_port.setPlaceholderText("COM порт на WinKeyer, напр. COM16")

        self.rc28_enabled = QCheckBox("RC-28 включен")
        self.rc28_enabled.setChecked(rc28_cfg.get("enabled", False))

        self.rc28_step = QSpinBox()
        self.rc28_step.setRange(1, 10000)
        self.rc28_step.setValue(rc28_cfg.get("step_hz", 10))
        self.rc28_step.setSuffix(" Hz / стъпка на диска")

        form = QFormLayout(self)
        form.addRow("Сървър", self.server_host)
        form.addRow("Парола", self.password)
        form.addRow("Latency на аудио устройствата", self.audio_latency)
        form.addRow("CW източник", self.source)
        form.addRow("Скорост (WPM)", self.wpm)
        form.addRow("Paddle порт", self.paddle_port)
        form.addRow("WinKeyer порт", self.winkeyer_port)
        form.addRow(self.rc28_enabled)
        form.addRow("RC-28 стъпка", self.rc28_step)

        self._radio_audio_widgets = {}  # radio name -> (input_combo, output_combo)
        if active_radios:
            input_devices = list_input_devices()
            output_devices = list_output_devices()
            form.addRow(QLabel("<b>Радиа — портове и аудио устройства</b>"))
            for radio_cfg in active_radios:
                name = radio_cfg["name"]
                com_port = com_ports.get(name, {}).get("local", "— (ще се създаде при връзка)")
                civ = f" | CIV {radio_cfg['civ_address']:02X}" if radio_cfg.get("civ_address") else ""
                details = (
                    f"COM {com_port} · CAT TCP :{radio_cfg.get('cat_port')} · Control :{radio_cfg.get('control_port')} · "
                    f"Audio UDP :{radio_cfg.get('audio_port')} · CW UDP :{radio_cfg.get('cw_port')}{civ}"
                )
                form.addRow(f"<b>{name}</b>", QLabel(details))

                current = radio_audio.get(name, {})
                in_combo = _device_combo(input_devices, current.get("input_device"))
                out_combo = _device_combo(output_devices, current.get("output_device"))
                form.addRow("  Микрофон (вход, УДП → радио)", in_combo)
                form.addRow("  Говорител (изход, УДП → тук)", out_combo)
                self._radio_audio_widgets[name] = (in_combo, out_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_server_host(self) -> str:
        return self.server_host.text().strip()

    def result_password(self) -> str:
        return self.password.text()

    def result_audio_latency(self) -> str:
        return self.audio_latency.currentData()

    def result_cw_cfg(self, base_cw_cfg: dict) -> dict:
        cfg = dict(base_cw_cfg)
        cfg["source"] = self.source.currentData()
        cfg["wpm"] = self.wpm.value()
        cfg["paddle_port"] = self.paddle_port.text().strip() or None
        cfg["winkeyer_port"] = self.winkeyer_port.text().strip() or None
        return cfg

    def result_rc28_cfg(self, base_rc28_cfg: dict) -> dict:
        cfg = dict(base_rc28_cfg)
        cfg["enabled"] = self.rc28_enabled.isChecked()
        cfg["step_hz"] = self.rc28_step.value()
        return cfg

    def result_radio_audio(self) -> dict:
        """{radio_name: {"input_device": idx|None, "output_device": idx|None}}
        — None means "use the global default device", same convention as
        app_cfg["audio"]."""
        return {
            name: {"input_device": in_combo.currentData(), "output_device": out_combo.currentData()}
            for name, (in_combo, out_combo) in self._radio_audio_widgets.items()
        }
