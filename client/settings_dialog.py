"""CW source and RC-28 settings — deliberately a separate, non-main
screen (per the project plan): these are configured once per operator
setup, not touched during a QSO."""

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox,
)


class SettingsDialog(QDialog):
    def __init__(self, cw_cfg: dict, rc28_cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки — CW и RC-28")

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
        form.addRow("CW източник", self.source)
        form.addRow("Скорост (WPM)", self.wpm)
        form.addRow("Paddle порт", self.paddle_port)
        form.addRow("WinKeyer порт", self.winkeyer_port)
        form.addRow(self.rc28_enabled)
        form.addRow("RC-28 стъпка", self.rc28_step)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

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
