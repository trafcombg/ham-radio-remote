"""Pick a root log level and which specific loggers (modules) should be
forced to DEBUG regardless of it — same idea and shared logic as the
admin panel's Debug settings dialog (common/debug_logging.py)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QScrollArea, QVBoxLayout, QWidget,
)

from common import debug_logging


class DebugDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Debug настройки")
        self.setWindowFlag(Qt.WindowStaysOnTopHint)
        self.resize(320, 420)

        state = debug_logging.current_state()

        self.root_level = QComboBox()
        self.root_level.addItems(["ERROR", "WARNING", "INFO", "DEBUG"])
        self.root_level.setCurrentText(state["root_level"])

        modules_widget = QWidget()
        modules_layout = QVBoxLayout(modules_widget)
        self._module_checks = {}
        for name in debug_logging.list_loggers():
            cb = QCheckBox(name)
            cb.setChecked(state["loggers"].get(name) == "DEBUG")
            modules_layout.addWidget(cb)
            self._module_checks[name] = cb
        modules_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(modules_widget)
        scroll.setWidgetResizable(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Приложи")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.root_level)
        layout.addWidget(scroll)
        layout.addWidget(buttons)

    def apply(self):
        modules = {name: "DEBUG" for name, cb in self._module_checks.items() if cb.isChecked()}
        debug_logging.apply(self.root_level.currentText(), modules)
