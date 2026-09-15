"""Icom RC-28 driver — USB VID:PID 0c26:001e, interrupt transfers, NOT a
standard HID keyboard/mouse (confirmed by the project plan).

STATUS: parse_report() is an UNVERIFIED PLACEHOLDER. The RC-28 protocol
isn't officially documented, and this environment has neither the
physical device nor a USB capture to confirm the report byte layout —
per the project plan, that needs Wireshark + USBPcap on real
RC-28<->RS-BA1 traffic (see gi1mic/rc28_emulator on GitHub as a reference
starting point). Everything downstream of parse_report() — dial delta ->
new frequency -> CI-V set-frequency command — IS verified (see the
self-test and common/civ.py's own self-test), so correcting
parse_report() against a real capture is the only thing standing between
this and a working driver.
"""

import logging

from common.civ import parse_frequency_reply, set_frequency_command

log = logging.getLogger("rc28")

VID, PID = 0x0C26, 0x001E


def parse_report(report: bytes) -> dict:
    """UNVERIFIED PLACEHOLDER. Assumed (unconfirmed) shape: byte[1] =
    signed dial delta since the last report, byte[2] = button bitmask.
    Replace against a real USB capture before trusting this."""
    if len(report) < 3:
        return {"dial_delta": 0, "buttons": 0}
    delta = report[1]
    if delta >= 128:
        delta -= 256
    return {"dial_delta": delta, "buttons": report[2]}


def apply_dial_delta(current_freq_hz: int, delta: int, step_hz: int) -> int:
    return current_freq_hz + delta * step_hz


class Rc28Driver:
    """Wires a physical RC-28 to CAT frequency changes on the currently
    selected radio, through the client's existing CAT relay connection —
    the same tunnel real CAT software uses (see client/com_relay.py)."""

    def __init__(self, send_cat, civ_address: int, step_hz: int = 10):
        self.send_cat = send_cat  # async callable(bytes) -> None, e.g. ComRelay.send_cat
        self.civ_address = civ_address
        self.step_hz = step_hz
        self.current_freq_hz = None

    def on_cat_reply(self, data: bytes):
        """Feed this from ComRelay.on_cat_data to keep our frequency
        baseline in sync with whatever the radio last reported."""
        freq = parse_frequency_reply(data)
        if freq is not None:
            self.current_freq_hz = freq

    async def handle_report(self, report: bytes):
        parsed = parse_report(report)
        delta = parsed["dial_delta"]
        if delta == 0 or self.current_freq_hz is None:
            return
        self.current_freq_hz = apply_dial_delta(self.current_freq_hz, delta, self.step_hz)
        await self.send_cat(set_frequency_command(self.civ_address, self.current_freq_hz))

    async def run(self):
        """Reads real RC-28 USB interrupt reports via hidapi. Needs the
        actual device and a corrected parse_report() to do anything
        useful — see the module docstring."""
        import hid

        dev = hid.device()
        dev.open(VID, PID)
        dev.set_nonblocking(False)
        log.info("RC-28 opened (VID:PID %04x:%04x)", VID, PID)
        try:
            while True:
                report = dev.read(64)
                if report:
                    await self.handle_report(bytes(report))
        finally:
            dev.close()


if __name__ == "__main__":
    assert apply_dial_delta(14250000, 5, 10) == 14250050
    assert apply_dial_delta(14250000, -3, 10) == 14249970
    assert parse_report(b"\x01\x05\x00") == {"dial_delta": 5, "buttons": 0}
    assert parse_report(b"\x01\xfb\x00") == {"dial_delta": -5, "buttons": 0}  # 0xfb = -5 signed
    print("rc28.py: ok (parse_report() is still an unverified placeholder — see module docstring)")
