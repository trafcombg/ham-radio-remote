"""Built-in radio control panel — frequency, mode, and S-meter, driven
over the same CAT tunnel real CAT software (WSJT-X, N1MM+...) uses.
Modeled on Icom RS-BA1 v2's Operate screen (a separate window from the
main connection window, dark rig-style readout, big frequency digits,
mode buttons, S-meter) — for when the operator doesn't want to run a
separate CAT app just to change frequency or mode.

The FILTER/MEMORY/SIGNALING/TUNER/AGC/etc. controls below are laid out
to match RS-BA1's Operate screen but are placeholders (permanently
disabled) — Icom's own RS-BA1 .ini files (RS-BA1/RemoteController/models/)
don't carry the CI-V command bytes for most of them (blank CMD= fields
mean RS-BA1 uses fixed opcodes baked into its own binary, not written to
the .ini), and guessing write-command bytes for a live transmitter isn't
something to do without a verified command reference. Wire each one up
(common/civ.py + here) once its exact CI-V command bytes are confirmed.

ponytail: RS-BA1 also has a spectrum scope and RIT/XIT — none of that is
here, not even as a placeholder. Add the piece that's actually needed
(CI-V scope data is cmd 0x27) rather than building the whole thing
speculatively.
"""

import asyncio

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDial, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
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

# Placeholder controls only — see module docstring. Labels match RS-BA1's
# own Operate screen so the layout is recognizable to anyone who's used it.
FILTER_BUTTONS = ["NOTCH", "TWIN PBT", "NB", "NR", "APF", "TPF", "COMP", "VSC", "DIGI-SEL"]
MEMORY_BUTTONS = ["MP-W", "MP-R", "A/B", "SPLIT", "DUP"]
SIGNALING_BUTTONS = ["TONE", "DTCS"]
AGC_OPTIONS = [("FAST", 1), ("MID", 2), ("SLOW", 3)]  # values per RS-BA1's IC-7300.ini [AGC] section

_UNSET = object()  # sentinel — see RadioPanel._last_freq_shown


def format_frequency(freq_hz: int | None) -> str:
    if freq_hz is None:
        return "— . — — — . — — —"
    s = f"{freq_hz:010d}"  # Hz, 10 digits fits up to 9.999 GHz
    mhz, khz, hz = s[:-6].lstrip("0") or "0", s[-6:-3], s[-3:]
    return f"{mhz}.{khz}.{hz}"


