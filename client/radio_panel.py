"""Built-in radio control panel — frequency, mode, and S-meter, driven
over the same CAT tunnel real CAT software (WSJT-X, N1MM+...) uses.
Modeled on Icom RS-BA1 v2's Operate screen (a separate window from the
main connection window, dark rig-style readout, big frequency digits,
mode buttons, S-meter) — for when the operator doesn't want to run a
separate CAT app just to change frequency or mode.

ponytail: RS-BA1 also has a spectrum scope, memory channels, RIT/XIT,
and NB/NR toggles — none of that is here. Add the piece that's actually
needed (CI-V scope data is cmd 0x27, memories are 0x1A/0x1B) rather than
building the whole thing speculatively.
"""

import asyncio

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from common.civ import MODE_NAMES, NAME_TO_MODE

STEPS_HZ = [1, 10, 100, 1000, 10000, 100000]
DEFAULT_STEP_INDEX = 3  # 1 kHz

# Приблизителна честота за бърз преход по обхват — НЕ памет на радиото
# (band-stacking регистри), просто разумна отправна точка за всеки бенд.
BAND_DEFAULTS_HZ = {
    "160m": 1_900_000, "80m": 3_650_000, "40m": 7_100_000, "30m": 10_125_000,
    "20m": 14_200_000, "17m": 18_130_000, "15m": 21_250_000, "12m": 24_950_000,
    "10m": 28_400_000, "6m": 50_150_000,
}

MODE_BUTTONS = ["LSB", "USB", "CW", "AM", "FM", "RTTY"]


def format_frequency(freq_hz: int | None) -> str:
    if freq_hz is None:
        return "— . — — — . — — —"
    s = f"{freq_hz:010d}"  # Hz, 10 digits fits up to 9.999 GHz
    mhz, khz, hz = s[:-6].lstrip("0") or "0", s[-6:-3], s[-3:]
    return f"{mhz}.{khz}.{hz}"


def smeter_label(raw: int | None) -> str:
    """Rough S-unit label from the raw 0-255 CI-V meter reading.
    ponytail: linear approximation between Icom's own published
    calibration points (S9≈141, S9+60≈241), not per-radio calibrated —
    good enough for an at-a-glance indicator, not a calibrated meter."""
    if raw is None:
        return "—"
    if raw <= 141:
        return f"S{max(0, round(raw / 141 * 9))}"
    return f"S9+{round((raw - 141) / (241 - 141) * 60)}"


# Dark rig-style look, roughly matching Icom RS-BA1 v2's Operate window —
# scoped to this widget's objectName so it never bleeds into the rest of
# the app (which uses the plain OS theme).
RS_BA1_STYLE = """
QWidget#radioPanelRoot { background: #1b1e23; }
QWidget#radioPanelRoot QLabel { color: #c9d3dc; }
QLineEdit#freqDisplay {
    background: #04150c; color: #39ff6a; border: 2px solid #2f3640;
    border-radius: 4px; padding: 4px;
}
QWidget#radioPanelRoot QPushButton {
    background: #2a2f37; color: #dfe6ee; border: 1px solid #3a4048;
    border-radius: 4px; padding: 6px 10px;
}
QWidget#radioPanelRoot QPushButton:hover { background: #333a44; }
QWidget#radioPanelRoot QPushButton:disabled { color: #5a6068; }
QPushButton#modeBtn:checked { background: #3b82f6; color: white; border-color: #3b82f6; }
QPushButton#bandBtn { padding: 4px 8px; font-size: 11px; }
QWidget#radioPanelRoot QComboBox {
    background: #2a2f37; color: #dfe6ee; border: 1px solid #3a4048;
    border-radius: 4px; padding: 4px;
}
QProgressBar#smeterBar { background: #04150c; border: 1px solid #2f3640; border-radius: 3px; }
QProgressBar#smeterBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #39ff6a, stop:0.6 #39ff6a, stop:0.75 #f59e0b, stop:1 #ef4444);
    border-radius: 3px;
}
QWidget#radioPanelRoot QCheckBox { color: #c9d3dc; }
"""


