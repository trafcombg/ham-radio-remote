"""Standalone window listing every configured antenna switch (server/
antenna_switch_*.py). Deliberately its own top-level window, not part of
the per-radio panel (client/radio_panel.py): a switch is a standalone
device, not tied to any one radio, so it doesn't belong scoped to
whichever radio happens to be selected.
"""

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

PORT_COUNT = 8


def status_text(switch: dict) -> str:
    labels = switch.get("port_labels") or []
    port = switch.get("port")
    current_label = labels[port - 1] if port and port - 1 < len(labels) else ""
    text = f"Порт {port}" if port else "—"
    if current_label:
        text += f" — {current_label}"
    if not switch.get("can_control"):
        text += " — нямаш права"
    return text


class AntennaSwitchPanel(QWidget):
    """No asyncio here — reads session.antenna_switches (polled by
    session.py's own loop) on a timer tick, same pattern as ui.py's
    amplifier list. Rebuilds its rows only when something actually
    changed (name/port/can_control signature), not on every tick."""

    def __init__(self, session, loop, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Антенни суичове")
        self.session = session
        self.loop = loop
        self._signature = None
        self._rows: dict = {}  # switch name -> {"buttons": [...], "status": QLabel}

        self.empty_label = QLabel("Няма конфигурирани антенни суичове.")
        self.container = QVBoxLayout()

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty_label)
        layout.addLayout(self.container)
        layout.addStretch()
        self.resize(420, 300)

    def closeEvent(self, event):
        # Same convention as RadioPanel: this window lives for the whole
        # client session, [X] just hides it so open_or_raise() reopens
        # the same instance instead of rebuilding it from scratch.
        event.ignore()
        self.hide()

    def open_or_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_port_clicked(self, name: str, port: int):
        asyncio.run_coroutine_threadsafe(self.session.set_antenna_switch_port(name, port), self.loop)

    def tick(self):
        switches = self.session.antenna_switches
        # port_labels is in the signature too — an admin edit to a port's
        # name must trigger a rebuild the same way a port change does,
        # now that the label is shown on the panel itself, not just in
        # each button's tooltip.
        signature = tuple(
            (s["name"], s.get("port"), s["can_control"], tuple(s.get("port_labels") or [])) for s in switches
        )
        if signature == self._signature:
            return
        self._signature = signature
        self._rebuild(switches)

    def _rebuild(self, switches: list):
        while self.container.count():
            item = self.container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}

        self.empty_label.setVisible(not switches)
        for switch in switches:
            name = switch["name"]
            box = QGroupBox(name)
            box_layout = QVBoxLayout(box)
            btn_row = QHBoxLayout()
            labels = switch.get("port_labels") or []
            buttons = []
            for port in range(1, PORT_COUNT + 1):
                btn = QPushButton(str(port))
                btn.setCheckable(True)
                btn.setChecked(port == switch.get("port"))
                btn.setEnabled(switch["can_control"])
                label = labels[port - 1] if port - 1 < len(labels) else ""
                btn.setToolTip(f"Порт {port}: {label}" if label else f"Порт {port}")
                btn.clicked.connect(lambda checked=False, n=name, p=port: self._on_port_clicked(n, p))
                buttons.append(btn)

                label_widget = QLabel(label)
                label_widget.setAlignment(Qt.AlignCenter)
                label_widget.setWordWrap(True)
                label_widget.setStyleSheet("font-size: 10px; color: gray;")
                label_widget.setFixedWidth(60)

                port_col = QVBoxLayout()
                port_col.addWidget(btn)
                port_col.addWidget(label_widget)
                btn_row.addLayout(port_col)
            box_layout.addLayout(btn_row)
            status_label = QLabel(status_text(switch))
            status_label.setStyleSheet("color: gray; font-size: 11px;")
            box_layout.addWidget(status_label)
            self.container.addWidget(box)
            self._rows[name] = {"buttons": buttons, "status": status_label}


if __name__ == "__main__":
    assert status_text({"port": None, "port_labels": [], "can_control": True}) == "—"
    assert status_text({"port": 3, "port_labels": [], "can_control": True}) == "Порт 3"
    assert status_text({"port": 3, "port_labels": ["", "", "40m Dipole"], "can_control": True}) == "Порт 3 — 40m Dipole"
    assert status_text({"port": 1, "port_labels": ["Vertical"], "can_control": False}) == \
        "Порт 1 — Vertical — нямаш права"

    from PySide6.QtWidgets import QApplication

    class _FakeSession:
        antenna_switches = [
            {"name": "SW1", "port": 3, "can_control": True, "port_labels": ["Vertical", "", "40m Dipole"]},
        ]

    app = QApplication.instance() or QApplication([])
    panel = AntennaSwitchPanel(_FakeSession(), loop=None)
    panel.tick()
    row = panel._rows["SW1"]
    assert row["buttons"][0].toolTip() == "Порт 1: Vertical"
    assert row["buttons"][1].toolTip() == "Порт 2"  # no label configured for this port
    assert row["buttons"][2].isChecked() is True  # port 3 is the switch's current port

    # An admin edit to a port's label (port/can_control unchanged) must
    # still trigger a rebuild now that the label is shown on the panel.
    _FakeSession.antenna_switches[0]["port_labels"] = ["Vertical", "80m Dipole", "40m Dipole"]
    panel.tick()
    assert panel._rows["SW1"]["buttons"][1].toolTip() == "Порт 2: 80m Dipole"

    print("antenna_switch_panel.py: ok")