def _dial_wrap_delta(old: int, new: int, modulo: int = 100) -> int:
    """Signed step direction from a wrapping QDial's old->new value change.
    A wrapping QDial (see RadioPanel.tuning_dial) reports 0-99 and jumps
    99->0 or 0->99 on every full turn — a plain `new - old` gets that
    wraparound backwards, reporting a huge jump the wrong direction
    instead of a one-step turn."""
    delta = new - old
    if delta > modulo // 2:
        delta -= modulo
    elif delta < -(modulo // 2):
        delta += modulo
    return delta


def smeter_label(raw: int | None) -> str:
    """Rough S-unit label from the raw 0-255 CI-V meter reading. Linear
    approximation between the IC-7300 Full Manual's own published
    calibration points (S0=0, S9=120, S9+60dB=241) — good enough for an
    at-a-glance indicator, not a calibrated meter."""
    if raw is None:
        return "—"
    if raw <= 120:
        return f"S{max(0, round(raw / 120 * 9))}"
    return f"S9+{round((raw - 120) / (241 - 120) * 60)}"


# Dark rig-style look, roughly matching Icom RS-BA1 v2's Operate window —
# scoped to this widget's objectName so it never bleeds into the rest of
# the app (which uses the plain OS theme).
RS_BA1_STYLE = """
QWidget#radioPanelRoot { background: #12151a; }
QWidget#radioPanelRoot QLabel { color: #c9d3dc; }
QLabel#sectionLabel { color: #7d8792; font-size: 10px; font-weight: bold; }
QLineEdit#freqDisplay {
    background: #000000; color: #4fd8ff; border: 2px solid #4a4030;
    border-radius: 4px; padding: 4px;
}
QWidget#radioPanelRoot QPushButton {
    background: #2a2f37; color: #dfe6ee; border: 1px solid #3a4048;
    border-radius: 4px; padding: 6px 10px;
}
QWidget#radioPanelRoot QPushButton:hover { background: #333a44; }
QWidget#radioPanelRoot QPushButton:disabled { color: #4b5058; background: #23262b; }
QWidget#radioPanelRoot QPushButton:checked { background: #3d4a3d; border-color: #34d399; color: #34d399; }
QWidget#radioPanelRoot QPushButton#modeBtn:checked { background: #2f7fd6; color: white; border-color: #2f7fd6; }
QPushButton#bandBtn, QPushButton#smallBtn { padding: 4px 8px; font-size: 11px; }
QWidget#radioPanelRoot QComboBox {
    background: #2a2f37; color: #dfe6ee; border: 1px solid #3a4048;
    border-radius: 4px; padding: 4px;
}
QWidget#radioPanelRoot QComboBox:disabled { color: #4b5058; }
QProgressBar#smeterBar { background: #000000; border: 1px solid #2f3640; border-radius: 3px; }
QProgressBar#smeterBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #39ff6a, stop:0.6 #39ff6a, stop:0.75 #f59e0b, stop:1 #ef4444);
    border-radius: 3px;
}
QLabel#smeterTicks { color: #7d8792; font-size: 9px; font-family: Consolas, monospace; }
QWidget#radioPanelRoot QCheckBox { color: #c9d3dc; }
QWidget#radioPanelRoot QDial { background: transparent; }
QLabel#dialLabel { color: #9aa4ae; font-size: 10px; qproperty-alignment: AlignCenter; }
QPushButton#txRxIndicator { font-weight: bold; font-size: 15px; padding: 8px; }
"""

# Applied directly (not via the QSS above) since it changes with live
# radio state — tick() picks one of these three per poll.
TX_RX_STYLES = {
    True: "font-weight: bold; font-size: 15px; padding: 8px; background: #3a1414; border: 2px solid #ef4444; color: #ef4444;",
    False: "font-weight: bold; font-size: 15px; padding: 8px; background: #12241c; border: 2px solid #34d399; color: #34d399;",
    None: "font-weight: bold; font-size: 15px; padding: 8px; background: #1e2126; border: 2px solid #5a6068; color: #5a6068;",
}


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
        self.setWindowFlag(Qt.WindowStaysOnTopHint)
        self.setStyleSheet(RS_BA1_STYLE)
        self.session = session
        self.loop = loop
        # Sentinel, not None: ctl.frequency_hz also STARTS as None (no
        # reply yet), so a plain None-vs-None comparison here would never
        # be true on the very first tick and the "— . — — — . — — —"
        # waiting placeholder would never actually render — the box would
        # just sit empty (indistinguishable from "broken") until the
        # radio's first real reply arrived.
        self._last_freq_shown = _UNSET
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
        self.smeter_value_label.setStyleSheet("font-weight: bold; color: #4fd8ff;")
        smeter_ticks = QLabel("S1   3   5   7   9   +20   +40   +60")
        smeter_ticks.setObjectName("smeterTicks")

        # --- Left column: TX/RX + PTT, rig controls, vertical band list ---
        # Doubles as the PTT button (press-and-hold, same session.control
        # call the main window's own PTT button uses) — RS-BA1's TRANSMIT
        # indicator is clickable the same way.
        self.tx_rx_button = QPushButton("RX")
        self.tx_rx_button.setObjectName("txRxIndicator")
        self.tx_rx_button.setToolTip("Задръж за предаване (PTT)")
        self.tx_rx_button.pressed.connect(lambda: self._send_ptt(True))
        self.tx_rx_button.released.connect(lambda: self._send_ptt(False))

        self.po_meter_bar = QProgressBar()
        self.po_meter_bar.setObjectName("smeterBar")
        self.po_meter_bar.setRange(0, 255)
        self.po_meter_bar.setTextVisible(False)
        po_label = QLabel("PWR")
        po_label.setObjectName("sectionLabel")

        self.tuner_button = QPushButton("TUNER")
        self.pamp_button = QPushButton("P.AMP")
        self.att_button = QPushButton("ATT")
        for btn in (self.tuner_button, self.pamp_button, self.att_button):
            btn.setCheckable(True)
            btn.setEnabled(False)
        self.agc_combo = QComboBox()
        for label, value in AGC_OPTIONS:
            self.agc_combo.addItem(label, value)
        self.agc_combo.setEnabled(False)

        self.band_buttons = []
        band_col = QVBoxLayout()
        for name, freq in BAND_DEFAULTS_HZ.items():
            btn = QPushButton(name)
            btn.setObjectName("bandBtn")
            btn.clicked.connect(lambda checked=False, f=freq: self._set_frequency(f))
            self.band_buttons.append(btn)
            band_col.addWidget(btn)

        left_col = QVBoxLayout()
        left_col.addWidget(self.tx_rx_button)
        left_col.addWidget(po_label)
        left_col.addWidget(self.po_meter_bar)
        left_col.addWidget(self.tuner_button)
        left_col.addWidget(self.pamp_button)
        left_col.addWidget(self.att_button)
        left_col.addWidget(self.agc_combo)
        band_label = QLabel("BAND")
        band_label.setObjectName("sectionLabel")
        left_col.addWidget(band_label)
        left_col.addLayout(band_col)
        left_col.addStretch(1)

        # --- Center column: frequency, mode, S-meter ---
        freq_row = QHBoxLayout()
        freq_row.addWidget(self.down_button)
        freq_row.addWidget(self.freq_edit, 1)
        freq_row.addWidget(self.up_button)
        freq_row.addWidget(self.step_combo)

        meter_row = QHBoxLayout()
        meter_row.addWidget(QLabel("S"))
        meter_row.addWidget(self.smeter_bar, 1)
        meter_row.addWidget(self.smeter_value_label)

        # Real, wired tuning knob — unlike the placeholder dials below,
        # this one reuses the already-working set_frequency CI-V command
        # (same as the +/- step buttons), so it's genuine functionality,
        # not a visual stand-in. QDial has no "infinite spin" mode, so it
        # wraps 0-99 and _dial_wrap_delta() turns each wrap into a step.
        self.tuning_dial = QDial()
        self.tuning_dial.setRange(0, 99)
        self.tuning_dial.setWrapping(True)
        self.tuning_dial.setNotchesVisible(True)
        self.tuning_dial.setFixedSize(84, 84)
        self._tuning_dial_last = self.tuning_dial.value()
        self.tuning_dial.valueChanged.connect(self._on_tuning_dial_changed)
        tuning_label = QLabel("TUNING")
        tuning_label.setObjectName("dialLabel")
        tuning_col = QVBoxLayout()
        tuning_col.addWidget(self.tuning_dial, alignment=Qt.AlignCenter)
        tuning_col.addWidget(tuning_label)
        meter_row.addLayout(tuning_col)

        center_col = QVBoxLayout()
        center_col.addLayout(freq_row)
        center_col.addLayout(mode_row)
        center_col.addWidget(smeter_ticks)
        center_col.addLayout(meter_row)
        center_col.addStretch(1)

        # --- Right column: FILTER grid (NOTCH/NB/NR/COMP/TPF wired for
        # IC-7300; TWIN PBT/APF/VSC/DIGI-SEL stay disabled — no confirmed
        # single-toggle CI-V command, or not supported on this model) ---
        self.filter_buttons = {}
        filter_grid = QGridLayout()
        for i, name in enumerate(FILTER_BUTTONS):
            btn = QPushButton(name)
            btn.setObjectName("smallBtn")
            btn.setCheckable(True)
            btn.setEnabled(False)
            self.filter_buttons[name] = btn
            filter_grid.addWidget(btn, i // 2, i % 2)
        filter_label = QLabel("FILTER")
        filter_label.setObjectName("sectionLabel")
        right_col = QVBoxLayout()
        right_col.addWidget(filter_label)
        right_col.addLayout(filter_grid)
        right_col.addStretch(1)

        content_row = QHBoxLayout()
        content_row.addLayout(left_col)
        content_row.addLayout(center_col, 1)
        content_row.addLayout(right_col)

        # --- Bottom: MEMORY / SIGNALING placeholder rows + level sliders ---
        self.memory_buttons = {}
        memory_row = QHBoxLayout()
        memory_label = QLabel("MEMORY")
        memory_label.setObjectName("sectionLabel")
        memory_row.addWidget(memory_label)
        for name in MEMORY_BUTTONS:
            btn = QPushButton(name)
            btn.setObjectName("smallBtn")
            if name == "SPLIT":
                btn.setCheckable(True)  # wired (cmd 0F) — a plain on/off, unlike MP-W/MP-R/A-B/DUP
            else:
                btn.setEnabled(False)
            self.memory_buttons[name] = btn
            memory_row.addWidget(btn)
        self.signaling_buttons = {}
        signaling_label = QLabel("SIGNALING")
        signaling_label.setObjectName("sectionLabel")
        memory_row.addSpacing(12)
        memory_row.addWidget(signaling_label)
        for name in SIGNALING_BUTTONS:
            btn = QPushButton(name)
            btn.setObjectName("smallBtn")
            btn.setCheckable(True)
            btn.setEnabled(False)
            self.signaling_buttons[name] = btn
            memory_row.addWidget(btn)
        memory_row.addStretch(1)

        def _labeled_dial(text):
            dial = QDial()
            dial.setRange(0, 255)
            dial.setNotchesVisible(True)
            dial.setFixedSize(52, 52)
            dial.setEnabled(False)
            label = QLabel(text)
            label.setObjectName("dialLabel")
            col = QVBoxLayout()
            col.addWidget(dial, alignment=Qt.AlignCenter)
            col.addWidget(label)
            return dial, col

        self.af_gain_dial, af_gain_col = _labeled_dial("AF GAIN")
        self.rf_gain_dial, rf_gain_col = _labeled_dial("RF GAIN")
        self.mic_gain_dial, mic_gain_col = _labeled_dial("MIC GAIN")
        self.rf_power_dial, rf_power_col = _labeled_dial("RF POWER")
        self.cw_pitch_dial, cw_pitch_col = _labeled_dial("CW PITCH")
        knobs_row = QHBoxLayout()
        for col in (af_gain_col, rf_gain_col, mic_gain_col, rf_power_col, cw_pitch_col):
            knobs_row.addLayout(col)

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

        # Start/stop for the CAT polling itself (client/radio_panel_ctl.py)
        # — independent of RC-28's own toggle above. Deliberately NOT in
        # tick()'s "disable while unavailable" list: unavailable can BE
        # "the operator turned this off", so the checkbox must stay
        # clickable to turn it back on.
        self.cat_enabled_checkbox = QCheckBox("CAT връзка с радиото")
        self.cat_enabled_checkbox.setChecked(bool(session.app_cfg.get("radio_panel", {}).get("enabled", True)))
        self.cat_enabled_checkbox.toggled.connect(self._on_cat_enabled_toggled)

        self.unavailable_label = QLabel()
        self.unavailable_label.setStyleSheet("color: #f59e0b;")
        self.unavailable_label.setWordWrap(True)
        self.unavailable_label.hide()

        # --- Wiring for the extra rig controls (RadioPanelController.toggles/
        # levels) — only fires real CI-V commands for controls the current
        # radio's model actually supports (see common/civ_models.py); tick()
        # disables everything else, same as it always did for an unavailable ctl.
        self._toggle_widgets = {
            "tuner": self.tuner_button,
            "preamp": self.pamp_button,
            "noise_blanker": self.filter_buttons["NB"],
            "noise_reduction": self.filter_buttons["NR"],
            "auto_notch": self.filter_buttons["NOTCH"],
            "compressor": self.filter_buttons["COMP"],
            "twin_peak_filter": self.filter_buttons["TPF"],
            "repeater_tone": self.signaling_buttons["TONE"],
        }
        for name, btn in self._toggle_widgets.items():
            btn.clicked.connect(lambda checked, n=name: self._on_toggle_clicked(n, checked))
        self.agc_combo.currentIndexChanged.connect(self._on_agc_changed)
        self.att_button.clicked.connect(self._on_attenuator_clicked)
        self.memory_buttons["SPLIT"].clicked.connect(self._on_split_clicked)

        self._level_widgets = {
            "af_gain": self.af_gain_dial,
            "rf_gain": self.rf_gain_dial,
            "mic_gain": self.mic_gain_dial,
            "rf_power": self.rf_power_dial,
            "cw_pitch": self.cw_pitch_dial,
        }
        for name, dial in self._level_widgets.items():
            dial.valueChanged.connect(lambda value, n=name: self._on_level_changed(n, value))

        layout = QVBoxLayout(self)
        layout.addWidget(self.cat_enabled_checkbox)
        layout.addWidget(self.unavailable_label)
        layout.addLayout(content_row, 1)
        layout.addLayout(memory_row)
        layout.addLayout(knobs_row)
        rc28_row = QHBoxLayout()
        rc28_row.addWidget(self.rc28_checkbox)
        rc28_row.addWidget(self.rc28_status_label, 1)
        layout.addLayout(rc28_row)

        self.resize(620, 480)

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

    def _on_tuning_dial_changed(self, value: int):
        delta = _dial_wrap_delta(self._tuning_dial_last, value, self.tuning_dial.maximum() + 1)
        self._tuning_dial_last = value
        if delta:
            self._step_frequency(1 if delta > 0 else -1)

    def _send_ptt(self, on: bool):
        if self.session.control:
            asyncio.run_coroutine_threadsafe(self.session.control.request_ptt(on), self.loop)

    def _on_toggle_clicked(self, name: str, checked: bool):
        ctl = self.session.radio_ctl
        if not ctl or name not in ctl.toggles:
            return
        asyncio.run_coroutine_threadsafe(ctl.set_toggle(name, 1 if checked else 0), self.loop)

    def _on_agc_changed(self, index: int):
        ctl = self.session.radio_ctl
        if not ctl or not self.agc_combo.isEnabled() or "agc" not in ctl.toggles:
            return
        value = self.agc_combo.itemData(index)
        if value is not None:
            asyncio.run_coroutine_threadsafe(ctl.set_toggle("agc", value), self.loop)

    def _on_attenuator_clicked(self, checked: bool):
        ctl = self.session.radio_ctl
        if not ctl:
            return
        asyncio.run_coroutine_threadsafe(ctl.set_attenuator(checked), self.loop)

    def _on_split_clicked(self, checked: bool):
        ctl = self.session.radio_ctl
        if not ctl:
            return
        asyncio.run_coroutine_threadsafe(ctl.set_split(checked), self.loop)

    def _on_level_changed(self, name: str, value: int):
        ctl = self.session.radio_ctl
        if not ctl or not self._level_widgets[name].isEnabled() or name not in ctl.levels:
            return
        asyncio.run_coroutine_threadsafe(ctl.set_level(name, value), self.loop)

    def _on_rc28_toggled(self, checked: bool):
        asyncio.run_coroutine_threadsafe(self.session.set_rc28_enabled(checked), self.loop)

    def _on_cat_enabled_toggled(self, checked: bool):
        asyncio.run_coroutine_threadsafe(self.session.set_radio_panel_enabled(checked), self.loop)

    def _update_rc28_status(self):
        if self.session.rc28_error:
            self.rc28_status_label.setText(self.session.rc28_error)
            self.rc28_status_label.setStyleSheet("color: #ef4444; font-size: 11px;")
        elif self.session.rc28:
            self.rc28_status_label.setText(f"активен — {self.session.radio_name}")
            self.rc28_status_label.setStyleSheet("color: #34d399; font-size: 11px;")
        elif self.rc28_checkbox.isChecked():
            self.rc28_status_label.setText("свързване...")
            self.rc28_status_label.setStyleSheet("color: gray; font-size: 11px;")
        else:
            self.rc28_status_label.setText("")

    def tick(self):
        ctl = self.session.radio_ctl
        available = ctl is not None
        if not available:
            self.unavailable_label.setText(
                "CAT връзката с радиото е спряна — включи отметката по-горе, за да я стартираш."
                if not self.session.radio_panel_enabled else
                "Няма CI-V адрес за това радио — радио панелът изисква настроен CI-V адрес в admin панела."
            )
        self.unavailable_label.setVisible(not available)
        for w in (self.freq_edit, self.step_combo, self.up_button, self.down_button, self.tuning_dial,
                  self.rc28_checkbox, self.att_button, self.memory_buttons["SPLIT"], *self.band_buttons):
            w.setEnabled(available)
        for btn in self.mode_buttons.values():
            btn.setEnabled(available)
        self._update_rc28_status()
        if not available:
            self.smeter_bar.setValue(0)
            self.smeter_value_label.setText("—")
            self.po_meter_bar.setValue(0)
            self.tx_rx_button.setText("—")
            self.tx_rx_button.setStyleSheet(TX_RX_STYLES[None])
            for btn in self._toggle_widgets.values():
                btn.setEnabled(False)
            self.agc_combo.setEnabled(False)
            for dial in self._level_widgets.values():
                dial.setEnabled(False)
            return

        if not self._editing_freq and ctl.frequency_hz != self._last_freq_shown:
            self.freq_edit.setText(format_frequency(ctl.frequency_hz))
            self._last_freq_shown = ctl.frequency_hz

        mode_name = MODE_NAMES.get(ctl.mode)
        for name, btn in self.mode_buttons.items():
            btn.setChecked(name == mode_name)

        self.smeter_bar.setValue(ctl.smeter or 0)
        self.smeter_value_label.setText(smeter_label(ctl.smeter))

        self.tx_rx_button.setText({True: "TX", False: "RX"}.get(ctl.transmitting, "—"))
        self.tx_rx_button.setStyleSheet(TX_RX_STYLES[ctl.transmitting])
        self.po_meter_bar.setValue(ctl.levels.get("po_meter") or 0)

        self.att_button.setChecked(bool(ctl.attenuator_db))
        self.memory_buttons["SPLIT"].setChecked(bool(ctl.split))

        for name, btn in self._toggle_widgets.items():
            supported = name in ctl.commands["toggles"]
            btn.setEnabled(supported)
            if supported and ctl.toggles[name] is not None:
                btn.setChecked(ctl.toggles[name] == 1)

        agc_supported = "agc" in ctl.commands["toggles"]
        self.agc_combo.setEnabled(agc_supported)
        if agc_supported and ctl.toggles.get("agc") is not None:
            idx = self.agc_combo.findData(ctl.toggles["agc"])
            if idx >= 0 and idx != self.agc_combo.currentIndex():
                self.agc_combo.blockSignals(True)
                self.agc_combo.setCurrentIndex(idx)
                self.agc_combo.blockSignals(False)

        for name, dial in self._level_widgets.items():
            supported = name in ctl.commands["levels"]
            dial.setEnabled(supported)
            value = ctl.levels.get(name)
            if supported and value is not None and not dial.isSliderDown() and value != dial.value():
                dial.blockSignals(True)
                dial.setValue(value)
                dial.blockSignals(False)


if __name__ == "__main__":
    assert format_frequency(14250000) == "14.250.000"
    assert format_frequency(7000000) == "7.000.000"
    assert format_frequency(0) == "0.000.000"
    assert format_frequency(None) == "— . — — — . — — —"

    assert smeter_label(None) == "—"
    assert smeter_label(0) == "S0"
    assert smeter_label(120) == "S9"
    assert smeter_label(241) == "S9+60"
    assert smeter_label(180) == "S9+30"

    assert _dial_wrap_delta(10, 15) == 5
    assert _dial_wrap_delta(15, 10) == -5
    assert _dial_wrap_delta(98, 2) == 4    # wrapped forward past 99->0
    assert _dial_wrap_delta(2, 98) == -4   # wrapped backward past 0->99
    assert _dial_wrap_delta(50, 50) == 0

    # Reproduces the report: freq_edit sat completely blank (not even the
    # waiting placeholder) even though the CI-V address was configured and
    # controls were enabled — the None-vs-None sentinel bug above.
    from PySide6.QtWidgets import QApplication

    from client.radio_panel_ctl import RadioPanelController
    from common.civ import CONTROLLER_ADDR, END, PREAMBLE

    async def _noop_send(data):
        pass

    class _FakeSession:
        app_cfg = {"rc28": {"enabled": False}, "radio_panel": {"enabled": True}}
        radio_ctl = RadioPanelController(_noop_send, 0x94, "IC-7300")
        radio_panel_enabled = True
        radio_name = "TEST"
        rc28 = None
        rc28_error = None
        control = None

    app = QApplication.instance() or QApplication([])
    panel = RadioPanel(_FakeSession(), loop=None)
    panel.tick()
    assert panel.freq_edit.text() == format_frequency(None), "waiting placeholder never rendered on first tick"

    # A control the model supports gets enabled the moment ctl exists
    # (same as the existing +/- frequency buttons) — it just has nothing
    # to show yet, same pattern as frequency_hz's own placeholder.
    assert panel.tuner_button.isEnabled() is True
    assert panel.agc_combo.isEnabled() is True
    assert panel.mic_gain_dial.isEnabled() is True
    # Controls with no confirmed CI-V command stay disabled regardless.
    assert panel.filter_buttons["APF"].isEnabled() is False
    assert panel.filter_buttons["TWIN PBT"].isEnabled() is False
    assert panel.signaling_buttons["DTCS"].isEnabled() is False
    assert panel.tx_rx_button.text() == "—"

    ctl = panel.session.radio_ctl
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x1c\x00\x01" + END)  # TX
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x16\x02\x01" + END)  # preamp ON
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x14\x0b\x01\x28" + END)  # mic gain 128
    panel.tick()
    assert panel.tx_rx_button.text() == "TX"
    assert panel.pamp_button.isChecked() is True
    assert panel.mic_gain_dial.value() == 128

    # A model with no entry in common/civ_models.py must degrade to every
    # extra control staying disabled — not a crash, not a guessed command.
    class _FakeSessionUnknownModel(_FakeSession):
        radio_ctl = RadioPanelController(_noop_send, 0x70, "SOME-OTHER-RADIO")

    panel_unknown = RadioPanel(_FakeSessionUnknownModel(), loop=None)
    panel_unknown.tick()
    assert panel_unknown.tuner_button.isEnabled() is False
    assert panel_unknown.agc_combo.isEnabled() is False
    assert panel_unknown.mic_gain_dial.isEnabled() is False

    # No radio selected at all — must not crash touching the new widgets.
    class _FakeSessionNoRadio(_FakeSession):
        radio_ctl = None

    panel_no_radio = RadioPanel(_FakeSessionNoRadio(), loop=None)
    panel_no_radio.tick()
    assert panel_no_radio.tx_rx_button.text() == "—"
    assert "CI-V адрес" in panel_no_radio.unavailable_label.text()

    # Operator turned the CAT checkbox off — must show a different reason
    # than "no CI-V configured", and the checkbox itself must stay
    # clickable (it's the only way to turn it back on).
    class _FakeSessionCatDisabled(_FakeSession):
        radio_ctl = None
        radio_panel_enabled = False

    panel_cat_disabled = RadioPanel(_FakeSessionCatDisabled(), loop=None)
    panel_cat_disabled.tick()
    assert "спряна" in panel_cat_disabled.unavailable_label.text()
    assert panel_cat_disabled.cat_enabled_checkbox.isEnabled()

    # The tuning dial IS wired — spinning it must not crash even with no
    # event loop (loop=None here), since frequency_hz is None so
    # _step_frequency's own guard returns before ever touching self.loop.
    panel.tuning_dial.setValue(3)
    assert panel.tuning_dial.isEnabled()  # ctl exists (even if frequency_hz is None) -> enabled by tick()

    print("radio_panel.py: ok")
