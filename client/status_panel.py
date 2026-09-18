"""Standalone window listing every port/connection the client currently
holds open — CAT (com0com), control TCP, CW UDP, and the active radio's
audio devices — each with a colored LED, so "why isn't audio/CAT working"
doesn't require checking client.log. Same standalone-window convention as
client/antenna_switch_panel.py: read-only, driven by session.py's own
state on a timer tick, no asyncio here.

Each channel also shows a live ↑/↓ throughput (from the cumulative byte
counters the channel objects themselves keep — com_relay.py, control.py,
audio_io.py, cw_link.py) and, for control, the last ping/pong latency.
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from client import audio_devices

LED_COLORS = {"green": "#34d399", "red": "#ef4444", "gray": "#5a6068"}


class _RateMeter:
    """Bytes/sec from a cumulative counter, exponentially smoothed (same
    idea as audio_io._update_jitter) so one busy/quiet 200ms tick doesn't
    make the number jump around."""

    SMOOTHING = 0.3

    def __init__(self):
        self._prev = None  # (monotonic_time, total_bytes)
        self.bytes_per_s = 0.0

    def update(self, total_bytes: int) -> float:
        now = time.monotonic()
        if self._prev is not None:
            dt = now - self._prev[0]
            if dt > 0:
                # clamp negative: a channel object can be replaced (radio
                # switch rebuilds AudioLink/CwLink) and its counter restarts
                # at 0 — treat that tick as "no data yet", not negative speed.
                instant = max(0, total_bytes - self._prev[1]) / dt
                self.bytes_per_s += (instant - self.bytes_per_s) * self.SMOOTHING
        self._prev = (now, total_bytes)
        return self.bytes_per_s


def _fmt_rate(bytes_per_s: float) -> str:
    if bytes_per_s < 1024:
        return f"{bytes_per_s:.0f} B/s"
    return f"{bytes_per_s / 1024:.1f} KB/s"


def _led(color: str) -> QLabel:
    label = QLabel()
    label.setFixedSize(12, 12)
    _set_led(label, color)
    return label


def _set_led(label: QLabel, color: str):
    label.setStyleSheet(f"background: {LED_COLORS[color]}; border-radius: 6px;")


def _device_name(index: int | None, devices: list) -> str:
    if index is None:
        return "по подразбиране"
    return next((name for i, name in devices if i == index), f"устройство #{index}")


class StatusPanel(QWidget):
    """No asyncio here — reads session state on a timer tick (tick(),
    called from ui.py's own poll timer), same pattern as radio_panel.py
    and antenna_switch_panel.py."""

    def __init__(self, session, loop, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Статус на връзки")
        self.setWindowFlag(Qt.WindowStaysOnTopHint)
        self.session = session
        self.loop = loop
        self._radios_signature = None
        self._radio_rows: dict = {}  # radio name -> {"cat_led", "cat_label", "ctl_led", "ctl_label", rate meters}
        self._audio_sent_meter = _RateMeter()
        self._audio_recv_meter = _RateMeter()
        self._cw_sent_meter = _RateMeter()
        self._cw_recv_meter = _RateMeter()

        self.radios_container = QVBoxLayout()

        audio_box = QGroupBox("Аудио (текущо радио)")
        audio_layout = QGridLayout(audio_box)
        self.audio_led = _led("gray")
        self.audio_label = QLabel("—")
        audio_layout.addWidget(self.audio_led, 0, 0)
        audio_layout.addWidget(self.audio_label, 0, 1)

        cw_box = QGroupBox("CW (текущо радио)")
        cw_layout = QGridLayout(cw_box)
        self.cw_led = _led("gray")
        self.cw_label = QLabel("—")
        cw_layout.addWidget(self.cw_led, 0, 0)
        cw_layout.addWidget(self.cw_label, 0, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Радиа — CAT порт / control връзка"))
        layout.addLayout(self.radios_container)
        layout.addWidget(audio_box)
        layout.addWidget(cw_box)
        layout.addStretch()
        self.resize(480, 360)

    def closeEvent(self, event):
        # Same convention as RadioPanel/AntennaSwitchPanel: lives for the
        # whole client session, [X] just hides it.
        event.ignore()
        self.hide()

    def open_or_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def tick(self):
        radios = self.session.app_cfg.get("radios", [])
        signature = tuple(r["name"] for r in radios)
        if signature != self._radios_signature:
            self._radios_signature = signature
            self._rebuild_radio_rows(radios)

        for radio_cfg in radios:
            name = radio_cfg["name"]
            row = self._radio_rows.get(name)
            if row:
                self._update_radio_row(row, radio_cfg)

        self._update_audio()
        self._update_cw()

    def _rebuild_radio_rows(self, radios: list):
        while self.radios_container.count():
            item = self.radios_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._radio_rows = {}

        for radio_cfg in radios:
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.addWidget(QLabel(radio_cfg["name"]))
            cat_led = _led("gray")
            cat_label = QLabel("")
            row.addWidget(cat_led)
            row.addWidget(cat_label, 1)
            ctl_led = _led("gray")
            ctl_label = QLabel("")
            row.addWidget(QLabel("Control:"))
            row.addWidget(ctl_led)
            row.addWidget(ctl_label)
            self.radios_container.addWidget(row_widget)
            self._radio_rows[radio_cfg["name"]] = {
                "cat_led": cat_led, "cat_label": cat_label, "ctl_led": ctl_led, "ctl_label": ctl_label,
                "cat_sent_meter": _RateMeter(), "cat_recv_meter": _RateMeter(),
                "ctl_sent_meter": _RateMeter(), "ctl_recv_meter": _RateMeter(),
            }

    def _update_radio_row(self, row: dict, radio_cfg: dict):
        name = radio_cfg["name"]
        active = radio_cfg.get("active", True)
        com_port = self.session.app_cfg.get("com_ports", {}).get(name, {}).get("local")

        if not active:
            cat_color, cat_text = "gray", "неактивно"
        else:
            # Same three-state categorization as ui.py's own radio list —
            # reused here rather than a fresh busy/free boolean, so the
            # two views of the same state can't drift apart.
            busy_by = self.session.radio_status(name)
            if busy_by:
                cat_color, cat_text = "red", f"{com_port or '—'} — заето от {busy_by}"
            elif self.session.external_cat_busy.get(name):
                cat_color, cat_text = "red", f"{com_port or '—'} — зает порт (CAT програма)"
            elif com_port:
                cat_color, cat_text = "green", f"{com_port} — свободно"
            else:
                cat_color, cat_text = "gray", "няма зададен порт"
        relay = self.session.cat_relays.get(name)
        if active and relay is not None:
            sent = row["cat_sent_meter"].update(relay.bytes_sent)
            recv = row["cat_recv_meter"].update(relay.bytes_recv)
            cat_text += f" · ↑{_fmt_rate(sent)} ↓{_fmt_rate(recv)}"
        _set_led(row["cat_led"], cat_color)
        row["cat_label"].setText(cat_text)

        control_client = self.session.status_clients.get(name)
        connected = bool(active and control_client is not None and control_client.writer is not None)
        if not active:
            ctl_color, ctl_text = "gray", "—"
        else:
            ctl_color, ctl_text = ("green", "свързан") if connected else ("red", "няма връзка")
        if connected:
            sent = row["ctl_sent_meter"].update(control_client.bytes_sent)
            recv = row["ctl_recv_meter"].update(control_client.bytes_recv)
            latency = f"{control_client.latency_ms:.0f}ms" if control_client.latency_ms is not None else "?ms"
            ctl_text += f" · {latency} · ↑{_fmt_rate(sent)} ↓{_fmt_rate(recv)}"
        _set_led(row["ctl_led"], ctl_color)
        row["ctl_label"].setText(ctl_text)

    def _update_audio(self):
        if not self.session.radio_name or not self.session.audio:
            _set_led(self.audio_led, "gray")
            self.audio_label.setText("няма активна аудио връзка")
            return
        radio_audio = self.session.app_cfg.get("radio_audio", {}).get(self.session.radio_name, {})
        audio_cfg = self.session.app_cfg.get("audio", {})
        input_device = radio_audio.get("input_device")
        if input_device is None:
            input_device = audio_cfg.get("input_device")
        output_device = radio_audio.get("output_device")
        if output_device is None:
            output_device = audio_cfg.get("output_device")
        input_name = _device_name(input_device, audio_devices.list_input_devices())
        output_name = _device_name(output_device, audio_devices.list_output_devices())
        sent = self._audio_sent_meter.update(self.session.audio.bytes_sent)
        recv = self._audio_recv_meter.update(self.session.audio.bytes_recv)
        _set_led(self.audio_led, "green")
        self.audio_label.setText(
            f'{self.session.radio_name}: вход "{input_name}" · изход "{output_name}" '
            f"· ↑{_fmt_rate(sent)} ↓{_fmt_rate(recv)}"
        )

    def _update_cw(self):
        if not self.session.cw_link:
            _set_led(self.cw_led, "gray")
            self.cw_label.setText("няма активна CW връзка")
            return
        peer = self.session.cw_link.peer
        peer_text = f"{peer[0]}:{peer[1]}" if peer else "?"
        sent = self._cw_sent_meter.update(self.session.cw_link.bytes_sent)
        recv = self._cw_recv_meter.update(self.session.cw_link.bytes_recv)
        _set_led(self.cw_led, "green")
        self.cw_label.setText(f"{self.session.radio_name} → {peer_text} · ↑{_fmt_rate(sent)} ↓{_fmt_rate(recv)}")


if __name__ == "__main__":
    assert _device_name(None, [(0, "Mic")]) == "по подразбиране"
    assert _device_name(1, [(0, "Mic"), (1, "USB Audio")]) == "USB Audio"
    assert _device_name(5, [(0, "Mic")]) == "устройство #5"

    from PySide6.QtWidgets import QApplication

    class _FakeControlClient:
        def __init__(self, writer):
            self.writer = writer
            self.bytes_sent = 100
            self.bytes_recv = 200
            self.latency_ms = 12.3

    class _FakeCatRelay:
        bytes_sent = 10
        bytes_recv = 20

    class _FakeCwLink:
        peer = ("127.0.0.1", 5104)
        bytes_sent = 1
        bytes_recv = 2

    class _FakeAudio:
        bytes_sent = 1000
        bytes_recv = 2000

    class _FakeSession:
        app_cfg = {
            "radios": [
                {"name": "IC-7300", "active": True},
                {"name": "FT-991", "active": False},
            ],
            "com_ports": {"IC-7300": {"local": "COM5"}},
            "radio_audio": {},
            "audio": {"input_device": None, "output_device": None},
        }
        status_clients = {"IC-7300": _FakeControlClient(object())}
        cat_relays = {"IC-7300": _FakeCatRelay()}
        external_cat_busy: dict = {}
        radio_name = "IC-7300"
        audio = _FakeAudio()
        cw_link = _FakeCwLink()

        def radio_status(self, name):
            return None

    app = QApplication.instance() or QApplication([])
    panel = StatusPanel(_FakeSession(), loop=None)
    panel.tick()

    row = panel._radio_rows["IC-7300"]
    assert "COM5" in row["cat_label"].text() and "свободно" in row["cat_label"].text()
    assert "B/s" in row["cat_label"].text()  # throughput appended even on the first tick (rate 0 until the 2nd)
    assert "свързан" in row["ctl_label"].text() and "12ms" in row["ctl_label"].text()

    inactive_row = panel._radio_rows["FT-991"]
    assert inactive_row["cat_label"].text() == "неактивно"
    assert inactive_row["ctl_label"].text() == "—"

    assert "IC-7300" in panel.audio_label.text() and "B/s" in panel.audio_label.text()
    assert "127.0.0.1:5104" in panel.cw_label.text() and "B/s" in panel.cw_label.text()

    # A second tick with more bytes must show a real >0 rate, not just the
    # "throughput text is present" check above.
    panel.session.cat_relays["IC-7300"].bytes_sent += 4096
    panel.tick()
    assert row["cat_sent_meter"].bytes_per_s > 0

    # A radio busy by another user must take priority over the plain
    # "free" state, and light red — same priority ui.py's own list uses.
    class _FakeSessionBusy(_FakeSession):
        def radio_status(self, name):
            return "OtherOp"

    panel_busy = StatusPanel(_FakeSessionBusy(), loop=None)
    panel_busy.tick()
    assert "заето от OtherOp" in panel_busy._radio_rows["IC-7300"]["cat_label"].text()

    print("status_panel.py: ok")