class RadioPanel(QWidget):
    """A separate top-level window (like RS-BA1's own Operate screen is
    separate from its connection window), not embedded in MainWindow —
    ui.py just shows/raises it. No asyncio here — reads session.radio_ctl's
    plain attributes on a timer tick (tick(), called from ui.py's own poll
    timer — same pattern as the audio level meters), and posts commands
    back via run_coroutine_threadsafe, same as the PTT button."""

    def __init__(self, session, loop, parent=None):
        super().__init__(parent)
        self.setObjectName("radioPanelRoot")
        self.setWindowTitle("Радио панел")
        self.setStyleSheet(RS_BA1_STYLE)
        self.session = session
        self.loop = loop
        self._last_freq_shown = None
        self._editing_freq = False  # true while the user is mid-typing — don't clobber it on poll

        self.freq_edit = QLineEdit()
        self.freq_edit.setObjectName("freqDisplay")
        self.freq_edit.setStyleSheet("font-size: 32px; font-weight: bold; font-family: Consolas, monospace;")
        self.freq_edit.setAlignment(Qt.AlignCenter)
        self.freq_edit.returnPressed.connect(self._on_freq_entered)
        self.freq_edit.textEdited.connect(lambda _: setattr(self, "_editing_freq", True))
        self.freq_edit.installEventFilter(self)

        self.step_combo = QComboBox()
        for step in STEPS_HZ:
            label = f"{step} Hz" if step < 1000 else f"{step // 1000} kHz"
            self.step_combo.addItem(label, step)
        self.step_combo.setCurrentIndex(DEFAULT_STEP_INDEX)

        self.down_button = QPushButton("−")
        self.up_button = QPushButton("+")
        self.down_button.clicked.connect(lambda: self._step_frequency(-1))
        self.up_button.clicked.connect(lambda: self._step_frequency(1))

        self.mode_buttons = {}
        mode_row = QHBoxLayout()
        for name in MODE_BUTTONS:
            btn = QPushButton(name)
            btn.setObjectName("modeBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, n=name: self._on_mode_clicked(n))
            self.mode_buttons[name] = btn
            mode_row.addWidget(btn)

        self.smeter_bar = QProgressBar()
        self.smeter_bar.setObjectName("smeterBar")
        self.smeter_bar.setRange(0, 255)
        self.smeter_bar.setTextVisible(False)
        self.smeter_value_label = QLabel("—")
        self.smeter_value_label.setStyleSheet("font-weight: bold; color: #39ff6a;")

        band_row = QHBoxLayout()
        self.band_buttons = []
        for name, freq in BAND_DEFAULTS_HZ.items():
            btn = QPushButton(name)
            btn.setObjectName("bandBtn")
            btn.clicked.connect(lambda checked=False, f=freq: self._set_frequency(f))
            self.band_buttons.append(btn)
            band_row.addWidget(btn)

        # Показва се само когато текущото радио има свързан антенен суич
        # (server/antenna_switch_*.py) — за разлика от усилвателите, НЕ
        # е глобален списък, защото един суич е окачен на точно едно
        # радио (в линията му до антената), не споделен.
        self.antenna_widget = QWidget()
        antenna_layout = QVBoxLayout(self.antenna_widget)
        antenna_layout.setContentsMargins(0, 0, 0, 0)
        antenna_header_row = QHBoxLayout()
        antenna_header_row.addWidget(QLabel("Антена"))
        self.antenna_label = QLabel("")
        self.antenna_label.setStyleSheet("color: gray; font-size: 11px;")
        antenna_header_row.addWidget(self.antenna_label, 1)
        antenna_layout.addLayout(antenna_header_row)
        antenna_btn_row = QHBoxLayout()
        self.antenna_buttons = []
        for port in range(1, 9):
            btn = QPushButton(str(port))
            btn.setObjectName("bandBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, p=port: self._on_antenna_port_clicked(p))
            self.antenna_buttons.append(btn)
            antenna_btn_row.addWidget(btn)
        antenna_layout.addLayout(antenna_btn_row)
        self.antenna_widget.hide()  # tick() reveals it once a linked switch is known — nothing to show before that

        # Работи над RC-28 (client/rc28.py) наравно с този панел — и
        # двата слушат едни и същи CAT отговори през ComRelay.cat_listeners
        # (виж session.py's _start_rc28). Отделен toggle тук вместо само
        # в "Настройки", защото включване/изключване на живо не изисква
        # затваряне на диалог + пренасочване на радиото.
        self.rc28_checkbox = QCheckBox("RC-28")
        self.rc28_checkbox.setChecked(bool(session.app_cfg.get("rc28", {}).get("enabled", False)))
        self.rc28_checkbox.toggled.connect(self._on_rc28_toggled)
        self.rc28_status_label = QLabel("")
        self.rc28_status_label.setStyleSheet("color: gray; font-size: 11px;")

        self.unavailable_label = QLabel(
            "Няма CI-V адрес за това радио — радио панелът изисква настроен CI-V адрес в admin панела."
        )
        self.unavailable_label.setStyleSheet("color: #f59e0b;")
        self.unavailable_label.setWordWrap(True)
        self.unavailable_label.hide()

        freq_row = QHBoxLayout()
        freq_row.addWidget(self.down_button)
        freq_row.addWidget(self.freq_edit, 1)
        freq_row.addWidget(self.up_button)
        freq_row.addWidget(self.step_combo)

        layout = QVBoxLayout(self)
        layout.addWidget(self.unavailable_label)
        layout.addLayout(freq_row)
        layout.addLayout(mode_row)
        meter_row = QHBoxLayout()
        meter_row.addWidget(QLabel("S-метър"))
        meter_row.addWidget(self.smeter_bar, 1)
        meter_row.addWidget(self.smeter_value_label)
        layout.addLayout(meter_row)
        layout.addLayout(band_row)
        layout.addWidget(self.antenna_widget)
        rc28_row = QHBoxLayout()
        rc28_row.addWidget(self.rc28_checkbox)
        rc28_row.addWidget(self.rc28_status_label, 1)
        layout.addLayout(rc28_row)

        self.resize(440, 320)

    def eventFilter(self, obj, event):
        # Scroll wheel over the frequency field steps it, same convention
        # as most CAT software's frequency display.
        if obj is self.freq_edit and event.type() == QEvent.Wheel:
            self._step_frequency(1 if event.angleDelta().y() > 0 else -1)
            return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        # The window's own [X] closes it like any other window, but this
        # instance (and its tick()-driven state) lives for the whole
        # client session — hide instead, so open_or_raise() reopens the
        # same window instead of needing to rebuild it.
        event.ignore()
        self.hide()

    def open_or_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _step_frequency(self, direction: int):
        ctl = self.session.radio_ctl
        if not ctl or ctl.frequency_hz is None:
            return
        self._set_frequency(ctl.frequency_hz + direction * self.step_combo.currentData())

    def _set_frequency(self, freq_hz: int):
        ctl = self.session.radio_ctl
        if not ctl:
            return
        asyncio.run_coroutine_threadsafe(ctl.set_frequency(max(0, freq_hz)), self.loop)
        self._editing_freq = False

    def _on_freq_entered(self):
        text = self.freq_edit.text().replace(".", "").replace(",", "").replace(" ", "").strip()
        if text.isdigit():
            self._set_frequency(int(text))
        else:
            self._editing_freq = False  # bad input — next poll tick redraws the real value

    def _on_mode_clicked(self, name: str):
        ctl = self.session.radio_ctl
        mode_byte = NAME_TO_MODE.get(name)
        if not ctl or mode_byte is None:
            return
        asyncio.run_coroutine_threadsafe(ctl.set_mode(mode_byte, ctl.filter_num or 1), self.loop)

    def _current_antenna_switch(self) -> dict | None:
        return next(
            (s for s in self.session.antenna_switches if s["linked_radio"] == self.session.radio_name), None
        )

    def _on_antenna_port_clicked(self, port: int):
        switch = self._current_antenna_switch()
        if switch:
            asyncio.run_coroutine_threadsafe(self.session.set_antenna_switch_port(switch["name"], port), self.loop)

    def _update_antenna_switch(self):
        switch = self._current_antenna_switch()
        self.antenna_widget.setVisible(switch is not None)
        if not switch:
            return
        # Сървърът вече отказва превключване докато радиото предава (виж
        # AntennaSwitchManager.is_radio_transmitting) — тук само пестим
        # на оператора един безполезен клик, не е защитата.
        transmitting = bool(self.session.control and self.session.control.busy_by)
        enabled = switch["can_control"] and not transmitting
        labels = switch.get("port_labels") or []
        for port, btn in enumerate(self.antenna_buttons, start=1):
            btn.setEnabled(enabled)
            btn.setChecked(port == switch.get("port"))
            label = labels[port - 1] if port - 1 < len(labels) else ""
            btn.setToolTip(f"Порт {port}: {label}" if label else f"Порт {port}")
        status = switch["name"]
        current_label = labels[switch["port"] - 1] if switch.get("port") and switch["port"] - 1 < len(labels) else ""
        if current_label:
            status += f" — {current_label}"
        if transmitting:
            status += " — блокирано (предава)"
        elif not switch["can_control"]:
            status += " — нямаш права"
        self.antenna_label.setText(status)

    def _on_rc28_toggled(self, checked: bool):
        asyncio.run_coroutine_threadsafe(self.session.set_rc28_enabled(checked), self.loop)

    def _update_rc28_status(self):
        if self.session.rc28_error:
            self.rc28_status_label.setText(self.session.rc28_error)
            self.rc28_status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
        elif self.session.rc28:
            self.rc28_status_label.setText("активен")
            self.rc28_status_label.setStyleSheet("color: #34d399; font-size: 11px;")
        elif self.rc28_checkbox.isChecked():
            self.rc28_status_label.setText("свързване...")
            self.rc28_status_label.setStyleSheet("color: gray; font-size: 11px;")
        else:
            self.rc28_status_label.setText("")

    def tick(self):
        ctl = self.session.radio_ctl
        available = ctl is not None
        self.unavailable_label.setVisible(not available)
        for w in (self.freq_edit, self.step_combo, self.up_button, self.down_button, self.rc28_checkbox, *self.band_buttons):
            w.setEnabled(available)
        for btn in self.mode_buttons.values():
            btn.setEnabled(available)
        self._update_rc28_status()
        self._update_antenna_switch()
        if not available:
            self.smeter_bar.setValue(0)
            self.smeter_value_label.setText("—")
            return

        if not self._editing_freq and ctl.frequency_hz != self._last_freq_shown:
            self.freq_edit.setText(format_frequency(ctl.frequency_hz))
            self._last_freq_shown = ctl.frequency_hz

        mode_name = MODE_NAMES.get(ctl.mode)
        for name, btn in self.mode_buttons.items():
            btn.setChecked(name == mode_name)

        self.smeter_bar.setValue(ctl.smeter or 0)
        self.smeter_value_label.setText(smeter_label(ctl.smeter))


if __name__ == "__main__":
    assert format_frequency(14250000) == "14.250.000"
    assert format_frequency(7000000) == "7.000.000"
    assert format_frequency(0) == "0.000.000"
    assert format_frequency(None) == "— . — — — . — — —"

    assert smeter_label(None) == "—"
    assert smeter_label(0) == "S0"
    assert smeter_label(141) == "S9"
    assert smeter_label(241) == "S9+60"
    assert smeter_label(191) == "S9+30"

    print("radio_panel.py: ok (pure-logic checks only — Qt widget construction needs a QApplication)")
